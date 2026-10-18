"""
objectives.py
=============
The game: two bases and a flag in the middle, marked by the host on the map
(the game server's map "points", in its metres).

  * A downed player is back in the moment they step into their own team's
    base.  Players whose position isn't tracked (not placed, no game server,
    the web phone page) or whose team has no base come back on a timer instead,
    so nobody is stuck down for good; it only runs while they can't be tracked.
    Down, a player can't shoot, reload or count in the flag's circle.
  * The team with more alive players inside the flag's circle takes the flag,
    once it has outnumbered the other for CAPTURE_S (a position that jitters
    across the edge can't steal it).  The owner keeps it until the other team
    outnumbers them there: an empty or tied circle changes nothing.
  * While a team owns the flag its clock runs.  The first team whose clock
    reaches win_s (total time: losing the flag pauses it) wins.

Kept free of networking so tests can drive it with made-up positions and time.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

TEAMS = ('red', 'blue')
CAPTURE_S = 3.0          # outnumber the owners in the flag circle this long to take it
DEFAULT_WIN_S = 120.0    # flag time that wins, unless the host picks another at the start
WIN_S_MIN = 10.0
WIN_S_MAX = 3600.0
RESPAWN_TIMER_S = 10.0   # comeback for players who can't walk to a base (see above)


@dataclass
class Zone:
    x: float
    y: float
    r: float

    def contains(self, pos: tuple[float, float]) -> bool:
        return math.hypot(pos[0] - self.x, pos[1] - self.y) <= self.r

    def to_dict(self) -> dict:
        return {'x': self.x, 'y': self.y, 'r': self.r}


def clamp_win_s(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return DEFAULT_WIN_S
    return DEFAULT_WIN_S if math.isnan(v) else min(max(v, WIN_S_MIN), WIN_S_MAX)


class Objectives:
    def __init__(self):
        self.bases: dict[str, Zone] = {}
        self.flag: Zone | None = None
        self.reset()

    def set_points(self, points: list[dict]) -> None:
        """The map's objectives, as the game server stores them: {kind, team, x, y, r}."""
        bases, flag = {}, None
        for p in points if isinstance(points, list) else []:
            try:
                zone = Zone(float(p['x']), float(p['y']), float(p['r']))
            except (KeyError, TypeError, ValueError):
                continue
            if p.get('kind') == 'base' and p.get('team') in TEAMS:
                bases[p['team']] = zone
            elif p.get('kind') == 'flag':
                flag = zone
        self.bases, self.flag = bases, flag

    def reset(self, win_s=None) -> None:
        """A new match: nobody owns the flag, both clocks at zero.  win_s: the
        flag time that wins (DEFAULT_WIN_S if not given)."""
        self.win_s = clamp_win_s(win_s)
        self.owner: str | None = None
        self.held = {t: 0.0 for t in TEAMS}
        self.challenger: str | None = None
        self.challenge_since = 0.0
        self.inside = {t: 0 for t in TEAMS}
        self.last: float | None = None

    def end(self) -> None:
        """The match is over: the owner and clocks stay (the result), nobody is taking the flag."""
        self.challenger = None
        self.inside = {t: 0 for t in TEAMS}
        self.last = None

    def update(self, players: dict, positions: dict[int, tuple[float, float]],
               now: float, wall: float) -> list[dict]:
        """One step of a running match.  players: player_id -> game_state.Player;
        positions: player_id -> (x, y) for the players being tracked; now: a
        monotonic clock; wall: time.time(), which Player.respawn_at uses.
        Returns what happened: respawned / flag_taken / win."""
        events = []
        for pid, p in players.items():
            pos, base = positions.get(pid), self.bases.get(p.team)
            p.respawn_mode = 'base' if base and pos is not None else 'timer'
            if p.alive:
                continue
            if p.respawn_mode == 'base':
                # The timer only runs while they can't be followed: losing tracking for a
                # moment mustn't bring a downed player back where they stand.
                p.respawn_at = max(p.respawn_at, wall + RESPAWN_TIMER_S)
            if p.respawn_mode == 'base' and base.contains(pos):
                how = 'base'
            elif p.respawn_mode == 'timer' and wall >= p.respawn_at:
                how = 'timer'
            else:
                continue
            p.respawn()
            events.append({'type': 'respawned', 'player_id': pid, 'how': how})

        dt = 0.0 if self.last is None else max(0.0, now - self.last)
        self.last = now
        if self.flag is None:
            self.inside = {t: 0 for t in TEAMS}
            self.challenger = None
            return events
        if self.owner:
            self.held[self.owner] += dt

        self.inside = {t: sum(1 for pid, p in players.items()
                              if p.team == t and p.alive and pid in positions
                              and self.flag.contains(positions[pid]))
                       for t in TEAMS}
        red, blue = self.inside['red'], self.inside['blue']
        leader = 'red' if red > blue else 'blue' if blue > red else None
        if leader is None or leader == self.owner:
            self.challenger = None
        elif leader != self.challenger:
            self.challenger, self.challenge_since = leader, now
        elif now - self.challenge_since >= CAPTURE_S:
            events.append({'type': 'flag_taken', 'team': leader, 'from': self.owner})
            self.owner, self.challenger = leader, None

        if self.owner and self.held[self.owner] >= self.win_s:
            events.append({'type': 'win', 'team': self.owner})
        return events

    def leading(self) -> str | None:
        """The team with more flag time (None: no flag, or level)."""
        red, blue = self.held['red'], self.held['blue']
        if self.flag is None or red == blue:
            return None
        return 'red' if red > blue else 'blue'

    def brief(self) -> dict | None:
        """What the phones need: who owns the flag and the clocks."""
        if self.flag is None:
            return None
        return {'owner': self.owner, 'challenger': self.challenger, 'win_s': self.win_s,
                'held': {t: round(s, 1) for t, s in self.held.items()}}

    def to_dict(self, now: float) -> dict:
        flag = None
        if self.flag is not None:
            progress = min(1.0, (now - self.challenge_since) / CAPTURE_S) if self.challenger else 0.0
            flag = {**self.flag.to_dict(), 'owner': self.owner, 'challenger': self.challenger,
                    'progress': round(progress, 2), 'inside': dict(self.inside)}
        return {
            'bases':     {t: z.to_dict() for t, z in self.bases.items()},
            'flag':      flag,
            'held':      {t: round(s, 1) for t, s in self.held.items()},
            'win_s':     self.win_s,
            'capture_s': CAPTURE_S,
        }
