# -*- coding: utf-8 -*-
"""
test_api.py
===========
Server test with the REAL camera pipeline — no camera, no phone, no separate
terminal (the server runs inside this test).

  1. Web pages and downloads (dashboard, phone page, tags, print PDF, zip)
  2. The camera pipeline on real JPEG frames: a real printed tag is rendered
     into a camera-sized frame, and the real AprilTag detector + aiming code
     + server must turn "finger pointing at tag 2" into a hit on player 2.
     The hand is scripted (MediaPipe needs a photo of a real hand; real hand
     detection is covered by playtesting).
  3. How small (far away) a tag can be and still be read, with motion blur
  4. Start / stop over the REST API
  5. Players told apart by their clothes: the scan station, then hits on the
     recognised player only.  People and their clothes are scripted (the real
     detectors need photos of real people; test_smoke checks the matching on
     synthetic outfits, playtesting covers real ones)

    python test_api.py
"""
from __future__ import annotations

import asyncio
import io
import pathlib
import sys
import tempfile
import zipfile

import aiohttp
import cv2
import numpy as np
from aiohttp.test_utils import TestServer

import person_id
import server
from game_state import ARM_DELAY_S, CLAP_MATCH_S
from gen_tags import make_tag_image
from person_id import Gallery, clothing_descriptor
from tag_detector import TagDetector

if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')

PASS = '\033[92mPASS\033[0m'
FAIL = '\033[91mFAIL\033[0m'
passed = failed = 0

W, H = 1280, 720          # the phone sends 1280x720 frames


def check(label, cond, detail=''):
    global passed, failed
    if cond:
        passed += 1
        print(f'  {PASS}  {label}')
    else:
        failed += 1
        print(f'  {FAIL}  {label}  {detail}')


# ── Frames ────────────────────────────────────────────────────────────────────

def frame_with_tag(tag_id: int | None, cell_px: int = 20, center=(640, 360), blur: int = 0) -> np.ndarray:
    """A grey camera frame with a printed tag (white border included) at `center`."""
    img = np.full((H, W), 128, np.uint8)
    if tag_id is not None:
        tag = make_tag_image(tag_id, cell_px=cell_px, quiet_extra=2)
        th, tw = tag.shape
        x0, y0 = center[0] - tw // 2, center[1] - th // 2
        img[y0:y0 + th, x0:x0 + tw] = tag
    if blur:
        img = cv2.GaussianBlur(img, (blur, blur), 0)
    return cv2.cvtColor(img, cv2.COLOR_GRAY2BGR)


def jpeg(bgr: np.ndarray) -> bytes:
    return cv2.imencode('.jpg', bgr, [cv2.IMWRITE_JPEG_QUALITY, 70])[1].tobytes()   # phone: quality 0.7


# ── Scripted hand ─────────────────────────────────────────────────────────────

def pointing_hand(at=(640, 360), knuckle=(330, 560)) -> dict:
    """A finger-gun hand (21 normalised landmarks) whose index finger points at `at`."""
    kx, ky = knuckle
    dx, dy = at[0] - kx, at[1] - ky
    n = (dx * dx + dy * dy) ** 0.5
    tip = (kx + 80 * dx / n, ky + 80 * dy / n)
    px = {0: (300, 650), 5: knuckle, 8: tip, 9: (315, 565), 13: (300, 570), 17: (285, 578)}
    lms = [(px.get(i, px[0])[0] / W, px.get(i, px[0])[1] / H) for i in range(21)]
    crit = {k: True for k in ('index_extended', 'thumb_extended', 'middle_curled', 'ring_curled', 'pinky_curled')}
    hand = {'confidence': 1.0, 'armed': True, 'ready': True, 'landmarks': lms, 'criteria': crit}
    return {**hand, 'detected': True, 'hands': [hand]}


class ScriptedHands:
    """Stands in for MediaPipe: returns whatever hand the test sets (None = real detector)."""
    hand: dict | None = None

    def __init__(self, real):
        self.real = real

    def process(self, rgb):
        return ScriptedHands.hand if ScriptedHands.hand is not None else self.real.process(rgb)


class ScriptedPeople:
    """Stands in for the person detector: `people` = [(box, clothing descriptor)] in view."""
    people: list = []

    def detect(self, rgb):
        return [{'box': box, 'score': 0.9} for box, _ in ScriptedPeople.people]

    def describe(self, rgb, box, mask_out=None):
        return next((d for b, d in ScriptedPeople.people if b == tuple(box)), None)


def outfit(shirt, trousers, light=1.0, seed=0, h=200, w=80) -> dict:
    """Clothing descriptor of a synthetic person: shirt colour over trousers colour (RGB)."""
    rng = np.random.default_rng(seed)
    img = np.zeros((h, w, 3), np.float32)
    img[:h // 2], img[h // 2:] = shirt, trousers
    img = np.clip(img * light + rng.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    rows = np.repeat(np.arange(h)[:, None], w, axis=1)
    return clothing_descriptor(img, rows < h // 2, rows >= h // 2)


RED, BLUE, GREEN, BLACK = (200, 30, 30), (40, 60, 160), (40, 160, 60), (25, 25, 28)
YELLOW, WHITE = (220, 200, 40), (230, 230, 225)


# ── Test ──────────────────────────────────────────────────────────────────────

async def run() -> int:
    real_hand_detector = server._hand_detector
    server._hand_detector = lambda: ScriptedHands(real_hand_detector())
    server._person_detector = lambda: ScriptedPeople()
    scans_dir = pathlib.Path(tempfile.mkdtemp())          # never touch the real scans
    server.SCANS_DIR = scans_dir
    server.gallery = Gallery(scans_dir / 'gallery.json')
    server.ID_MODE = 'tags'                               # sections 2-4: printed bibs

    ts = TestServer(server.build_app())
    await ts.start_server()
    g = server.game
    async with aiohttp.ClientSession() as s:
        url = lambda path: ts.make_url(path)

        print('\n1. Web pages and downloads')
        for path, ctype in [('/', 'text/html'), ('/phone', 'text/html'), ('/tags', 'text/html'),
                            ('/tags/player_01.png', 'image/png'), ('/tags/print_tags.pdf', 'application/pdf'),
                            ('/api/state', 'application/json'), ('/api/network-info', 'application/json'),
                            ('/scan', 'text/html'), ('/api/scans', 'application/json')]:
            async with s.get(url(path)) as r:
                check(f'GET {path} -> 200 {ctype}', r.status == 200 and r.content_type == ctype,
                      f'{r.status} {r.content_type}')
        async with s.get(url('/tags.zip')) as r:
            names = zipfile.ZipFile(io.BytesIO(await r.read())).namelist()
            check('GET /tags.zip -> all 10 player tags', r.status == 200 and len(names) == 10, names)
        async with s.get(url('/api/network-info')) as r:
            info = await r.json()
            check('network info gives the LAN IP and both ports',
                  info['ip'] and info['port'] and info['https_port'], info)
        async with s.get(url('/phone')) as r:
            page = await r.text()
            check('phone page allows player IDs 1-34 (the printed tags)', 'max="34"' in page)
        async with s.get(url('/scan')) as r:
            page = await r.text()
            check('scan station page talks to /ws/scan and /api/scans', "'ws/scan'" in page and "'api/scans'" in page)

        print('\n2. Camera pipeline on real JPEG frames')
        p1 = await s.ws_connect(url('/ws/phone'))
        p2 = await s.ws_connect(url('/ws/phone'))
        await p1.send_json({'type': 'register', 'player_id': 1, 'team': 'red'})
        await p1.receive_json()
        await p2.send_json({'type': 'register', 'player_id': 2, 'team': 'blue'})
        await p2.receive_json()
        async with s.post(url('/api/start')) as r:
            check('POST /api/start', (await r.json()).get('ok') and g.game_active)

        async def send(frame: bytes) -> dict:
            await p1.send_bytes(frame)
            return await p1.receive_json(timeout=20)

        ScriptedHands.hand = None
        r = await send(jpeg(frame_with_tag(None)))
        check('empty frame, real hand detector: no hand, no shot',
              r['status'] == 'no_hand' and r['pose'] == 0 and not r['shot_fired'], r['status'])
        r = await send(b'this is not a jpeg')
        check('corrupt frame: the phone still gets a reply', r.get('error') == 'jpeg_decode_failed', r)

        ScriptedHands.hand = pointing_hand(at=(640, 360))
        r = await send(jpeg(frame_with_tag(2)))
        seen = [t['id'] for t in r['tags_seen']]
        check('the real tag detector reads printed tag 2 in the JPEG', seen == [2], seen)
        check('finger pointing at it: HIT on player 2',
              r['status'] == 'hit' and r['aiming_tag_id'] == 2 and r['target_hp'] == 2, r['status'])
        check('...where the tag really is (corners around the frame centre)',
              all(abs(x - 640) < 140 and abs(y - 360) < 140 for x, y in r['tags_seen'][0]['corners']),
              r['tags_seen'][0]['corners'])
        await asyncio.sleep(0.05)
        ev = await p2.receive_json(timeout=5)
        check("player 2's phone is told it was hit", ev.get('type') == 'shot_event' and ev['hp'] == 2, ev)

        async def reload():
            await p1.send_json({'type': 'clap', 'peak': 0.9})
            await asyncio.sleep(CLAP_MATCH_S + 0.15 + ARM_DELAY_S + 0.15)
            await asyncio.sleep(max(server.TRIGGER_WINDOW_S, server.TAG_MEMORY_S))   # old aims/tags expire

        await reload()
        ScriptedHands.hand = pointing_hand(at=(640, 700))      # pointing well below the tag
        r = await send(jpeg(frame_with_tag(2)))
        check('finger pointing away from the tag: no shot',
              r['status'] == 'not_aimed' and g.players[2].hp == 2, r['status'])
        ScriptedHands.hand = pointing_hand(at=(640, 360))
        r = await send(jpeg(frame_with_tag(None)))
        check('tag missed for a frame (blur): still hittable where it just was',
              r['status'] == 'hit' and r['target_hp'] == 1, r['status'])

        await reload()
        r = await send(jpeg(frame_with_tag(None)))
        check('...but not once it has been gone for 0.8 s: no shot', r['status'] == 'no_tag_visible', r['status'])
        moved = (1050, 560)          # ~390 px off the finger's line (aiming allows ~250 px here)
        r = await send(jpeg(frame_with_tag(2, center=moved)))
        check('tag moved well off the finger line: no shot', r['status'] == 'not_aimed', r['status'])
        ScriptedHands.hand = pointing_hand(at=moved)
        r = await send(jpeg(frame_with_tag(2, center=moved)))
        check('...pointing at it there: HIT (the kill)',
              r['status'] == 'hit' and r['target_hp'] == 0 and r['shot_killed'], r['status'])
        await p1.close()
        await p2.close()

        print('\n3. How far away a tag still reads (real detector, JPEG, motion blur)')
        det = TagDetector(quad_decimate=1.0)               # the server's settings
        for cell_px, blur, label in [(20, 0, '260 px'), (8, 0, '104 px'), (5, 0, '65 px'),
                                     (8, 5, '104 px, blurred'), (5, 3, '65 px, slightly blurred')]:
            bgr = cv2.imdecode(np.frombuffer(jpeg(frame_with_tag(2, cell_px=cell_px, blur=blur)), np.uint8),
                               cv2.IMREAD_COLOR)
            ids = [t['id'] for t in det.detect(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY))]
            check(f'tag {label} wide in a 1280x720 frame: read', ids == [2], ids)

        print('\n4. Stop')
        async with s.post(url('/api/stop')) as r:
            j = await r.json()
            check('POST /api/stop returns the final scores',
                  j.get('ok') and j['scores']['red_kills'] == 1 and not g.game_active, j)

        print('\n5. Told apart by clothes: scan station, then the game')
        server.ID_MODE = 'clothes'
        person_id.SCAN_SAMPLE_S = 0.0                      # no need to wait for a real turn-around
        person_id.SCAN_SAMPLES = server.SCAN_SAMPLES = 4
        whole = (300.0, 150.0, 600.0, 650.0)               # a whole person in a 1280x720 frame
        blank = jpeg(frame_with_tag(None))

        st = await s.ws_connect(url('/ws/scan'))
        await st.send_bytes(blank)
        r = await st.receive_json(timeout=20)
        check('scan station: frames before Start just get "idle"', r.get('type') == 'scan_idle', r)
        await st.send_json({'type': 'scan_start', 'player_id': 99})
        r = await st.receive_json(timeout=20)
        check('scan station: player number out of range refused', r.get('error') == 'invalid_player_id', r)

        async def scan(pid: int, shirt, trousers, seed: int) -> dict:
            await st.send_json({'type': 'scan_start', 'player_id': pid})
            await st.receive_json(timeout=20)
            for i in range(10):
                ScriptedPeople.people = [(whole, outfit(shirt, trousers, light=0.9 + 0.05 * i, seed=seed + i))]
                await st.send_bytes(blank)
                r = await st.receive_json(timeout=20)
                if r.get('type') == 'scan_done':
                    return r
            return r

        await st.send_json({'type': 'scan_start', 'player_id': 2})
        await st.receive_json(timeout=20)
        ScriptedPeople.people = []
        await st.send_bytes(blank)
        r = await st.receive_json(timeout=20)
        check('scan station: nobody in view -> a hint, no view taken',
              r.get('type') == 'scan_progress' and r['hint'] == 'Nobody in view' and r['samples'] == 0, r)
        ScriptedPeople.people = [((300.0, 150.0, 600.0, 720.0), outfit(RED, BLUE))]
        await st.send_bytes(blank)
        r = await st.receive_json(timeout=20)
        check('scan station: feet out of view -> "step back"', r['hint'].startswith('Step back') and r['samples'] == 0, r)
        r = await scan(2, RED, BLUE, 100)
        check('P2 scanned (red shirt, blue trousers): no look-alikes',
              r.get('type') == 'scan_done' and r['player_id'] == 2 and r['looks_like'] == [], r)
        async with s.get(url('/' + r['thumbnail'])) as tr:
            check("...the scan's thumbnail is served", tr.status == 200 and tr.content_type == 'image/jpeg', tr.status)
        r = await scan(3, GREEN, BLACK, 200)
        check('P3 scanned (green shirt, black trousers)', r.get('type') == 'scan_done' and r['looks_like'] == [], r)
        r = await scan(4, RED, BLUE, 300)
        check('P4 scanned in the same clothes as P2: warned, looks like P2',
              [a['player_id'] for a in r.get('looks_like', [])] == [2], r)
        async with s.get(url('/api/scans')) as resp:
            j = await resp.json()
        check('/api/scans lists P2-P4 and the look-alike pair P2/P4',
              [x['player_id'] for x in j['scans']] == [2, 3, 4]
              and [(p['a'], p['b']) for p in j['lookalikes']] == [(2, 4)], j)
        async with s.delete(url('/api/scans/4')) as resp:
            check('DELETE /api/scans/4 removes the scan', (await resp.json())['ok'] and 4 not in server.gallery)
        check('scans are saved to disk (kept across a restart)', Gallery(scans_dir / 'gallery.json').players() == [2, 3])
        await st.close()

        p1, p2, p3 = [await s.ws_connect(url('/ws/phone')) for _ in range(3)]
        await p1.send_json({'type': 'register', 'player_id': 1, 'team': 'red'})
        r = await p1.receive_json()
        check('P1 joins: told it is not scanned', r.get('scanned') is False and r.get('id_mode') == 'clothes', r)
        await p2.send_json({'type': 'register', 'player_id': 2, 'team': 'blue'})
        r = await p2.receive_json()
        check('P2 joins: told it is scanned', r.get('scanned') is True, r)
        await p3.send_json({'type': 'register', 'player_id': 3, 'team': 'blue'})
        await p3.receive_json()
        async with s.get(url('/api/state')) as resp:
            st_players = (await resp.json())['players']
        check('host state shows who is scanned', st_players['1']['scanned'] is False and st_players['2']['scanned'], st_players)
        async with s.post(url('/api/start')) as resp:
            await resp.json()

        async def send1(frame: bytes = blank) -> dict:
            await p1.send_bytes(frame)
            return await p1.receive_json(timeout=20)

        async def reload1():
            await p1.send_json({'type': 'clap', 'peak': 0.9})
            await asyncio.sleep(CLAP_MATCH_S + 0.15 + ARM_DELAY_S + 0.15)
            await asyncio.sleep(max(server.TRIGGER_WINDOW_S, server.PERSON_MEMORY_S))

        ScriptedHands.hand = pointing_hand(at=(450, 400))
        ScriptedPeople.people = [(whole, outfit(RED, BLUE, light=1.1, seed=7))]      # P2, as P1's camera sees them
        r = await send1()
        check('pointing at P2: the first frame alone is not enough to fire',
              r['status'] == 'not_identified' and not r['shot_fired'], r['status'])
        check('...P2 is in view as an unrecognised person ("?")',
              r['tags_seen'][0]['id'] is None and r['tags_seen'][0]['kind'] == 'person', r['tags_seen'])
        r = await send1()
        check('...recognised on the next frame: HIT on P2',
              r['status'] == 'hit' and r['aiming_tag_id'] == 2 and g.players[2].hp == 2, r['status'])
        await asyncio.sleep(0.05)
        ev = await p2.receive_json(timeout=5)
        check("P2's phone is told it was hit", ev.get('type') == 'shot_event' and ev['hp'] == 2, ev)

        await reload1()
        ScriptedPeople.people = [(whole, outfit(YELLOW, WHITE, seed=8))]        # nobody scanned looks like this
        statuses = [(await send1())['status'] for _ in range(4)]
        check('pointing at someone who matches no scan: never a hit',
              statuses == ['not_identified'] * 4 and g.players[2].hp == 2 and g.players[3].hp == 3, statuses)

        left, right = (200.0, 150.0, 450.0, 650.0), (800.0, 150.0, 1050.0, 650.0)
        ScriptedPeople.people = [(left, outfit(RED, BLUE, seed=9)), (right, outfit(GREEN, BLACK, seed=10))]
        ScriptedHands.hand = pointing_hand(at=(925, 400))
        r1, r2 = await send1(), await send1()
        check('P2 and P3 in view, pointing at P3: HIT on P3, not P2',
              r2['status'] == 'hit' and r2['aiming_tag_id'] == 3 and g.players[3].hp == 2 and g.players[2].hp == 2,
              (r1['status'], r2['status'], r2['aiming_tag_id']))

        await reload1()
        ScriptedPeople.people = [((500.0, 450.0, 800.0, 720.0), outfit(RED, BLUE, seed=11))]   # your own arm
        r = await send1()
        check("your own arm (rising from the frame bottom) isn't a person",
              r['tags_seen'] == [] and r['status'] == 'no_tag_visible', (r['tags_seen'], r['status']))
        for ws_ in (p1, p2, p3):
            await ws_.close()
        async with s.post(url('/api/stop')) as resp:
            await resp.json()
    await ts.close()
    server._executor.shutdown(wait=True)      # frame workers finish before Python exits

    print('\n' + '=' * 55)
    print(f'  {passed} passed   {failed} failed')
    print('=' * 55)
    return failed


if __name__ == '__main__':
    code = asyncio.run(run())
    sys.stdout.flush()
    # Skip interpreter teardown: Python 3.13 prints a harmless "Exception ignored
    # in _DeleteDummyThreadOnDel" for the camera libraries' native threads
    import os
    os._exit(code)
