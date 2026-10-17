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

except Exception as e:
    print(f"  {FAIL}  TagDetector failed to load: {e}")

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("4. GAME STATE")
print("=" * 55)

try:
    from game_state import GameState, Player, Flag, RESPAWN_DURATION

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
        event = await g.process_shot(shooter_id=1, target_id=2)
        check("shot event returned",     lambda: event is not None)
        check("target is dead",          lambda: not g.players[2].alive)
        check("shooter still alive",     lambda: g.players[1].alive)
        check("event has type",          lambda: event['type'] == 'shot_event')

        # Cooldown: same shooter fires again immediately → should be rejected
        event2 = await g.process_shot(shooter_id=1, target_id=2)
        check("cooldown prevents re-shot", lambda: event2 is None)

        # Friendly fire off: p1 (red) can't shoot another red player
        p3 = await g.add_player(3, 'red', ws=None)
        event3 = await g.process_shot(shooter_id=1, target_id=3)
        check("no friendly fire",        lambda: event3 is None)

        # Scores
        await g.add_flag(0, 0.5, 0.5)
        await g.capture_flag(0, 'red')
        time.sleep(0.1)
        scores = g.scores()
        check("red accrues flag time",   lambda: scores['red'] > 0)
        check("blue has 0",              lambda: scores['blue'] == 0.0)

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

