"""
game_state.py  (simplified — shoot-only)
=========================================
Tracks players and processes shots.  Each player has MAX_HP hit points;
each hit takes one, and the player dies when they reach zero.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, Tuple

log = logging.getLogger('hacktheskies.state')

SHOT_COOLDOWN  = 2.5    # seconds between shots by the same player
MAX_HP         = 3      # hits it takes to kill a player
FRIENDLY_FIRE  = False


@dataclass
class Player:
    player_id: int
    team: str                    # 'red' | 'blue'
    alive: bool = True
    hp: int = MAX_HP
    respawn_at: float = 0.0
    last_shot_at: float = 0.0
    kills: int = 0
    deaths: int = 0
    ws: Any = field(default=None, repr=False)

    def can_shoot(self) -> bool:
        return self.alive and self.cooldown_left() == 0.0

    def cooldown_left(self) -> float:
        return max(0.0, SHOT_COOLDOWN - (time.time() - self.last_shot_at))

    def seconds_until_respawn(self) -> float:
        if self.alive:
            return 0.0
        return max(0.0, self.respawn_at - time.time())

    def mark_shot(self):
        self.last_shot_at = time.time()

    def die(self, respawn_secs: float = 10.0):
        self.alive = False
        self.respawn_at = time.time() + respawn_secs
        self.deaths += 1
        log.info(f'Player {self.player_id} died (deaths={self.deaths})')

    def respawn(self):
        self.alive = True
        self.hp = MAX_HP
        self.respawn_at = 0.0
        log.info(f'Player {self.player_id} respawned')

    def check_auto_respawn(self):
        if not self.alive and time.time() >= self.respawn_at:
            self.respawn()

    def to_dict(self) -> dict:
        return {
            'player_id':          self.player_id,
            'team':               self.team,
            'alive':              self.alive,
            'hp':                 self.hp,
            'max_hp':             MAX_HP,
            'kills':              self.kills,
            'deaths':             self.deaths,
            'respawn_at':         self.respawn_at if not self.alive else None,
            'seconds_to_respawn': round(self.seconds_until_respawn(), 1),
        }


class GameState:
    def __init__(self):
        self.players: Dict[int, Player] = {}
        self.game_active: bool = False
        self.game_start:  float = 0.0
        self._lock = asyncio.Lock()

    # ── Player management ────────────────────────────────────────────────

    async def add_player(self, player_id: int, team: str, ws) -> Player:
        async with self._lock:
            p = Player(player_id=player_id, team=team, ws=ws)
            self.players[player_id] = p
            log.info(f'Registered player {player_id} team={team}')
            return p

    async def remove_player(self, player_id: int, ws=None) -> bool:
        """Remove a player.  If ws is given, only remove if that socket still owns
        the registration — a phone that reconnected must not be removed by its old
        socket closing late.  Returns True if removed."""
        async with self._lock:
            p = self.players.get(player_id)
            if p is None or (ws is not None and p.ws is not ws):
                return False
            del self.players[player_id]
            return True

    # ── Shot resolution ──────────────────────────────────────────────────

    async def process_shot(self, shooter_id: int, target_id: int) -> Tuple[Optional[dict], str]:
        """Returns (shot_event or None, reason).  reason is 'hit' on success,
        otherwise why the shot was rejected — shown on the shooter's screen."""
        async with self._lock:
            shooter = self.players.get(shooter_id)
            target  = self.players.get(target_id)
            if not shooter:
                return None, 'not_registered'
            if not target:
                return None, 'target_not_in_game'
            if not shooter.alive:
                return None, 'dead'
            if not shooter.can_shoot():
                return None, 'cooldown'
            if not target.alive:
                return None, 'target_dead'
            if not FRIENDLY_FIRE and shooter.team == target.team:
                return None, 'same_team'

            shooter.mark_shot()
            target.hp -= 1
            killed = target.hp <= 0
            if killed:
                target.hp = 0
                target.die(respawn_secs=10.0)
                shooter.kills += 1
            log.info(f'P{shooter_id} hit P{target_id}: {target.hp}/{MAX_HP} HP'
                     f'{" (killed)" if killed else ""}')

            return {
                'type':       'shot_event',
                'shooter_id': shooter_id,
                'target_id':  target_id,
                'hp':         target.hp,
                'max_hp':     MAX_HP,
                'killed':     killed,
                'respawn_at': target.respawn_at if killed else None,
            }, 'hit'

    # ── Auto-respawn ─────────────────────────────────────────────────────

    async def check_auto_respawns(self):
        async with self._lock:
            for p in self.players.values():
                p.check_auto_respawn()

    # ── Game control ─────────────────────────────────────────────────────

    async def start_game(self):
        async with self._lock:
            self.game_active = True
            self.game_start  = time.time()
            for p in self.players.values():
                p.alive      = True
                p.hp         = MAX_HP
                p.respawn_at = 0.0
                p.kills      = 0
                p.deaths     = 0
            log.info('Game started')

    async def stop_game(self) -> dict:
        async with self._lock:
            self.game_active = False
            scores = self._scores_locked()
            log.info(f'Game over: {scores}')
            return scores

    # ── Scoring ──────────────────────────────────────────────────────────

    def _scores_locked(self) -> dict:
        red_k  = sum(p.kills  for p in self.players.values() if p.team == 'red')
        blue_k = sum(p.kills  for p in self.players.values() if p.team == 'blue')
        red_d  = sum(p.deaths for p in self.players.values() if p.team == 'red')
        blue_d = sum(p.deaths for p in self.players.values() if p.team == 'blue')
        if red_k > blue_k:
            winner = 'red'
        elif blue_k > red_k:
            winner = 'blue'
        else:
            winner = 'tie'
        return {'red_kills': red_k, 'blue_kills': blue_k,
                'red_deaths': red_d, 'blue_deaths': blue_d,
                'winner': winner}

    def scores(self) -> dict:
        return self._scores_locked()

    # ── Snapshot ─────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        import time as _t
        return {
            'players':     {str(pid): p.to_dict() for pid, p in self.players.items()},
            'scores':      self.scores(),
            'game_active': self.game_active,
            'elapsed':     round(_t.time() - self.game_start, 1) if self.game_active else 0,
        }
