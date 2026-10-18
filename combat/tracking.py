"""
tracking.py
===========
Where everyone is, from the game server (server/main.py, started by run.py):
this connects to it as a read-only "watch" client and gets the game map (its
bases and flag) and ~20 position updates a second.  Players are matched by
their tag number: the game server's player "tag" is the player_id here.

Without a game server (this server run on its own) nobody is tracked, and the
objectives fall back to respawn timers.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from typing import Callable

import aiohttp

log = logging.getLogger('hacktheskies.tracking')

STALE_S = 2.0     # positions older than this don't count (the game server stopped sending)
RETRY_MAX_S = 5.0


def ws_url(server: str) -> str:
    """The game server's address (https://host:8443, as the viewer's GAME_SERVER) -> its WebSocket."""
    server = server.rstrip('/')
    for http, ws in (('https://', 'wss://'), ('http://', 'ws://')):
        if server.startswith(http):
            return ws + server[len(http):] + '/ws'
    return f'wss://{server}/ws'


def tracked_positions(players: list) -> dict[int, tuple[float, float]]:
    """player_id -> (x, y) map metres, for players the game server has placed and still hears from."""
    out = {}
    for p in players if isinstance(players, list) else []:
        try:
            if p.get('tag') and p.get('connected') and p.get('calibrated') and 'x' in p:
                out[int(p['tag'])] = (float(p['x']), float(p['y']))
        except (AttributeError, TypeError, ValueError):
            continue
    return out


class PositionFeed:
    def __init__(self, url: str | None, on_points: Callable[[list], None] = lambda points: None):
        self.url = url
        self.on_points = on_points
        self.connected = False
        self._positions: dict[int, tuple[float, float]] = {}
        self._at = -math.inf
        self._warned = False

    def on_message(self, msg: dict, now: float) -> None:
        """One message from the game server: the map (objectives) or everyone's position."""
        if msg.get('t') == 'map':
            self.on_points((msg.get('map') or {}).get('points') or [])
        elif msg.get('t') == 'state':
            self._positions = tracked_positions(msg.get('players'))
            self._at = now

    def positions(self, now: float) -> dict[int, tuple[float, float]]:
        return dict(self._positions) if now - self._at <= STALE_S else {}

    async def run(self) -> None:
        delay = 0.5
        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    # The game server's certificate is self-signed
                    async with session.ws_connect(self.url, ssl=False, heartbeat=10.0) as ws:
                        await ws.send_json({'role': 'watch'})
                        self.connected, self._warned, delay = True, False, 0.5
                        log.info(f'Following positions and objectives from the game server ({self.url})')
                        async for msg in ws:
                            if msg.type == aiohttp.WSMsgType.TEXT:
                                self.on_message(json.loads(msg.data), time.monotonic())
                            elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                                break
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not (self._warned or self.connected):
                    self._warned = True
                    log.warning(f'No game server at {self.url} ({exc.__class__.__name__}): '
                                'nobody is tracked, downed players come back on a timer')
            if self.connected:
                self.connected, self._warned = False, True
                log.warning('Lost the game server: nobody is tracked until it is back')
            self._positions = {}
            await asyncio.sleep(delay)
            delay = min(delay * 2, RETRY_MAX_S)
