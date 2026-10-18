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
          lambda: pick_aimed_tag([fake_tag(5, 1150, 600, r=6)], k, t) is None)   # ~17 deg off

    from hand_detector import ClapTracker, own_palms, shooting_hand
    def fake_hand(cx, cy, s=0.12):  # normalised coords; palm length (wrist -> middle knuckle) = s
        lms = [(cx, cy)] * 21
        lms[0], lms[9] = (cx, cy + s / 2), (cx, cy - s / 2)
        return {'landmarks': lms}
    L, R, far = fake_hand(.3, .5), fake_hand(.7, .5), fake_hand(.9, .4, s=.02)
    near_hand = fake_hand(.2, .5, s=.2)   # biggest in frame = closest to the camera
    check("hands: both your hands in view = no shooting (one-handed only)",
          lambda: shooting_hand([L, R], 1000, 1000) is None)
    check("hands: your hand shoots past a smaller, further-off hand",
          lambda: shooting_hand([fake_hand(.7, .5, s=.08), near_hand], 1000, 1000) is near_hand)
    check("hands: the only hand shoots",       lambda: shooting_hand([L], 1000, 1000) is L)
    check("hands: none → no shooter",          lambda: shooting_hand([], 1000, 1000) is None)
    check("hands: a far-away (other player's) hand is ignored",
          lambda: shooting_hand([L, far], 1000, 1000) is L)
    def claps(*frames, dt=0.1):   # each frame = list of hands; True if any frame reads as a clap
        tracker = ClapTracker()
        return any(tracker.update(own_palms(f, 1000, 1000), i * dt) for i, f in enumerate(frames))
    near_l, near_r, mid = fake_hand(.3, .5), fake_hand(.7, .5), fake_hand(.5, .5)
    check("clap: palms together in one frame", lambda: claps([fake_hand(.45, .5), fake_hand(.55, .5)]))
    check("clap: hands approach, then merge into one", lambda: claps([near_l, near_r], [mid]))
    check("clap: hands approach, then blur out",       lambda: claps([near_l, near_r], []))
    check("clap: hands apart = no clap",       lambda: not claps([fake_hand(.1, .5), fake_hand(.9, .5)]))
    check("clap: one hand only = no clap",     lambda: not claps([L], [L]))
    check("clap: one hand leaving the frame isn't a clap", lambda: not claps([near_l, near_r], [near_l]))
    check("clap: far-apart hands vanishing isn't a clap",  lambda: not claps([fake_hand(.1, .5), fake_hand(.9, .5)], []))
    check("clap: hands meet within 0.8 s = clap",          lambda: claps([near_l, near_r], [], dt=0.7))
    check("clap: too slow (> 0.8 s) isn't a clap",         lambda: not claps([near_l, near_r], [], dt=0.9))
    # Fingers pointing away from a chest camera: wrist and middle knuckle nearly
    # overlap in the image, but the knuckle line (index -> pinky) stays visible.
    def end_on_hand(cx, cy, width=0.10):
        lms = [(cx, cy)] * 21
        lms[0], lms[9] = (cx, cy + .005), (cx, cy - .005)
        lms[5], lms[17] = (cx, cy - width / 2), (cx, cy + width / 2)
        return {'landmarks': lms}
    pointing_away = end_on_hand(.5, .5)
    check("hands: a hand pointing away from the camera still counts as yours",
          lambda: shooting_hand([pointing_away], 1000, 1000) is pointing_away)
    check("clap: hands pointing away from the camera can clap",
          lambda: claps([end_on_hand(.44, .5), end_on_hand(.56, .5)]))
    check("clap: a smaller (further away) hand can't clap with yours",
          lambda: not claps([fake_hand(.45, .5, s=.2), fake_hand(.55, .5, s=.08)]))
    check("clap: a third, further hand doesn't stop your clap",
          lambda: claps([fake_hand(.45, .5), fake_hand(.55, .5), fake_hand(.9, .2, s=.07)]))
    check("clap: other player's clap doesn't count",
          lambda: not claps([fake_hand(.5, .3, s=.02), fake_hand(.52, .3, s=.02)]))

    from hand_detector import ready_to_fire
    pose = lambda **kw: {'index_extended': True, 'thumb_extended': False, 'middle_curled': False,
                         'ring_curled': False, 'pinky_curled': False, **kw}
    check("pose: fires without thumb",          lambda: ready_to_fire(pose(middle_curled=True)))
    check("pose: one curled finger is enough",  lambda: ready_to_fire(pose(ring_curled=True)))
    check("pose: open hand doesn't fire",       lambda: not ready_to_fire(pose(thumb_extended=True)))
    check("pose: index must be out",            lambda: not ready_to_fire(pose(index_extended=False,
                                                         middle_curled=True, ring_curled=True)))
    check("aim: finger at camera → tag under tip",
          lambda: pick_aimed_tag([fake_tag(4, 155, 305)], (150, 300), (152, 301))['id'] == 4)

    # Every allowed player ID must have a printable tag, or that player could never be shot
    from tag_detector import MAX_PLAYER_ID, TAG_FAMILY
    import pupil_apriltags
    detector = pupil_apriltags.Detector(families=TAG_FAMILY)  # kept alive: freeing it frees the family struct
    ncodes = detector.tag_families[TAG_FAMILY].contents.ncodes
    check(f"player IDs 1-{MAX_PLAYER_ID} all have a tag in {TAG_FAMILY} ({ncodes} codes)",
          lambda: MAX_PLAYER_ID == min(99, ncodes - 1))

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

        # One-handed gun, driven with a fake clock: one clap reading per frame
        await g.start_game()
        gun, clock = g.players[1].gun, [100.0]
        def frame(clapped=False, dt=0.1):
            clock[0] += dt
            gun.update(clapped, clock[0])
        async def shoot(target):
            return await g.process_shot(shooter_id=1, target_id=target, now=clock[0])
        async def reload():
            frame(True); frame(dt=1.0)

        check("gun: starts loaded and ready", lambda: gun.state(clock[0]) == 'ready')
        event, why = await shoot(2)
        check("shot event returned",     lambda: event is not None and why == 'hit')
        check("event has type",          lambda: event['type'] == 'shot_event')
        check("1 hit: target alive, 2 HP", lambda: g.players[2].alive and g.players[2].hp == 2
                                                    and not event['killed'])
        _, why = await shoot(2)
        check("gun: empty after a hit",  lambda: why == 'reload')
        frame(); frame(dt=5.0)
        check("gun: waiting doesn't reload, only a clap", lambda: gun.state(clock[0]) == 'reload')
        frame(True)
        _, why = await shoot(2)
        check("gun: clap reloads, then 1 s wait", lambda: why == 'arming')
        frame(dt=0.5)
        check("gun: still waiting at 0.5 s", lambda: gun.state(clock[0]) == 'arming')
        frame(dt=0.6)
        check("gun: ready 1 s after the clap", lambda: gun.state(clock[0]) == 'ready')
        frame(True)
        check("gun: clapping while loaded changes nothing", lambda: gun.state(clock[0]) == 'ready')
        check("gun: reload counter counts real reloads only (1 so far)", lambda: gun.reloads == 1)

        # A miss (teammate) leaves the gun loaded: it keeps shooting until a hit
        await g.add_player(3, 'red', ws=None)
        event3, why3 = await shoot(3)
        check("no friendly fire",        lambda: event3 is None and why3 == 'same_team')
        check("gun: a miss keeps it loaded", lambda: gun.state(clock[0]) == 'ready')

        # 3 hits to kill
        await shoot(2)
        check("2 hits: target alive, 1 HP", lambda: g.players[2].alive and g.players[2].hp == 1)
        await reload()
        event_k, _ = await shoot(2)
        check("3 hits: target is dead",  lambda: not g.players[2].alive and event_k['killed'])
        check("shooter still alive",     lambda: g.players[1].alive)
        check("kill credited once",      lambda: g.players[1].kills == 1)
        g.players[2].gun.fired()     # P2 died with an empty gun
        g.players[2].respawn()
        check("respawn restores HP",     lambda: g.players[2].hp == 3)
        check("respawn: loaded and ready, no clap needed", lambda: g.players[2].gun.state(0) == 'ready')
        check("respawn isn't a reload (no reload sound)", lambda: g.players[2].gun.reloads == 0)
        g.players[2].alive = False   # back to dead (no extra death) for the checks below

        # Already-dead target is rejected
        await reload()
        event4, why4 = await shoot(2)
        check("can't shoot a dead player", lambda: event4 is None and why4 == 'target_dead')

        # Unregistered target
        event5, why5 = await shoot(42)
        check("unknown target rejected", lambda: event5 is None and why5 == 'target_not_in_game')

        # Whose clap was it?  The phone that heard it loudest.
        from game_state import ClapReferee
        ref = ClapReferee()
        ref.heard(1, 0.6, 10.00); ref.heard(2, 0.95, 10.08)            # P2 claps next to P1
        check("clap referee: waits for other phones' reports", lambda: ref.settle(10.1) == [])
        check("clap referee: loudest phone owns the clap", lambda: ref.settle(10.3) == [(2, [1])])
        ref.heard(1, 0.9, 20.0)
        check("clap referee: a clap nobody else heard is yours", lambda: ref.settle(20.3) == [(1, [])])
        ref.heard(1, 0.9, 30.0); ref.heard(2, 0.9, 31.0)                # 1 s apart = two claps
        check("clap referee: separate claps stay separate",
              lambda: ref.settle(31.3) == [(1, []), (2, [])])

        # Reconnect: P9 re-registers on a new socket; the old socket closing late must not remove it
        old_ws, new_ws = object(), object()
        await g.add_player(9, 'blue', ws=old_ws)
        await g.add_player(9, 'blue', ws=new_ws)
        removed = await g.remove_player(9, old_ws)
        check("stale socket close keeps reconnected player", lambda: removed is None and 9 in g.players)

        # Rejoining (Wi-Fi drop, page refresh) is never a free heal: same player, as they were
        p2_before = g.players[2]
        p2_again  = await g.add_player(2, 'blue', ws=object())
        check("rejoin keeps the player as they were (down stays down, deaths kept)",
              lambda: p2_again is p2_before and not p2_again.alive and p2_again.deaths == 1)

        # A phone drops mid-game: the player is kept (offline) and their kills still count
        left = await g.remove_player(1, g.players[1].ws)
        check("mid-game disconnect keeps the player, marked offline",
              lambda: left == 'offline' and 1 in g.players and g.players[1].to_dict()['connected'] is False)
        check("an offline player's kills still count for the team", lambda: g.scores()['red_kills'] == 1)
        p1_back = await g.add_player(1, 'red', ws=object())
        check("rejoining brings them back online with their kills", lambda: p1_back.connected and p1_back.kills == 1)
        await g.remove_player(9, new_ws)   # P9's phone drops for good

        # Scores (kills-based)
        scores = g.scores()
        check("red has 1 kill",          lambda: scores['red_kills'] == 1)
        check("blue has 1 death",        lambda: scores['blue_deaths'] == 1)
        check("red is winning",          lambda: scores['winner'] == 'red')

        # Between games: a disconnect removes the player; a new game drops offline players
        await g.stop_game()
        await g.start_game()
        check("a new game drops players whose phones left", lambda: 9 not in g.players and 1 in g.players)
        await g.stop_game()
        left = await g.remove_player(3, None)
        check("disconnect between games removes the player", lambda: left == 'removed' and 3 not in g.players)

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
print("7. PERSON ID — telling players apart by their clothes")
print("=" * 55)

try:
    import tempfile, pathlib
    import numpy as np
    import person_id as pid_mod
    from person_id import (CONFIRM_S, Gallery, PeopleMatcher, PersonDetector, ScanSession,
                           clothing_descriptor, hands_mask, is_own_arm, pick_aimed_person, similarity)

    def outfit(shirt, trousers, light=1.0, seed=0, h=200, w=80):
        """Descriptor of a synthetic person: shirt colour over trousers colour (RGB),
        with camera noise, and the shirt / trousers masks the pose step would give."""
        rng = np.random.default_rng(seed)
        img = np.zeros((h, w, 3), np.float32)
        img[:h // 2], img[h // 2:] = shirt, trousers
        img = np.clip(img * light + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
        rows = np.repeat(np.arange(h)[:, None], w, axis=1)
        return clothing_descriptor(img, rows < h // 2, rows >= h // 2)

    RED, BLUE, GREEN = (200, 30, 30), (40, 60, 160), (40, 160, 60)
    BLACK, WHITE, JEANS = (25, 25, 28), (230, 230, 225), (60, 80, 130)
    red_blue = outfit(RED, BLUE)
    check("clothes: same outfit, 20% darker, still matches (> 0.9)",
          lambda: similarity(red_blue, outfit(RED, BLUE, light=0.8, seed=1)) > 0.9)
    check("clothes: same outfit, 20% brighter, still matches (> 0.9)",
          lambda: similarity(red_blue, outfit(RED, BLUE, light=1.2, seed=2)) > 0.9)
    check("clothes: red/blue vs green/black don't match (< 0.4)",
          lambda: similarity(red_blue, outfit(GREEN, BLACK)) < 0.4)
    check("clothes: black vs white shirt over the same jeans are told apart (< 0.75)",
          lambda: similarity(outfit(BLACK, JEANS), outfit(WHITE, JEANS)) < 0.75)
    check("clothes: identical = 1, and more alike scores higher",
          lambda: abs(similarity(red_blue, red_blue) - 1) < 1e-6
                  and similarity(red_blue, outfit(RED, BLACK)) > similarity(red_blue, outfit(GREEN, BLACK)))
    check("clothes: no trousers in view (cut off) -> compared on the shirt alone",
          lambda: similarity({**red_blue, 'lower': None}, outfit(RED, BLACK)) > 0.9)

    views = lambda shirt, trousers, base: [outfit(shirt, trousers, light=0.85 + 0.03 * i, seed=base + i)
                                           for i in range(8)]
    g = Gallery()
    g.set(1, views(RED, BLUE, 10))
    g.set(2, views(GREEN, BLACK, 20))
    g.set(4, views(WHITE, JEANS, 30))
    sc = g.scores(outfit(RED, BLUE, light=1.1, seed=99))
    check("gallery: a new view of player 1 scores best for player 1",
          lambda: max(sc, key=sc.get) == 1 and sc[1] > 0.9 and sc[2] < 0.5)
    check("gallery: the shooter is never a candidate", lambda: 1 not in g.scores(red_blue, exclude={1}))
    check("gallery: distinct outfits -> no look-alikes", lambda: g.confusable() == [])
    g.set(3, views(RED, BLUE, 40))               # same outfit as player 1
    check("gallery: two players in the same outfit are flagged as look-alikes",
          lambda: [p[:2] for p in g.confusable()] == [(1, 3)] and g.lookalikes(1)[0][0] == 3)
    with tempfile.TemporaryDirectory() as d:
        path = pathlib.Path(d) / 'gallery.json'
        g2 = Gallery(path)
        g2.set(1, views(RED, BLUE, 10))
        loaded = Gallery(path)
        check("gallery: saved scans load back after a restart",
              lambda: loaded.players() == [1] and abs(similarity(loaded.views(1)[0], g2.views(1)[0]) - 1) < 1e-3)
        check("gallery: deleting a scan", lambda: loaded.remove(1) and Gallery(path).players() == [])

    clear1 = {1: 0.95, 2: 0.60}
    m = PeopleMatcher(player_id=9)
    check("matcher: one clear frame is not enough (never fire on one misread)",
          lambda: m.identify([clear1], 10.0) == [None])
    check("matcher: the same player again within CONFIRM_S -> recognised",
          lambda: m.identify([clear1], 10.0 + CONFIRM_S / 2) == [1])
    m = PeopleMatcher()
    m.identify([clear1], 10.0)
    check("matcher: ...but not after more than CONFIRM_S", lambda: m.identify([clear1], 10.0 + CONFIRM_S + 0.1) == [None])
    m = PeopleMatcher()
    weak, close = {1: pid_mod.MIN_SIM - 0.05, 2: 0.3}, {1: 0.95, 2: 0.95 - pid_mod.MIN_MARGIN / 2}
    check("matcher: below MIN_SIM -> never recognised",
          lambda: m.identify([weak], 1.0) == [None] and m.identify([weak], 1.1) == [None])
    check("matcher: runner-up within MIN_MARGIN -> never recognised",
          lambda: m.identify([close], 2.0) == [None] and m.identify([close], 2.1) == [None])
    m = PeopleMatcher()
    check("matcher: alternating between two players doesn't confirm either",
          lambda: m.identify([clear1], 3.0) == [None] and m.identify([{1: 0.6, 2: 0.95}], 3.1) == [None])
    m = PeopleMatcher()
    m.identify([clear1], 4.0)
    check("matcher: two people can't both be player 1 (the better match keeps it)",
          lambda: m.identify([{1: 0.90, 2: 0.5}, {1: 0.97, 2: 0.5}], 4.1) == [None, 1])
    m.reset()
    check("matcher: reset forgets", lambda: m.identify([clear1], 4.2) == [None])

    W_, H_ = 960, 1280
    check("own arm: a box rising from the frame bottom to below the middle is ignored",
          lambda: is_own_arm((300, 800, 700, 1280), W_, H_))
    check("own arm: a close player cut off at the bottom is not",
          lambda: not is_own_arm((300, 200, 700, 1280), W_, H_))

    near_p, far_p = {'box': (400, 300, 600, 900)}, {'box': (450, 450, 520, 650)}
    up = ((500, 1100), (500, 1000))               # knuckle -> tip, pointing straight up the frame
    check("aim: finger ray through a body box hits it",
          lambda: pick_aimed_person([near_p], *up) is near_p)
    check("aim: a person well off the ray is missed",
          lambda: pick_aimed_person([{'box': (800, 300, 900, 600)}], *up) is None)
    check("aim: a person behind the fingertip is missed",
          lambda: pick_aimed_person([{'box': (450, 1150, 550, 1270)}], (500, 1000), (500, 900)) is None)
    check("aim: two people on the ray -> the closer (bigger) one",
          lambda: pick_aimed_person([far_p, near_p], *up) is near_p)
    check("aim: finger pointing at the camera -> the person under the fingertip",
          lambda: pick_aimed_person([near_p], (500, 600), (505, 605)) is near_p)
    behind_hand, across = {'box': (200, 150, 450, 650)}, {'box': (800, 150, 1050, 650)}
    check("aim: hand in front of one person, pointing across at another -> the other",
          lambda: pick_aimed_person([behind_hand, across], (330, 560), (407, 539)) is across)

    box = lambda y0, y1, x0=400, x1=560: {'box': (x0, y0, x1, y1)}
    check("scan: nobody in view", lambda: ScanSession.pick([], W_, H_) == (None, 'no_person'))
    check("scan: two people in view", lambda: ScanSession.pick([box(300, 1000), box(320, 990, 700, 860)], W_, H_)[1] == 'one_person')
    check("scan: feet cut off -> step back", lambda: ScanSession.pick([box(300, 1280)], W_, H_)[1] == 'step_back')
    check("scan: too far -> step closer", lambda: ScanSession.pick([box(500, 800)], W_, H_)[1] == 'closer')
    check("scan: one whole person -> scanned (a small one far behind is fine)",
          lambda: ScanSession.pick([box(200, 1100), box(500, 700, 50, 120)], W_, H_)[1] is None)

    mask = hands_mask([[(0.5, 0.5), (0.55, 0.5), (0.5, 0.6), (0.55, 0.6)]], 100, 100)
    check("hand mask covers the hand (grown a little), nothing else",
          lambda: mask[55, 52] and mask[49, 52] and not mask[10, 10] and not mask[90, 90])

    det = PersonDetector()
    check("person detector loads both models (efficientdet_lite2 + pose_landmarker_lite)", lambda: True)
    check("person detector: nobody in an empty frame",
          lambda: det.detect(np.full((640, 480, 3), 128, np.uint8)) == [])
    det.close()
except Exception as e:
    print(f"  {FAIL}  person_id error: {e}")
    import traceback; traceback.print_exc()

# ─────────────────────────────────────────────────────────────────────────────
print("\n" + "=" * 55)
print("DONE — fix any FAIL lines before running the game")
print("=" * 55)

