# -*- coding: utf-8 -*-
"""
test_api.py
===========
End-to-end server test — no camera, no tags, no phone needed.
Spins up two fake players over WebSocket, runs through the full game loop,
and verifies every response.

Requires the server to already be running:
    python server.py      (in one terminal)
    python test_api.py    (in another terminal)
"""

import asyncio
import sys
import aiohttp

BASE     = 'http://localhost:8765'
WS_PHONE = 'ws://localhost:8765/ws/phone'

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'

passed = 0
failed = 0

def ok(label):
    global passed
    passed += 1
    print(f'  {PASS}  {label}')

def fail(label, detail=''):
    global failed
    failed += 1
    print(f'  {FAIL}  {label}  {detail}')

async def run():
    async with aiohttp.ClientSession() as s:

        # ── 1. HTTP pages ────────────────────────────────────────────────────
        print('\n1. HTTP pages')
        try:
            async with s.get(f'{BASE}/') as r:
                (ok if r.status == 200 else fail)(f'GET /  → {r.status}')
            async with s.get(f'{BASE}/phone') as r:
                (ok if r.status == 200 else fail)(f'GET /phone  → {r.status}')
            async with s.get(f'{BASE}/api/network-info') as r:
                j = await r.json()
                (ok if 'ip' in j else fail)(f'GET /api/network-info  → {j}')
        except Exception as e:
            fail('HTTP pages', str(e))

        # ── 2. Add flags ──────────────────────────────────────────────────────
        print('\n2. Add flags via REST')
        try:
            for i, (x, y) in enumerate([(0.3, 0.4), (0.7, 0.6)]):
                async with s.post(f'{BASE}/api/flag',
                                  json={'flag_id': 200 + i, 'x': x, 'y': y}) as r:
                    j = await r.json()
                    (ok if j.get('ok') else fail)(f'POST /api/flag  flag_id={200+i}')
        except Exception as e:
            fail('Add flags', str(e))

        # ── 3. Register two players ───────────────────────────────────────────
        print('\n3. WebSocket — player registration')
        ws1 = ws2 = None
        try:
            ws1 = await s.ws_connect(WS_PHONE)
            ws2 = await s.ws_connect(WS_PHONE)

            await ws1.send_json({'type': 'register', 'player_id': 1, 'team': 'red'})
            r1 = await ws1.receive_json(timeout=4)
            (ok if r1.get('type') == 'registered' and r1.get('player_id') == 1
               else fail)(f'Player 1 registered  → {r1}')

            await ws2.send_json({'type': 'register', 'player_id': 2, 'team': 'blue'})
            r2 = await ws2.receive_json(timeout=4)
            (ok if r2.get('type') == 'registered' and r2.get('player_id') == 2
               else fail)(f'Player 2 registered  → {r2}')

            async with s.get(f'{BASE}/api/state') as r:
                state = await r.json()
                both = '1' in state['players'] and '2' in state['players']
                (ok if both else fail)('Both players appear in /api/state')
        except Exception as e:
            fail('Registration', str(e))

        await asyncio.sleep(0.3)   # let server process both registrations

        # ── 4. Start game ──────────────────────────────────────────────────────
        print('\n4. Start game')
        try:
            async with s.post(f'{BASE}/api/start') as r:
                j = await r.json()
                (ok if j.get('ok') else fail)(f'POST /api/start  → {j}')

            await asyncio.sleep(0.1)
            async with s.get(f'{BASE}/api/state') as r:
                state = await r.json()
                (ok if state.get('game_active') else fail)('game_active == True')
        except Exception as e:
            fail('Start game', str(e))

        # ── 5. Fake JPEG frame — no detection expected (blank frame) ──────────
        print('\n5. Send blank JPEG frame (tests frame pipeline, expect no detection)')
        try:
            import cv2, numpy as np
            blank = np.zeros((240, 320, 3), dtype=np.uint8)
            _, buf = cv2.imencode('.jpg', blank, [cv2.IMWRITE_JPEG_QUALITY, 70])
            jpeg_bytes = buf.tobytes()

            if ws1 and not ws1.closed:
                await ws1.send_bytes(jpeg_bytes)
                msg = await ws1.receive_json(timeout=10)
                is_frame_result = msg.get('type') == 'frame_result'
                (ok if is_frame_result else fail)(f'Got frame_result response  → type={msg.get("type")}')

                has_fields = all(k in msg for k in
                    ['pose', 'armed', 'aiming_role', 'capture_progress',
                     'alive', 'scores'])
                (ok if has_fields else fail)(
                    f'frame_result has expected fields  → {list(msg.keys())}')

                no_tag = msg.get('aiming_role') is None
                (ok if no_tag else fail)(
                    f'Blank frame → aiming_role=None  (got {msg.get("aiming_role")})')

                pose_zero = msg.get('pose', 1.0) < 0.05
                (ok if pose_zero else fail)(
                    f'Blank frame → pose~0  (got {msg.get("pose")})')
        except Exception as e:
            fail('Blank frame pipeline', str(e))

        # ── 6. Flag capture via direct state call ─────────────────────────────
        print('\n6. Flag capture (direct)')
        try:
            from game_state import GameState   # import the module directly
            # We test game_state in isolation (already tested in test_smoke.py)
            # Here just verify the REST state reflects flag data
            async with s.get(f'{BASE}/api/state') as r:
                state = await r.json()
                flags = state.get('flags', {})
                (ok if len(flags) == 2 else fail)(
                    f'Two flags in state  (got {len(flags)})')
                no_owner = all(f.get('owner_team') is None for f in flags.values())
                (ok if no_owner else fail)('Flags have no owner yet (game just started)')
        except Exception as e:
            fail('Flag state check', str(e))

        # ── 7. Stop game ───────────────────────────────────────────────────────
        print('\n7. Stop game')
        try:
            await asyncio.sleep(0.3)   # let flag time tick
            async with s.post(f'{BASE}/api/stop') as r:
                j = await r.json()
                (ok if j.get('ok') else fail)('POST /api/stop')
                scores = j.get('scores', {})
                has_scores = 'red' in scores and 'blue' in scores
                (ok if has_scores else fail)(f'Scores in response  → {scores}')

            async with s.get(f'{BASE}/api/state') as r:
                state = await r.json()
                (ok if not state.get('game_active') else fail)('game_active == False')
        except Exception as e:
            fail('Stop game', str(e))

        # ── 8. Cleanup ─────────────────────────────────────────────────────────
        print('\n8. Cleanup')
        for ws in [ws1, ws2]:
            if ws and not ws.closed:
                await ws.close()
        ok('WebSocket connections closed')

    # ── Summary ────────────────────────────────────────────────────────────────
    print()
    print('=' * 48)
    print(f'  {passed} passed   {failed} failed')
    if failed == 0:
        print('  Server is working correctly!')
    else:
        print('  Fix the FAIL lines above.')
    print('=' * 48)
    return failed

if __name__ == '__main__':
    try:
        failures = asyncio.run(run())
        sys.exit(failures)
    except aiohttp.ClientConnectorError:
        print('\nERROR: Could not connect to server.')
        print('Make sure the server is running first:')
        print('    python server.py')
        sys.exit(1)
