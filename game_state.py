"""
game_state.py
=============
Pure-Python game state: players, flags, shooting, respawns, scoring.
All mutable state lives here; server.py calls into this module.

Concurrency model
-----------------
All methods that mutate state are async and hold a single asyncio.Lock.
This is safe because aiohttp handles each WebSocket message in the same
event-loop — no extra thread coordination needed outside of frame
processing (which is CPU-bound and dispatched to a ThreadPoolExecutor).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any

log = logging.getLogger('hacktheskies.state')

# ── Tunable constants ────────────────────────────────────────────────────────
RESPAWN_DURATION  = 20.0   # seconds dead before you can respawn
SHOT_COOLDOWN     = 2.5    # minimum seconds between shots by the same player
FRIENDLY_FIRE     = False  # allow shooting teammates?


# ── Player ───────────────────────────────────────────────────────────────────

@dataclass
class Player:
    player_id: int
    team: str                    # 'red' | 'blue'
    alive: bool = True
    respawn_at: float = 0.0      # epoch time; 0 means not dead
    last_shot_at: float = 0.0
    ws: Any = field(default=None, repr=False)   # aiohttp WebSocketResponse

    # ── State queries ────────────────────────────────────────────────────

    def can_shoot(self) -> bool:
        return self.alive and (time.time() - self.last_shot_at) >= SHOT_COOLDOWN

    def seconds_until_respawn(self) -> float:
        if self.alive:
            return 0.0
        return max(0.0, self.respawn_at - time.time())

    # ── State transitions ────────────────────────────────────────────────

    def mark_shot(self):
        self.last_shot_at = time.time()

    def die(self):
        self.alive = False
        self.respawn_at = time.time() + RESPAWN_DURATION
        log.info(f'Player {self.player_id} died; respawn at {self.respawn_at:.0f}')

    def respawn(self):
        self.alive = True
        self.respawn_at = 0.0
        log.info(f'Player {self.player_id} respawned')

    def check_auto_respawn(self):
        """Called periodically; auto-respawn when timer expires."""
        if not self.alive and time.time() >= self.respawn_at:
            self.respawn()

    # ── Serialisation ────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            'player_id':          self.player_id,
            'team':               self.team,
            'alive':              self.alive,
            'respawn_at':         self.respawn_at if not self.alive else None,
            'seconds_to_respawn': round(self.seconds_until_respawn(), 1),
        }


# ── Flag ─────────────────────────────────────────────────────────────────────

@dataclass
class Flag:
    flag_id: int
    x: float                         # map coordinates (normalised 0–1)
    y: float
    owner_team: Optional[str] = None # 'red' | 'blue' | None
    hold_start: float = 0.0          # epoch time current owner started holding
    time_red:  float = 0.0           # accumulated hold seconds
    time_blue: float = 0.0

    def flush_time(self):
        """Credit elapsed time to the current owner and reset the clock."""
        if self.owner_team and self.hold_start > 0:
            now = time.time()
            dt  = now - self.hold_start
            if self.owner_team == 'red':
                self.time_red  += dt
            elif self.owner_team == 'blue':
                self.time_blue += dt
            self.hold_start = now

    def capture(self, team: str):
        """Transfer ownership to a new team."""
        self.flush_time()
        self.owner_team = team
        self.hold_start = time.time()
        log.info(f'Flag {self.flag_id} captured by {team}')

    def to_dict(self) -> dict:
        self.flush_time()
        return {
            'flag_id':    self.flag_id,
            'x':          self.x,
            'y':          self.y,
            'owner_team': self.owner_team,
            'time_red':   round(self.time_red,  1),
            'time_blue':  round(self.time_blue, 1),
        }


# ── GameState ────────────────────────────────────────────────────────────────

class GameState:
    """
    Central game state.  All mutating methods are async and use self._lock.
    """

    def __init__(self):
        self.players: Dict[int, Player] = {}
        self.flags:   Dict[int, Flag]   = {}
        self.game_active: bool  = False
        self.game_start:  float = 0.0
        self._lock = asyncio.Lock()

    # ── Player management ────────────────────────────────────────────────

    async def add_player(self, player_id: int, team: str, ws) -> Player:
        async with self._lock:
            p = Player(player_id=player_id, team=team, ws=ws)
            self.players[player_id] = p
            log.info(f'Registered player {player_id} team={team}')
            return p

    async def remove_player(self, player_id: int):
        async with self._lock:
            self.players.pop(player_id, None)

    # ── Flag management ──────────────────────────────────────────────────

    async def add_flag(self, flag_id: int, x: float, y: float):
        async with self._lock:
            self.flags[flag_id] = Flag(flag_id=flag_id, x=x, y=y)
            log.info(f'Flag {flag_id} added at ({x:.2f}, {y:.2f})')

    async def capture_flag(self, flag_id: int, team: str):
        async with self._lock:
            if flag_id in self.flags:
                self.flags[flag_id].capture(team)

    # ── Shot resolution ──────────────────────────────────────────────────

    async def process_shot(self, shooter_id: int, target_id: int) -> Optional[dict]:
        """
        Attempt to register a shot from shooter_id hitting target_id.

        Returns a shot_event dict if the shot lands, else None.
        Reasons for rejection:
          - Either player not found
          - Shooter on cooldown or dead
          - Target already dead
          - Friendly fire (if disabled)
        """
        async with self._lock:
            shooter = self.players.get(shooter_id)
            target  = self.players.get(target_id)

            if not shooter or not target:
                return None
            if not shooter.can_shoot():
                return None
            if not target.alive:
                return None
            if not FRIENDLY_FIRE and shooter.team == target.team:
                return None

            shooter.mark_shot()
            target.die()

            return {
                'type':       'shot_event',
                'shooter_id': shooter_id,
                'target_id':  target_id,
                'respawn_at': target.respawn_at,
            }

    # ── Respawn triggers ─────────────────────────────────────────────────

    async def request_respawn(self, player_id: int, base_tag_id: int) -> bool:
        """
        Player scanned their team's base tag.  Respawn them if timer expired.
        Returns True if respawned.
        """
        async with self._lock:
            p = self.players.get(player_id)
            if not p or p.alive:
                return False
            # Validate that the base tag belongs to their team
            # Base tag IDs: 100 = red base, 101 = blue base
            expected_base = 100 if p.team == 'red' else 101
            if base_tag_id != expected_base:
                return False
            if time.time() < p.respawn_at:
                return False  # timer not up yet
            p.respawn()
            return True

    async def check_auto_respawns(self):
        """Called periodically; auto-respawn players whose timer has expired."""
        async with self._lock:
            for p in self.players.values():
                p.check_auto_respawn()

    # ── Game control ─────────────────────────────────────────────────────

    async def start_game(self):
        async with self._lock:
            self.game_active = True
            self.game_start  = time.time()
            # Reset all players and flag timers
            for p in self.players.values():
                p.alive      = True
                p.respawn_at = 0.0
            for f in self.flags.values():
                f.owner_team = None
                f.hold_start = 0.0
                f.time_red   = 0.0
                f.time_blue  = 0.0
            log.info('Game started')

    async def stop_game(self) -> dict:
        async with self._lock:
            self.game_active = False
            scores = self._scores_locked()
            log.info(f'Game over — scores: {scores}')
            return scores

    # ── Scoring ──────────────────────────────────────────────────────────

    def _scores_locked(self) -> dict:
        """Must be called while holding self._lock (or read-only)."""
        red  = sum(f.time_red  for f in self.flags.values())
        blue = sum(f.time_blue for f in self.flags.values())
        return {
            'red':    round(red,  1),
            'blue':   round(blue, 1),
            'winner': ('red' if red > blue else ('blue' if blue > red else 'tie')),
        }

    def scores(self) -> dict:
        """Thread-safe read. Flushes in-progress hold time before summing."""
        for f in self.flags.values():
            f.flush_time()
        red  = sum(f.time_red  for f in self.flags.values())
        blue = sum(f.time_blue for f in self.flags.values())
        return {
            'red':    round(red,  1),
            'blue':   round(blue, 1),
            'winner': ('red' if red > blue else ('blue' if blue > red else 'tie')),
        }

    # ── Snapshot (for host dashboard) ────────────────────────────────────

    def snapshot(self) -> dict:
        return {
            'players':     {pid: p.to_dict() for pid, p in self.players.items()},
            'flags':       {fid: f.to_dict() for fid, f in self.flags.items()},
            'scores':      self.scores(),
            'game_active': self.game_active,
            'elapsed':     round(time.time() - self.game_start, 1) if self.game_active else 0,
        }
