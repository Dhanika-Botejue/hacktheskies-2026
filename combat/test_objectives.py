# -*- coding: utf-8 -*-
"""
test_objectives.py
==================
The match: bases to respawn at and the flag (objectives.py).  First the rules,
driven with made-up positions and time; then end to end through the real server
with real WebSocket phones and a host, positions fed in exactly as the game
server sends them (tracking.PositionFeed.on_message).

    python test_objectives.py
"""
from __future__ import annotations

import asyncio
import sys
import time

import aiohttp
from aiohttp.test_utils import TestServer

import objectives
import server
from game_state import GameState, Player
from objectives import Objectives, clamp_win_s
from test_gun import Host, Phone, fake_process_frame
from tracking import tracked_positions, ws_url

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
passed = failed = 0


def check(label: str, cond, detail='') -> None:
    global passed, failed
    if cond:
        passed += 1
        print(f'  {PASS}  {label}')
    else:
        failed += 1
        print(f'  {FAIL}  {label}  {detail}')


RED_BASE  = {'kind': 'base', 'team': 'red',  'x': 0.0,  'y': 0.0, 'r': 2.0}
BLUE_BASE = {'kind': 'base', 'team': 'blue', 'x': 20.0, 'y': 0.0, 'r': 2.0}
FLAG      = {'kind': 'flag', 'x': 10.0, 'y': 0.0, 'r': 3.0}
IN_RED, IN_BLUE, IN_FLAG, AWAY = (0.5, 0.0), (20.0, 1.0), (10.0, 1.0), (10.0, 10.0)


def rules(points=(RED_BASE, BLUE_BASE, FLAG), win_s=60) -> Objectives:
    o = Objectives()
    o.set_points(list(points))
    o.reset(win_s)
    return o


def team(**ids) -> dict:
    """team(red=[1, 3], blue=[2]) -> {player_id: Player}"""
    return {pid: Player(player_id=pid, team=t) for t, pids in ids.items() for pid in pids}


def kinds(events) -> list:
    return [e['type'] for e in events]


# ── The rules ─────────────────────────────────────────────────────────────────

def test_rules():
    C = objectives.CAPTURE_S

    print('\n1. Reading the map')
    o = rules((RED_BASE, BLUE_BASE, FLAG, {'kind': 'base', 'team': 'green', 'x': 1, 'y': 1, 'r': 1},
               {'kind': 'flag', 'x': 'bad', 'y': 0, 'r': 1}, {'name': 'old key position', 'x': 1, 'y': 1, 'r': 1}))
    check('both bases and the flag are read; bad ones ignored',
          set(o.bases) == {'red', 'blue'} and o.flag and o.flag.x == 10, (o.bases, o.flag))
    check('win time: clamped, default when it makes no sense',
          clamp_win_s(1) == objectives.WIN_S_MIN and clamp_win_s(1e9) == objectives.WIN_S_MAX
          and clamp_win_s('x') == objectives.DEFAULT_WIN_S and clamp_win_s(float('nan')) == objectives.DEFAULT_WIN_S
          and clamp_win_s('90') == 90)

    print('\n2. Taking the flag')
    o, ps = rules(), team(red=[1, 3], blue=[2, 4])
    at = {1: IN_FLAG, 2: AWAY, 3: AWAY, 4: AWAY}
    check('red alone in the circle: not theirs straight away',
          not o.update(ps, at, 0.0, 0.0) and o.owner is None and o.challenger == 'red')
    check(f'...nor just before {C:.0f} s', not o.update(ps, at, C - 0.1, 0.0) and o.owner is None)
    ev = o.update(ps, at, C, 0.0)
    check(f'...theirs after {C:.0f} s', kinds(ev) == ['flag_taken'] and ev[0]['team'] == 'red'
          and ev[0]['from'] is None and o.owner == 'red', ev)
    check('no clock before anyone owned it', o.held == {'red': 0.0, 'blue': 0.0}, o.held)

    print('\n3. Holding it')
    at[1] = AWAY
    o.update(ps, at, C + 10, 0.0)
    check('everyone leaves the circle: red keeps the flag, its clock runs',
          o.owner == 'red' and abs(o.held['red'] - 10) < 1e-6, (o.owner, o.held))
    at.update({1: IN_FLAG, 2: IN_FLAG})
    o.update(ps, at, C + 11, 0.0)
    o.update(ps, at, C + 11 + 2 * C, 0.0)
    check('one red, one blue in the circle (tie): still red, nobody taking it',
          o.owner == 'red' and o.challenger is None and o.inside == {'red': 1, 'blue': 1}, (o.owner, o.inside))
    t = C + 11 + 2 * C
    at[4] = IN_FLAG
    o.update(ps, at, t, 0.0)
    check('blue outnumbers red 2 to 1: blue is taking it', o.challenger == 'blue' and o.owner == 'red')
    at[4] = AWAY
    o.update(ps, at, t + 1, 0.0)
    at[4] = IN_FLAG
    o.update(ps, at, t + 2, 0.0)
    o.update(ps, at, t + 2 + C - 0.1, 0.0)
    check("a blue player stepping out restarts blue's take", o.owner == 'red', o.owner)
    ev = o.update(ps, at, t + 2 + C, 0.0)
    check('...then blue holds the majority long enough: theirs',
          kinds(ev) == ['flag_taken'] and ev[0] == {'type': 'flag_taken', 'team': 'blue', 'from': 'red'}, ev)
    red_before = o.held['red']
    o.update(ps, at, t + 2 + C + 5, 0.0)
    check("red's clock stops but keeps its time; blue's runs",
          abs(o.held['red'] - red_before) < 1e-6 and abs(o.held['blue'] - 5) < 1e-6, o.held)

    print('\n4. Who counts')
    o, ps = rules(), team(red=[1], blue=[2, 4])
    at = {1: IN_FLAG, 2: IN_FLAG, 4: IN_FLAG}
    ps[4].die()
    for s in (0.0, C, 2 * C):
        o.update(ps, at, s, 0.0)
    check('a downed player in the circle does not count (1 v 1: nobody takes it)',
          o.owner is None and o.inside == {'red': 1, 'blue': 1}, (o.owner, o.inside))
    o, ps = rules(), team(red=[1], blue=[2, 4])
    at = {1: IN_FLAG, 2: IN_FLAG}                   # 4 isn't tracked (not placed)
    for s in (0.0, C, 2 * C):
        o.update(ps, at, s, 0.0)
    check('an untracked player does not count either', o.owner is None and o.inside['blue'] == 1)

    print('\n5. Winning')
    o, ps = rules(win_s=10), team(red=[1], blue=[2])
    at = {1: IN_FLAG, 2: AWAY}
    o.update(ps, at, 0.0, 0.0)
    o.update(ps, at, C, 0.0)
    check('not won before the flag time is up', 'win' not in kinds(o.update(ps, at, C + 9.9, 0.0)))
    ev = o.update(ps, at, C + 10.5, 0.0)
    check('won when the owner reaches the flag time', {'type': 'win', 'team': 'red'} in ev, ev)

    print('\n6. Respawning at your base')
    o, ps = rules(), team(red=[1], blue=[2])
    ps[1].die()
    wall = ps[1].respawn_at + 60                     # long after the timer would have run out
    o.update(ps, {1: AWAY, 2: AWAY}, 0.0, wall)
    check('downed and tracked: waits to reach the base, however long it takes',
          not ps[1].alive and ps[1].respawn_mode == 'base')
    o.update(ps, {1: IN_BLUE, 2: AWAY}, 1.0, wall)
    check("the other team's base does nothing", not ps[1].alive)
    ev = o.update(ps, {1: IN_RED, 2: AWAY}, 2.0, wall)
    check('stepping into your own base: back in, full HP, gun loaded',
          ev == [{'type': 'respawned', 'player_id': 1, 'how': 'base'}] and ps[1].alive
          and ps[1].hp == 3 and ps[1].gun.state(0) == 'ready', ev)
    ps[2].die()
    ev = o.update(ps, {1: IN_RED, 2: IN_BLUE}, 3.0, 0.0)
    check('downed inside your own base: back in at once', ps[2].alive and kinds(ev) == ['respawned'], ev)

    print('\n7. When walking back to a base is impossible: the timer')
    o, ps = rules(), team(red=[1], blue=[2])
    ps[1].die()
    o.update(ps, {}, 0.0, ps[1].respawn_at - 0.1)
    check('not tracked: respawn_mode timer, still down before the time',
          ps[1].respawn_mode == 'timer' and not ps[1].alive)
    ev = o.update(ps, {}, 1.0, ps[1].respawn_at)
    check('...back in when it runs out', ps[1].alive and ev[0]['how'] == 'timer', ev)
    o, ps = rules((RED_BASE, FLAG)), team(red=[1], blue=[2])
    ps[2].die()
    o.update(ps, {1: AWAY, 2: IN_BLUE}, 0.0, ps[2].respawn_at)
    check('a team with no base: on the timer too', ps[2].alive and ps[2].respawn_mode == 'timer')
    o, ps = rules(), team(red=[1], blue=[2])
    ps[1].die()
    wall = ps[1].respawn_at + 60
    o.update(ps, {1: AWAY, 2: AWAY}, 0.0, wall)      # tracked, long past the timer
    o.update(ps, {2: AWAY}, 1.0, wall + 2)           # then tracking drops out for a moment
    check('tracking lost for a moment: still down, not back where they stand',
          not ps[1].alive and ps[1].respawn_mode == 'timer')
    ev = o.update(ps, {2: AWAY}, 2.0, wall + objectives.RESPAWN_TIMER_S)
    check('...back in once the timer has run while untracked', ps[1].alive and ev[0]['how'] == 'timer', ev)

    print('\n7b. Down: nothing counts')
    o, ps = rules(), team(red=[1], blue=[2])
    ps[1].die()
    o.update(ps, {1: IN_FLAG, 2: AWAY}, 0.0, 0.0)
    o.update(ps, {1: IN_FLAG, 2: AWAY}, objectives.CAPTURE_S + 1, 0.0)
    check('a downed player in the flag circle neither takes it nor counts',
          o.owner is None and o.inside['red'] == 0)

    print('\n8. No flag on the map')
    o, ps = rules((RED_BASE, BLUE_BASE)), team(red=[1], blue=[2])
    for s in (0.0, 10.0, 100.0):
        ev = o.update(ps, {1: IN_FLAG, 2: AWAY}, s, 0.0)
    check('nobody owns anything, no clocks, nobody leads',
          not ev and o.owner is None and o.held == {'red': 0, 'blue': 0} and o.leading() is None)


async def test_game_state():
    C = objectives.CAPTURE_S
    print('\n9. The match (game_state.GameState)')
    g = GameState()
    g.objectives.set_points([RED_BASE, BLUE_BASE, FLAG])
    for pid, t in ((1, 'red'), (2, 'blue')):
        await g.add_player(pid, t, None)
    at = {1: IN_FLAG, 2: AWAY}
    check('no game running: nothing happens', await g.tick(at, 0.0) == [] and g.objectives.owner is None)
    check('...but who is tracked is known', g.tracked == {1, 2})
    await g.start_game(win_s=12)
    check('start: the flag time to win is the one asked for', g.objectives.win_s == 12)
    await g.tick(at, 0.0)
    await g.tick(at, C)
    g.players[2].kills = 5
    await g.tick(at, C + 1)
    s = g.scores()
    check('flag time beats kills while the game runs', s['winner'] == 'red' and s['reason'] is None, s)
    ev = await g.tick(at, C + 12)
    over = [e for e in ev if e['type'] == 'game_over']
    check("red's flag time is up: game over, red wins on the flag",
          over and over[0]['team'] == 'red' and over[0]['scores']['winner'] == 'red'
          and over[0]['scores']['reason'] == 'flag' and not g.game_active, ev)
    check('the result stays readable after the game', g.scores()['red_flag_s'] >= 12 and g.objectives.owner == 'red')
    at[2] = IN_FLAG
    await g.start_game(win_s=12)
    await g.tick(at, 100.0)
    await g.tick({1: AWAY, 2: IN_FLAG}, 101.0)
    check('(blue is taking the flag...)', g.objectives.challenger == 'blue')
    await g.stop_game()
    o = g.objectives.to_dict(102.0)
    check('...when the game ends nobody is still taking it',
          o['flag']['challenger'] is None and o['flag']['progress'] == 0, o['flag'])
    at[2] = AWAY
    await g.start_game()
    check('a new game: nobody owns the flag, clocks at zero, default flag time',
          g.objectives.owner is None and g.objectives.held == {'red': 0, 'blue': 0}
          and g.objectives.win_s == objectives.DEFAULT_WIN_S and g.end_reason is None)
    g.players[1].hp = 0
    g.players[1].die()
    check('down: the gun does nothing', await g.process_shot(1, 2) == (None, 'dead'))
    check('down: nobody can shoot you again', await g.process_shot(2, 1) == (None, 'target_dead'))
    await g.add_player(1, 'blue', None)
    check('down, re-joining on the other team: still down',
          not g.players[1].alive and g.players[1].team == 'blue' and g.players[1].hp == 0)
    await g.add_player(1, 'red', None)
    s = await g.stop_game()
    check('ended by the host: reason host', s['reason'] == 'host', s)
    g.objectives.set_points([RED_BASE, BLUE_BASE])
    g.players[1].kills, g.players[2].kills = 1, 2
    check('no flag on the map: kills decide', g.scores()['winner'] == 'blue' and not g.scores()['flag'])


def test_feed():
    print('\n10. Reading the game server')
    check('game server address -> its WebSocket',
          ws_url('https://127.0.0.1:8443') == 'wss://127.0.0.1:8443/ws'
          and ws_url('http://host:8443/') == 'ws://host:8443/ws' and ws_url('host:8443') == 'wss://host:8443/ws')
    got = tracked_positions([
        {'tag': 3, 'connected': True, 'calibrated': True, 'x': 1.5, 'y': 2},
        {'tag': 4, 'connected': False, 'calibrated': True, 'x': 1, 'y': 1},   # phone gone
        {'tag': 5, 'connected': True, 'calibrated': False},                   # not placed yet
        {'tag': None, 'connected': True, 'calibrated': True, 'x': 1, 'y': 1},
        'junk',
    ])
    check('only placed, connected players with a tag', got == {3: (1.5, 2.0)}, got)


# ── End to end ────────────────────────────────────────────────────────────────

async def test_server():
    print('\n11. End to end: phones, the host and positions from the game server')
    objectives.CAPTURE_S, objectives.WIN_S_MIN = 0.4, 1.0     # a quick match
    server._process_frame = fake_process_frame
    server.feed.url = None                                     # positions are fed in below
    g = server.game
    feed = server.feed
    feed.on_message({'t': 'map', 'map': {'points': [RED_BASE, BLUE_BASE, FLAG]}}, time.monotonic())
    check("the game server's map sets the objectives", set(g.objectives.bases) == {'red', 'blue'} and g.objectives.flag)

    where = {1: AWAY, 2: AWAY}

    async def game_server():
        """Positions ~10 times a second, as the game server's state messages."""
        while True:
            feed.on_message({'t': 'state', 'players': [
                {'tag': pid, 'connected': True, 'calibrated': True, 'x': x, 'y': y}
                for pid, (x, y) in where.items()]}, time.monotonic())
            await asyncio.sleep(0.1)

    positions = asyncio.create_task(game_server())
    ts = TestServer(server.build_app())
    await ts.start_server()
    settle = lambda: asyncio.sleep(2 * server.STATE_BROADCAST_INTERVAL + 0.1)
    async with aiohttp.ClientSession() as s:
        host = Host()
        await host.connect(s, str(ts.make_url('/ws/host')))
        p1, p2 = Phone('P1'), Phone('P2')
        for p, pid, t in ((p1, 1, 'red'), (p2, 2, 'blue')):
            await p.connect(s, str(ts.make_url('/ws/phone')))
            await p.join(pid, t)
        await host.ws.send_json({'type': 'start_game', 'win_s': 3})
        await settle()
        check('host starts the match with a flag time', g.game_active and host.of('game_started')
              and host.of('game_started')[-1]['win_s'] == 3, host.of('game_started'))
        st = host.of('state_update')[-1]
        check('host sees the bases, the flag, the game server link and who is tracked',
              st['objectives']['flag']['r'] == 3 and set(st['objectives']['bases']) == {'red', 'blue'}
              and st['objectives']['tracking'] is False and st['players']['2']['tracked'] is True, st['objectives'])

        g.players[2].hp = 1
        r = await p1.frame(aim=2)
        await asyncio.sleep(0.1)
        down = p2.of('shot_event')
        check('P2 is shot down: told to walk back to base',
              r['shot_killed'] and down and down[-1]['respawn_mode'] == 'base', down)
        r = await p2.frame()
        check("P2's next frame: down, respawn_mode base", r['alive'] is False and r['respawn_mode'] == 'base', r)
        await settle()
        check('...and stays down while away from the base', not g.players[2].alive)
        where[2] = IN_BLUE
        await settle()
        check('P2 walks into the blue base: back in (host told)',
              {'type': 'respawned', 'player_id': 2, 'how': 'base'} in host.got, host.of('respawned'))
        r = await p2.frame()
        check("P2's phone sees it", r['alive'] is True and r['hp'] == 3, r)

        where[2] = AWAY
        await settle()
        g.players[2].die()
        await host.ws.send_json({'type': 'respawn_player', 'player_id': 2})
        await settle()
        check('host brings a stuck player back by hand',
              g.players[2].alive and {'type': 'respawned', 'player_id': 2, 'how': 'host'} in host.got)

        where[1] = IN_FLAG
        await asyncio.sleep(objectives.CAPTURE_S + 0.5)
        check('P1 holds the flag circle: red takes the flag',
              any(e['team'] == 'red' for e in host.of('flag_taken')), host.of('flag_taken'))
        await settle()
        r = await p1.frame()
        check("P1's phone: red owns the flag, red's clock running",
              r['flag']['owner'] == 'red' and r['flag']['win_s'] == 3 and r['flag']['held']['red'] > 0, r['flag'])
        await asyncio.sleep(3.2)
        over = host.of('game_over')
        check("red's 3 s of flag time: game over, red wins",
              over and over[-1]['scores']['winner'] == 'red' and over[-1]['scores']['reason'] == 'flag'
              and not g.game_active, over)
        r = await p2.frame()
        check('the phones hear the result', r['type'] == 'idle' and r['scores']['winner'] == 'red', r)

        for p in (p1, p2):
            await p.close()
        await host.ws.close()
    positions.cancel()
    await ts.close()


async def run() -> int:
    test_rules()
    await test_game_state()
    test_feed()
    await test_server()
    print('\n' + '=' * 55)
    print(f'  {passed} passed   {failed} failed')
    print('=' * 55)
    return failed


if __name__ == '__main__':
    sys.exit(asyncio.run(run()))
