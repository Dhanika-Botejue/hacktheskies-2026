# -*- coding: utf-8 -*-
"""
test_gun.py
===========
End-to-end test of the gun: the real server, real WebSocket phones and a real
host dashboard socket, all in this one process (no separate terminal, no
camera).  Each phone "frame" is a small JSON note saying what the camera sees
this frame (which tag the finger points at, finger-gun pose or not, one hand /
two hands / a clap), and the camera pipeline is swapped for one that returns
exactly that.  Everything after it is the real server code: aiming windows,
the gun, hits, HP, kills, mic + camera reload claps, the clap referee, scores,
reconnects.

Respawning is a placeholder (the walk-back-to-your-spawn-point code replaces
it), so the automatic respawn is switched off here and respawns are done by
calling player.respawn() — exactly what that code should call.

    python test_gun.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

import aiohttp
from aiohttp.test_utils import TestServer

import server
from game_state import ARM_DELAY_S, CLAP_MATCH_S

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
passed = failed = 0

CLAP_SETTLE = CLAP_MATCH_S + 0.15   # the server waits for other phones' reports of a clap
ARM         = ARM_DELAY_S + 0.15    # the wait after a reload


def check(label: str, cond, detail='') -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f'  {PASS}  {label}')
    else:
        failed += 1
        print(f'  {FAIL}  {label}  {detail}')


# ── Scripted camera ───────────────────────────────────────────────────────────
# palms: (centre px, size px) of the wearer's hands, as hand_detector.own_palms gives
_PALMS = {
    0:      [],
    1:      [((640.0, 500.0), 100.0)],
    2:      [((300.0, 500.0), 100.0), ((980.0, 500.0), 100.0)],   # both hands, apart
    'clap': [((610.0, 500.0), 100.0), ((690.0, 500.0), 100.0)],   # both hands together
}


def fake_process_frame(data: bytes, remembered: list, people=None) -> dict:
    """What the camera sees, from the phone's JSON note:
    aim=tag the finger points at, ready=finger-gun pose, hands=0/1/2/'clap',
    tags=tags in view (default: the aimed one), unknown=the finger points at a
    person the server doesn't recognise, corrupt=undecodable frame."""
    see = json.loads(data)
    if see.get('corrupt'):
        return {'error': 'jpeg_decode_failed'}
    aim, palms = see.get('aim'), _PALMS[see.get('hands', 1)]
    one = len(palms) == 1                    # one hand = it can shoot
    ready = one and see.get('ready', True)
    tags = [{'id': i, 'center': (640.0, 300.0), 'margin': 50.0, 'role': 'player',
             'corners': [[600, 260], [680, 260], [680, 340], [600, 340]]}
            for i in see.get('tags', [aim] if aim else [])]
    tag = next((t for t in tags if t['id'] == aim), None) if one else None
    unknown = [{'id': None, 'corners': [[900, 200], [1000, 200], [1000, 500], [900, 500]],
                'stale': False, 'kind': 'person'}] if see.get('unknown') else []
    return {
        'pose': 1.0 if ready else 0.0, 'armed': ready, 'ready': ready, 'shooting': one,
        'own_palms': palms, 'n_hands': len(palms), 'detected': bool(palms),
        'landmarks': None, 'other_landmarks': [], 'criteria': {}, 'tag': tag,
        'fresh_tags': tags, 'aimed_unknown': one and tag is None and bool(unknown),
        'tags_seen': [{'id': t['id'], 'corners': t['corners'], 'stale': False} for t in tags] + unknown,
        'frame_w': 1280, 'frame_h': 720,
    }


# ── Clients ───────────────────────────────────────────────────────────────────

class Phone:
    """A phone: replies to its own messages go to a queue, pushes (shot_event) to a list."""

    def __init__(self, name: str):
        self.name, self.pushed, self.q = name, [], asyncio.Queue()

    async def connect(self, session, url):
        self.ws = await session.ws_connect(url)
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            if d.get('type') in ('frame_result', 'idle', 'registered') or 'error' in d:
                await self.q.put(d)
            else:
                self.pushed.append(d)

    async def reply(self) -> dict:
        return await asyncio.wait_for(self.q.get(), 10)

    async def join(self, pid, team) -> dict:
        await self.ws.send_json({'type': 'register', 'player_id': pid, 'team': team})
        return await self.reply()

    async def frame(self, **see) -> dict:
        await self.ws.send_bytes(json.dumps(see).encode())
        return await self.reply()

    async def clap(self, peak: float):
        await self.ws.send_json({'type': 'clap', 'peak': peak})

    def of(self, kind: str) -> list:
        return [d for d in self.pushed if d.get('type') == kind]

    async def close(self):
        await self.ws.close()
        await self.task


class Host:
    async def connect(self, session, url):
        self.got = []
        self.ws = await session.ws_connect(url)
        self.task = asyncio.create_task(self._read())

    async def _read(self):
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                self.got.append(json.loads(msg.data))

    def of(self, kind: str) -> list:
        return [d for d in self.got if d.get('type') == kind]


# ── The game ──────────────────────────────────────────────────────────────────

async def run() -> int:
    server._process_frame = fake_process_frame

    async def no_auto_respawn():          # placeholder respawn off (see top)
        pass
    server.game.check_auto_respawns = no_auto_respawn

    ts = TestServer(server.build_app())
    await ts.start_server()
    g = server.game
    async with aiohttp.ClientSession() as s:
        url = str(ts.make_url('/ws/phone'))
        host = Host()
        await host.connect(s, str(ts.make_url('/ws/host')))
        p1, p2, p3 = Phone('P1'), Phone('P2'), Phone('P3')
        for p in (p1, p2, p3):
            await p.connect(s, url)

        print('\n1. Joining')
        r = await p1.join(50, 'red')
        check('player ID with no printed tag (50) is refused',
              r.get('error') == 'invalid_player_id' and r.get('max_player_id') == 34, r)
        r = await p1.join(0, 'red')
        check('player ID 0 is refused', r.get('error') == 'invalid_player_id', r)
        r = await p1.join(1, 'green')
        check('unknown team is refused', r.get('error') == 'invalid_team', r)
        await p1.ws.send_json({'type': 'register'})
        r = await p1.reply()
        check('register without an ID is refused', r.get('error') == 'bad_register', r)
        r = await p1.join(1, 'red')
        check('P1 (red) joins on the same connection after those refusals', r.get('type') == 'registered', r)
        r = await p2.join(2, 'blue')
        check('P2 (blue) joins', r.get('type') == 'registered', r)
        r = await p3.join(3, 'red')
        check('P3 (red) joins', r.get('type') == 'registered', r)
        await asyncio.sleep(0.1)
        check('host sees the three players join', len(host.of('player_joined')) == 3, host.of('player_joined'))

        print('\n2. Before the game starts')
        r = await p1.frame(aim=2)
        check('frames before Start get "idle": no shooting', r.get('type') == 'idle' and g.players[2].hp == 3, r)
        await host.ws.send_json({'type': 'start_game'})
        await asyncio.sleep(0.1)
        check('host presses Start: game running', g.game_active and host.of('game_started'))

        print('\n3. A hit')
        r = await p1.frame(aim=2)
        check('P1 points a finger gun at P2: HIT',
              r['status'] == 'hit' and r['shot_fired'] and r['aiming_tag_id'] == 2, r['status'])
        check('...P2 down to 2 HP', r['target_hp'] == 2 and g.players[2].hp == 2, r['target_hp'])
        check("...P1's gun is now empty", r['gun'] == 'reload', r['gun'])
        await asyncio.sleep(0.05)
        ev = p2.of('shot_event')
        check("P2's phone is told it was hit (2/3 HP)",
              len(ev) == 1 and ev[0]['hp'] == 2 and not ev[0]['killed'], ev)
        check('host log gets the hit', len(host.of('shot_event')) == 1, host.of('shot_event'))
        r = await p1.frame(aim=2)
        check('still aiming with an empty gun: no second hit',
              r['status'] == 'reload' and not r['shot_fired'] and g.players[2].hp == 2, r['status'])

        print('\n4. Reload by clapping (microphone)')
        await p1.clap(0.9)
        await p2.clap(0.5)                       # P2's phone hears P1's clap, quieter
        await asyncio.sleep(CLAP_SETTLE)
        r = await p1.frame(aim=2)
        check("P1's clap reloads P1 (then the 1 s wait)",
              r['gun'] == 'arming' and r['status'] == 'arming' and r['reloads'] == 1, r)
        check('...no shot during the 1 s wait', not r['shot_fired'] and g.players[2].hp == 2)
        await asyncio.sleep(ARM)
        r = await p1.frame(aim=2)
        check('1 s after the clap the gun fires again: HIT (1 HP left)',
              r['status'] == 'hit' and r['target_hp'] == 1, r['status'])

        print("\n5. Someone else's clap")
        await p2.clap(0.95)
        await p1.clap(0.6)                       # P2 claps right next to P1
        await asyncio.sleep(CLAP_SETTLE)
        r = await p1.frame()
        check("P2's clap next to P1 doesn't reload P1", r['gun'] == 'reload' and r['reloads'] == 1, r['gun'])
        await p1.clap(0.8)                       # nobody else heard this one
        await asyncio.sleep(CLAP_SETTLE)
        r = await p1.frame()
        check("P1's own clap does", r['gun'] == 'arming' and r['reloads'] == 2, r['gun'])
        await asyncio.sleep(ARM)

        print('\n6. The kill')
        r = await p1.frame(aim=2)
        check('third hit kills P2', r['status'] == 'hit' and r['shot_killed'] and r['target_hp'] == 0, r['status'])
        check('...kill credited to P1, red team 1 kill',
              r['kills'] == 1 and r['scores']['red_kills'] == 1, r['scores'])
        await asyncio.sleep(0.05)
        ev = p2.of('shot_event')[-1]
        check("P2's phone is told it was killed", ev['killed'] and ev['hp'] == 0, ev)
        check('host log gets the kill', host.of('shot_event')[-1]['killed'])
        r = await p2.frame(aim=1)
        check("P2 is down: can't shoot", r['status'] == 'dead' and not r['alive'] and g.players[1].hp == 3, r['status'])

        print('\n7. Reload by clapping (camera backup); misses keep the gun loaded')
        r = await p1.frame(hands='clap')
        check('camera sees P1 clap: reloaded', r['gun'] == 'arming' and r['reloads'] == 3, r['gun'])
        await asyncio.sleep(ARM)
        r = await p1.frame(aim=2)
        check('shooting a player who is down: no hit', r['status'] == 'target_dead', r['status'])
        r = await p1.frame(aim=3)
        check('shooting a teammate: no hit', r['status'] == 'same_team' and g.players[3].hp == 3, r['status'])
        r = await p1.frame(aim=7)
        check("shooting a tag nobody's wearing: no hit", r['status'] == 'target_not_in_game', r['status'])
        check('...after those misses the gun is still loaded', r['gun'] == 'ready', r['gun'])

        print("\n8. Respawn (the spawn-point code calls player.respawn())")
        g.players[2].respawn()
        r = await p2.frame()
        check('P2 back in: alive, 3 HP, gun loaded', r['alive'] and r['hp'] == 3 and r['gun'] == 'ready', r)

        print('\n9. One hand only')
        r = await p1.frame(aim=2, hands=2)
        check('both hands in view: no shot even when aimed', r['status'] == 'two_hands' and not r['shot_fired'], r['status'])
        r = await p1.frame(aim=2, ready=False)
        check('aimed without a finger-gun pose: no shot', r['status'] == 'pose_not_ready', r['status'])
        r = await p1.frame(unknown=True, ready=True)
        check("swinging onto someone not recognised: no shot at the player aimed at a moment ago",
              r['status'] == 'not_identified' and not r['shot_fired'] and g.players[2].hp == 3, r['status'])
        await p1.frame(hands=2)
        r = await p1.frame(ready=True)
        check("raising the 2nd hand cancels the earlier aim (hands can't combine into a shot)",
              r['status'] != 'hit' and g.players[2].hp == 3, r['status'])
        await asyncio.sleep(server.TRIGGER_WINDOW_S + 0.1)   # let that pose expire
        r = await p1.frame(aim=2, ready=False)
        check('aim without the pose: no shot yet', r['status'] == 'pose_not_ready', r['status'])
        r = await p1.frame(ready=True)
        check('...the pose a frame later fires: they count together (detection flickers)',
              r['status'] == 'hit' and r['target_hp'] == 2, r['status'])
        await p1.frame(hands=1, ready=False)
        await asyncio.sleep(server.TRIGGER_WINDOW_S + 0.1)
        g.players[1].gun.update(True, time.monotonic() - ARM_DELAY_S)   # reloaded a while ago
        await p1.frame(aim=2, ready=False)
        await asyncio.sleep(server.TRIGGER_WINDOW_S + 0.1)
        r = await p1.frame(ready=True)
        check('...but not once they are more than 0.8 s apart', r['status'] != 'hit' and g.players[2].hp == 2, r['status'])
        g.players[1].gun.fired()                 # back to how the hit left it: empty

        print('\n10. Two shooters, one hit point left')
        g.players[2].hp = 1
        await p1.clap(0.9)
        await asyncio.sleep(CLAP_SETTLE + ARM)   # P1 reloaded; P3 never fired: both loaded
        r1, r3 = await asyncio.gather(p1.frame(aim=2), p3.frame(aim=2))
        statuses = sorted([r1['status'], r3['status']])
        check('both fire at once: exactly one gets the kill', statuses == ['hit', 'target_dead'], statuses)
        check('...one kill credited, one death', g.players[1].kills + g.players[3].kills == 2
              and g.players[2].deaths == 2, (g.players[1].kills, g.players[3].kills, g.players[2].deaths))

        print("\n11. Down players can't reload")
        p2obj = g.players[2]
        p2obj.gun.fired()                        # P2 is down, and their gun is empty
        r = await p2.frame(hands='clap')
        check('camera clap while down: gun stays empty', r['gun'] == 'reload', r['gun'])
        await p2.clap(0.9)
        await asyncio.sleep(CLAP_SETTLE)
        r = await p2.frame()
        check('mic clap while down: gun stays empty', r['gun'] == 'reload', r['gun'])
        p2obj.respawn()
        r = await p2.frame()
        check("respawn: gun loaded, and it isn't counted as a reload (no reload sound)",
              r['gun'] == 'ready' and r['reloads'] == 0, r)
        p2obj.gun.fired()
        r = await p2.frame(aim=1)                 # P2 aims at P1 with an empty gun...
        p2obj.alive = False                       # ...goes down...
        await p2.frame(hands=0)
        p2obj.respawn()                           # ...and is back within 0.8 s, gun loaded
        r = await p2.frame(hands=0)
        check("an aim from before going down doesn't fire on respawn",
              r['status'] != 'hit' and g.players[1].hp == 3, r['status'])

        print('\n12. Phones dropping off the Wi-Fi')
        p2obj.hp = 2
        await p2.close()
        await asyncio.sleep(0.2)
        left = host.of('player_left')
        check('host is told P2 disconnected, kept in the game',
              left and left[-1] == {'type': 'player_left', 'player_id': 2, 'kept': True}, left)
        async with s.get(ts.make_url('/api/state')) as resp:
            st = await resp.json()
        check("P2 is still listed (offline) and P2's deaths still count",
              st['players']['2']['connected'] is False and st['scores']['blue_deaths'] == 2, st['players'].get('2'))
        p2 = Phone('P2')
        await p2.connect(s, url)
        await p2.join(2, 'blue')
        r = await p2.frame()
        check('P2 rejoins exactly as they were: 2 HP (no free heal)', r['hp'] == 2 and r['alive'], r)
        check('...and is back online', g.players[2].connected)

        # A rejoin while the old connection still looks open (the server hasn't
        # noticed the Wi-Fi drop yet): the new connection takes over
        p1b = Phone('P1b')
        await p1b.connect(s, url)
        await p1b.join(1, 'red')
        r = await p1.frame(aim=2)
        check('the old P1 connection is ignored once P1 rejoined',
              r.get('error') == 'replaced_by_another_phone', r)
        reloads = g.players[1].gun.reloads
        await p1.clap(0.99)
        await asyncio.sleep(CLAP_SETTLE)
        check("...its claps are ignored too", g.players[1].gun.reloads == reloads)
        r = await p1b.frame()
        check('the new P1 connection plays on', r.get('type') == 'frame_result', r)
        await p1.close()
        await asyncio.sleep(0.1)
        check("the old connection closing doesn't take P1 offline", g.players[1].connected)
        p1 = p1b

        p4, p4b = Phone('P4'), Phone('P4b')
        await p4.connect(s, url)
        await p4.join(4, 'red')
        await p4b.connect(s, url)
        await p4b.join(4, 'blue')                # same ID, other team: a new player
        r = await p4.frame(aim=2)
        check('same ID re-joined on the other team: the old connection is ignored',
              r.get('error') == 'replaced_by_another_phone' and g.players[4].team == 'blue', r)
        await p4.close()

        print("\n13. Bad input doesn't break a phone's connection")
        for junk in ('null', '[1, 2]', 'not json', '{"type": "clap", "peak": "loud"}'):
            await p3.ws.send_str(junk)
        r = await p3.frame(corrupt=True)
        check("a corrupt camera frame gets a reply (the phone doesn't stall)",
              r.get('error') == 'jpeg_decode_failed', r)
        r = await p3.frame()
        check('...and the connection still works', r.get('type') == 'frame_result', r)

        print('\n14. Game over and a new game')
        await host.ws.send_json({'type': 'stop_game'})
        await asyncio.sleep(0.1)
        over = host.of('game_over')
        check('host presses Stop: final scores (red 2 kills, blue 0)',
              over and over[-1]['scores']['red_kills'] == 2 and over[-1]['scores']['blue_kills'] == 0
              and over[-1]['scores']['winner'] == 'red', over[-1:] )
        r = await p1.frame(aim=2)
        check('frames after Stop get "idle": no shooting', r.get('type') == 'idle', r)
        await host.ws.send_json({'type': 'start_game'})
        await asyncio.sleep(0.1)
        fresh = all(p.alive and p.hp == 3 and p.kills == 0 and p.deaths == 0
                    and p.gun.state(0) == 'ready' for p in g.players.values())
        check('new game: everyone alive, 3 HP, gun loaded, no kills', fresh,
              {i: (p.alive, p.hp, p.kills, p.gun.state(0)) for i, p in g.players.items()})
        r = await p1.frame(aim=2)
        check('...and the guns work in the new game', r['status'] == 'hit', r['status'])

        for p in (p1, p2, p3, p4b):
            await p.close()
        await host.ws.close()
    await ts.close()

    print('\n' + '=' * 55)
    print(f'  {passed} passed   {failed} failed')
    print('=' * 55)
    return failed


if __name__ == '__main__':
    sys.exit(asyncio.run(run()))
