"""
game_state.py  (simplified — shoot-only)
=========================================
Tracks players and processes shots.  Each player has MAX_HP hit points;
each hit takes one, and the player dies when they reach zero.  Shooting is
one-handed; after every hit the gun reloads with a clap (see Gun).
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, Any, Tuple

log = logging.getLogger('hacktheskies.state')

MAX_HP           = 3      # hits it takes to kill a player
FRIENDLY_FIRE    = False
ARM_DELAY_S      = 1.0    # wait after a reload clap before the gun fires again
CLAP_MATCH_S     = 0.25   # clap reports from different phones this close together = one clap


@dataclass
class Gun:
    """
    One-handed gun.  While loaded it fires on every frame that aims at a tag,
    until a shot actually hits (misses don't empty it).  After a hit it is
    empty until a reload clap (heard by the phone's mic, or seen by the
    camera), and then fires again ARM_DELAY_S later.  Starts loaded and ready;
    respawn and a new game reset it.

    reloads counts real reloads (not resets), so the phone can play the reload
    sound exactly once per clap.
    """
    loaded: bool = True
    loaded_at: float = float('-inf')
    reloads: int = 0

    def update(self, clapped: bool, now: float) -> None:
        if clapped and not self.loaded:
            self.loaded, self.loaded_at = True, now
            self.reloads += 1

    def arming_left(self, now: float) -> float:
        return max(0.0, ARM_DELAY_S - (now - self.loaded_at))

    def ready(self, now: float) -> bool:
        return self.loaded and self.arming_left(now) == 0.0

    def state(self, now: float) -> str:
        """'reload' (clap to reload) | 'arming' (just reloaded) | 'ready'"""
        if not self.loaded:
            return 'reload'
        return 'ready' if self.ready(now) else 'arming'

    def fired(self) -> None:
        self.loaded = False

    def reset(self) -> None:
        self.loaded, self.loaded_at = True, float('-inf')


@dataclass
class Player:
    player_id: int
    team: str                    # 'red' | 'blue'
    alive: bool = True
    hp: int = MAX_HP
    respawn_at: float = 0.0
    kills: int = 0
    deaths: int = 0
    gun: Gun = field(default_factory=Gun)
    ws: Any = field(default=None, repr=False)
    connected: bool = True       # False: phone dropped mid-game; kept so kills still count

    def seconds_until_respawn(self) -> float:
        if self.alive:
            return 0.0
        return max(0.0, self.respawn_at - time.time())

    def die(self, respawn_secs: float = 10.0):
        self.alive = False
        self.respawn_at = time.time() + respawn_secs
        self.deaths += 1
        log.info(f'Player {self.player_id} died (deaths={self.deaths})')

    def respawn(self):
        """Back in the game: full HP and a loaded gun.  Call this from whatever
        decides respawning (the 10 s timer below is a placeholder for the
        walk-back-to-your-spawn-point code); the phone plays its "Back in"
        sound on its own when it sees the player alive again."""
        self.alive = True
        self.hp = MAX_HP
        self.respawn_at = 0.0
        self.gun.reset()
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
            'connected':          self.connected,
            'respawn_at':         self.respawn_at if not self.alive else None,
            'seconds_to_respawn': round(self.seconds_until_respawn(), 1),
        }


class ClapReferee:
    """
    Decides whose clap it was.  Every phone reports every clap its microphone
    hears, with how loud it was.  One real clap is heard by all phones nearby at
    about the same moment, and loudest by the clapper's own phone (right next to
    their hands), so reports within CLAP_MATCH_S of each other are grouped and
    the clap belongs to the loudest reporter only.
    """

    def __init__(self):
        self.pending: list[tuple[float, int, float]] = []   # (time, player_id, peak)

    def heard(self, player_id: int, peak: float, now: float) -> None:
        self.pending.append((now, player_id, peak))

    def settle(self, now: float) -> list[tuple[int, list[int]]]:
        """Resolve claps whose window has closed: [(clapper, [other phones that heard it])]."""
        self.pending.sort()
        decided = []
        while self.pending and now - self.pending[0][0] >= CLAP_MATCH_S:
            t0 = self.pending[0][0]
            group = [c for c in self.pending if c[0] - t0 <= CLAP_MATCH_S]
            self.pending = [c for c in self.pending if c[0] - t0 > CLAP_MATCH_S]
            clapper = max(group, key=lambda c: c[2])[1]
            decided.append((clapper, sorted({c[1] for c in group} - {clapper})))
        return decided


class GameState:
    def __init__(self):
        self.players: Dict[int, Player] = {}
        self.game_active: bool = False
        self.game_start:  float = 0.0
        self._lock = asyncio.Lock()
        self.claps = ClapReferee()

    # ── Player management ────────────────────────────────────────────────

    async def add_player(self, player_id: int, team: str, ws) -> Player:
        """Register a phone.  A phone re-joining as the same player (Wi-Fi drop,
        page refresh) gets its player back exactly as it was — HP, gun, kills,
        alive or down — not a fresh one, so reconnecting is never a free heal."""
        async with self._lock:
            p = self.players.get(player_id)
            if p is not None and p.team == team:
                p.ws, p.connected = ws, True
                log.info(f'Player {player_id} rejoined (hp={p.hp}, alive={p.alive})')
                return p
            p = Player(player_id=player_id, team=team, ws=ws)
            self.players[player_id] = p
            log.info(f'Registered player {player_id} team={team}')
            return p

    async def remove_player(self, player_id: int, ws=None) -> Optional[str]:
        """A phone disconnected.  If ws is given and a newer socket has taken the
        player over (it rejoined), nothing happens: returns None.  During a game
        the player is kept, marked offline — their kills still count for the
        team and they can rejoin: returns 'offline'.  Otherwise: 'removed'."""
        async with self._lock:
            p = self.players.get(player_id)
            if p is None or (ws is not None and p.ws is not ws):
                return None
            if self.game_active:
                p.ws, p.connected = None, False
                return 'offline'
            del self.players[player_id]
            return 'removed'

    # ── Shot resolution ──────────────────────────────────────────────────

    async def process_shot(self, shooter_id: int, target_id: int,
                           now: Optional[float] = None) -> Tuple[Optional[dict], str]:
        """Returns (shot_event or None, reason).  reason is 'hit' on success,
        otherwise why the shot was rejected — shown on the shooter's screen.
        A rejected shot leaves the gun loaded (it keeps firing); a hit empties it.
        now: the clock the shooter's gun was updated with (server: time.monotonic)."""
        now = time.monotonic() if now is None else now
        async with self._lock:
            shooter = self.players.get(shooter_id)
            target  = self.players.get(target_id)
            if not shooter:
                return None, 'not_registered'
            if not shooter.alive:
                return None, 'dead'
            if not shooter.gun.ready(now):
                return None, shooter.gun.state(now)    # 'reload' | 'arming'
            if not target:
                return None, 'target_not_in_game'
            if not target.alive:
                return None, 'target_dead'
            if not FRIENDLY_FIRE and shooter.team == target.team:
                return None, 'same_team'

            shooter.gun.fired()
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
            # Players whose phones left during the last game aren't in this one
            for pid in [pid for pid, p in self.players.items() if not p.connected]:
                del self.players[pid]
            for p in self.players.values():
                p.alive      = True
                p.hp         = MAX_HP
                p.respawn_at = 0.0
                p.kills      = 0
                p.deaths     = 0
                p.gun.reset()
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
