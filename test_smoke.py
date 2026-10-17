# -*- coding: utf-8 -*-
"""
test_smoke.py
=============
Quick smoke tests for HackTheSkies.
Run BEFORE the full game to catch broken dependencies early.

Usage:
    python test_smoke.py

Each test prints PASS / FAIL / SKIP.
You do NOT need cameras or phones for these tests.
"""

import sys, time, asyncio, json, struct, io

# Force UTF-8 output so Unicode arrows and ANSI colours work on Windows.
if sys.stdout.encoding and sys.stdout.encoding.lower() != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
if sys.stderr.encoding and sys.stderr.encoding.lower() != 'utf-8':
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8', errors='replace')

print(f"Python {sys.version}\n")

PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
SKIP = "\033[93mSKIP\033[0m"

def check(label, fn):
    try:
        result = fn()
        tag = PASS if result is not False else FAIL
        print(f"  {tag}  {label}")
        return result is not False
    except ImportError as e:
        print(f"  {SKIP}  {label}  ({e})")
        return None
    except Exception as e:
        print(f"  {FAIL}  {label}  → {e}")
        return False

# ─────────────────────────────────────────────────────────────────────────────
print("=" * 55)
print("1. DEPENDENCIES")
print("=" * 55)

check("numpy",            lambda: __import__('numpy').__version__ and True)
check("cv2 (opencv)",     lambda: __import__('cv2').__version__ and True)
check("mediapipe",        lambda: __import__('mediapipe').__version__ and True)
check("pupil_apriltags",  lambda: __import__('pupil_apriltags') and True)
check("aiohttp",          lambda: __import__('aiohttp').__version__ and True)

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("2. HAND DETECTOR  (no camera needed)")
print("=" * 55)

try:
    import numpy as np
    from hand_detector import HandDetector

    det = HandDetector()

    # Blank frame → no hand → confidence should be 0
    blank = np.zeros((480, 640, 3), dtype=np.uint8)
    result = det.process(blank)
    check("blank frame → confidence=0.0", lambda: result['confidence'] == 0.0)
    check("blank frame → detected=False", lambda: result['detected'] == False)
    check("result has 'criteria' key",    lambda: 'criteria' in result)
    # criteria is empty when no hand is detected - that's correct
    check("blank frame → criteria empty", lambda: result['criteria'] == {})

    det.close()
    print("  HandDetector closed cleanly")
except Exception as e:
    print(f"  {FAIL}  HandDetector failed to load: {e}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("3. TAG DETECTOR  (no camera needed)")
print("=" * 55)

try:
    import numpy as np
    from tag_detector import TagDetector, _tag_role

    det = TagDetector()

    # Blank frame → no tags
    blank_gray = np.zeros((480, 640), dtype=np.uint8)
    tags = det.detect(blank_gray)
    check("blank frame → no tags",        lambda: tags == [])
    check("best_centered → None",         lambda: det.best_centered_tag(blank_gray, 640, 480) is None)

    # Role mapping
    check("tag 7   → 'player'",   lambda: _tag_role(7)   == 'player')
    check("tag 100 → 'base'",     lambda: _tag_role(100) == 'base')
    check("tag 200 → 'flag'",     lambda: _tag_role(200) == 'flag')
    check("tag 0   → 'unknown'",  lambda: _tag_role(0)   == 'unknown')

    # Round trip: a tag printed by gen_tags is read back by the game's detector
    import cv2
    from gen_tags import make_tag_image
    from tag_detector import TAG_FAMILY
    printed = make_tag_image(7, cell_px=12)
    scene   = np.full((480, 640), 128, np.uint8)
    scene[100:100+printed.shape[0], 200:200+printed.shape[1]] = printed
    found = det.detect(scene)
    check(f"gen_tags {TAG_FAMILY} tag 7 → detected as player 7",
          lambda: [t['id'] for t in found] == [7] and found[0]['role'] == 'player')

    # Finger aiming: knuckle at (100,300) -> tip at (150,300), i.e. pointing right
    from tag_detector import pick_aimed_tag
    def fake_tag(tid, cx, cy, r=40):
        return {'id': tid, 'center': (cx, cy), 'role': 'player',
                'corners': [[cx-r, cy-r], [cx+r, cy-r], [cx+r, cy+r], [cx-r, cy+r]]}
    right, above, behind = fake_tag(1, 500, 310), fake_tag(2, 500, 50), fake_tag(3, 10, 300)
    k, t = (100, 300), (150, 300)
    check("aim: tag on finger ray is picked",   lambda: pick_aimed_tag([right, above], k, t)['id'] == 1)
    check("aim: tag off the ray is ignored",    lambda: pick_aimed_tag([above], k, t) is None)
    check("aim: tag behind fingertip ignored",  lambda: pick_aimed_tag([behind], k, t) is None)
    check("aim: no tags → None",                lambda: pick_aimed_tag([], k, t) is None)
    # Far-away tag: tiny (radius 6px) and 1000px down the ray, 60px off-line.
    # 60px is 10 tag-radii, but only ~3.4 deg — within the angular tolerance.
    check("aim: far tiny tag hit within a few degrees",
          lambda: pick_aimed_tag([fake_tag(5, 1150, 360, r=6)], k, t)['id'] == 5)
    check("aim: far tiny tag missed when well off-angle",
          lambda: pick_aimed_tag([fake_tag(5, 1150, 450, r=6)], k, t) is None)
    check("aim: finger at camera → tag under tip",
          lambda: pick_aimed_tag([fake_tag(4, 155, 305)], (150, 300), (152, 301))['id'] == 4)

except Exception as e:
    print(f"  {FAIL}  TagDetector failed to load: {e}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("4. GAME STATE")
print("=" * 55)

try:
    from game_state import GameState

    async def test_game_state():
        g = GameState()

        # Add players
        p1 = await g.add_player(1, 'red',  ws=None)
        p2 = await g.add_player(2, 'blue', ws=None)
        check("player 1 registered",     lambda: 1 in g.players)
        check("player 2 registered",     lambda: 2 in g.players)
        check("player 1 alive",          lambda: g.players[1].alive)

        # Shot: p1 shoots p2
        await g.start_game()
        event, why = await g.process_shot(shooter_id=1, target_id=2)
        check("shot event returned",     lambda: event is not None and why == 'hit')
        check("event has type",          lambda: event['type'] == 'shot_event')
        check("1 hit: target alive, 2 HP", lambda: g.players[2].alive and g.players[2].hp == 2
                                                    and not event['killed'])

        # Cooldown: same shooter fires again immediately → should be rejected
        event2, why2 = await g.process_shot(shooter_id=1, target_id=2)
        check("cooldown prevents re-shot", lambda: event2 is None and why2 == 'cooldown')

        # 3 hits to kill
        g.players[1].last_shot_at = 0
        await g.process_shot(shooter_id=1, target_id=2)
        check("2 hits: target alive, 1 HP", lambda: g.players[2].alive and g.players[2].hp == 1)
        g.players[1].last_shot_at = 0
        event_k, _ = await g.process_shot(shooter_id=1, target_id=2)
        check("3 hits: target is dead",  lambda: not g.players[2].alive and event_k['killed'])
        check("shooter still alive",     lambda: g.players[1].alive)
        check("kill credited once",      lambda: g.players[1].kills == 1)
        g.players[2].respawn()
        check("respawn restores HP",     lambda: g.players[2].hp == 3)
        g.players[2].alive = False   # back to dead (no extra death) for the checks below

        # Friendly fire off: p1 (red) can't shoot another red player
        await g.add_player(3, 'red', ws=None)
        g.players[1].last_shot_at = 0
        event3, why3 = await g.process_shot(shooter_id=1, target_id=3)
        check("no friendly fire",        lambda: event3 is None and why3 == 'same_team')

        # Already-dead target is rejected
        event4, why4 = await g.process_shot(shooter_id=1, target_id=2)
        check("can't shoot a dead player", lambda: event4 is None and why4 == 'target_dead')

        # Unregistered target
        event5, why5 = await g.process_shot(shooter_id=1, target_id=42)
        check("unknown target rejected", lambda: event5 is None and why5 == 'target_not_in_game')

        # Reconnect: P2 re-registers on a new socket; the old socket closing late must not remove it
        old_ws, new_ws = object(), object()
        await g.add_player(9, 'blue', ws=old_ws)
        await g.add_player(9, 'blue', ws=new_ws)
        removed = await g.remove_player(9, old_ws)
        check("stale socket close keeps reconnected player", lambda: not removed and 9 in g.players)
        await g.remove_player(9, new_ws)

        # Scores (kills-based)
        scores = g.scores()
        check("red has 1 kill",          lambda: scores['red_kills'] == 1)
        check("blue has 1 death",        lambda: scores['blue_deaths'] == 1)
        check("red is winning",          lambda: scores['winner'] == 'red')

    asyncio.run(test_game_state())

except Exception as e:
    print(f"  {FAIL}  GameState error: {e}")
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("5. JPEG DECODE  (frame pipeline stub)")
print("=" * 55)

try:
    import cv2, numpy as np

    # Create a tiny synthetic JPEG and round-trip it
    img = np.random.randint(0, 255, (240, 320, 3), dtype=np.uint8)
    ok, buf = cv2.imencode('.jpg', img, [cv2.IMWRITE_JPEG_QUALITY, 70])
    check("cv2 encodes JPEG",  lambda: ok)

    decoded = cv2.imdecode(np.frombuffer(buf.tobytes(), np.uint8), cv2.IMREAD_COLOR)
    check("cv2 decodes JPEG",  lambda: decoded is not None)
    check("shape preserved",   lambda: decoded.shape == (240, 320, 3))

    rgb  = cv2.cvtColor(decoded, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(decoded, cv2.COLOR_BGR2GRAY)
    check("BGR→RGB  works",    lambda: rgb.shape  == (240, 320, 3))
    check("BGR→GRAY works",    lambda: gray.shape == (240, 320))

except Exception as e:
    print(f"  {FAIL}  JPEG pipeline error: {e}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("6. SERVER IMPORT")
print("=" * 55)

try:
    # Just test that server.py imports without error (don't start it)
    import importlib.util, pathlib
    spec = importlib.util.spec_from_file_location("server", "server.py")
    mod  = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("server.py imports OK",     lambda: hasattr(mod, 'build_app'))
    check("build_app() callable",     lambda: callable(mod.build_app))
    app = mod.build_app()
    check("build_app() returns app",  lambda: app is not None)
except Exception as e:
    print(f"  {FAIL}  server.py import error: {e}")
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("DONE — fix any FAIL lines before running the game")
print("=" * 55)

