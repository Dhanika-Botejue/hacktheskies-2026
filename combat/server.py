"""
server.py
=========
HackTheSkies: finger-gun shooting game.  Who you shot is told by what they're
wearing (ID_MODE 'clothes': everyone's body scan from the scan station, see
person_id.py), or by a printed AprilTag bib (ID_MODE 'tags', see tag_detector.py).
It also runs the match (objectives.py): downed players come back in at their
base, and the team that holds the flag longest wins.  Where everyone is comes
from the game server (tracking.py; GAME_SERVER, default https://127.0.0.1:8443).

WebSocket protocol — phone (/ws/phone)
---------------------------------------
  Phone -> Server:  JSON  {"type":"register","player_id":7,"team":"red"}
                    (player_id 1..MAX_PLAYER_ID; re-registering the same ID + team,
                    e.g. after a Wi-Fi drop, rejoins as that player)
  Phone -> Server:  JSON  {"type":"clap","peak":0.83}   (mic heard a clap; sent for
                    every clap, so the server can tell whose it was — see ClapReferee)
  Phone -> Server:  JSON  {"type":"mic","state":"listening"}   (for the log)
  Phone -> Server:  BINARY  <raw JPEG bytes>  (~10 fps)
  Server -> Phone:  JSON  registered | {"error": ...}   (reply to register)
  Server -> Phone:  JSON  frame_result | idle | {"error": ...}   (reply to every frame)
  Server -> Phone:  JSON  shot_event     (push, when this player is hit)
  In frame_result, tags_seen / aiming_tag_id are the targets in view whatever
  ID_MODE is: bib tags, or people (kind "person"; id null = not recognised).
  frame_result and shot_event carry respawn_mode: 'base' (when down, walk back
  to your base) or 'timer' (back at respawn_at); frame_result carries flag:
  {owner, challenger, win_s, held: {red, blue}} (null: no flag on the map), and
  idle carries the scores, with the winner once a game is over.

WebSocket protocol — scan station (/ws/scan, static/scan.html)
---------------------------------------------------------------
  Station -> Server:  JSON  {"type":"scan_start","player_id":7} | {"type":"scan_stop"}
  Station -> Server:  BINARY  <raw JPEG bytes>  (one frame in flight)
  Server -> Station:  JSON  scan_started | scan_progress (every frame) | scan_done |
                            scan_idle (frame while no scan runs) | {"error": ...}

WebSocket protocol — host (/ws/host)
--------------------------------------
  Host -> Server:  JSON  {"type":"start_game","match_s":30,"win_s":120} (both optional: match_s = match length, most kills wins; no match_s = no limit) | {"type":"stop_game"}
                         {"type":"respawn_player","player_id":7}   (back in by hand)
  Server -> Host:  JSON  state_update  (every 250 ms; objectives: bases, flag, clocks)
  Server -> Host:  JSON  shot_event    (push)
  Server -> Host:  JSON  game_started | game_over (scores.reason 'flag' | 'host')
  Server -> Host:  JSON  respawned {player_id, how: 'base'|'timer'|'host'} | flag_taken {team, from}

REST
-----
  POST /api/start         Start game (optional JSON body {"match_s": 30, "win_s": 120})
  POST /api/stop          Stop game
  GET  /api/state         Full game snapshot
  GET  /api/network-info  Returns LAN IP for phone auto-fill
  GET  /api/scans         Scanned players, thumbnails, look-alike pairs
  DELETE /api/scans/{id}  Forget a player's scan
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Set

import cv2
import numpy as np
from aiohttp import web, WSMsgType

from game_state import CLAP_MATCH_S, GameState, MAX_HP
from hand_detector import HandDetector, INDEX_MCP, INDEX_TIP, ClapTracker, own_hands, own_palms, shooting_hand
from person_id import (SCAN_SAMPLES, Gallery, PeopleMatcher, PersonDetector, ScanSession, aim_candidates,
                       box_corners, box_iou, hands_mask, is_own_arm, pick_aimed_person)
from tag_detector import MAX_PLAYER_ID, TagDetector, pick_aimed_tag
from tracking import PositionFeed, ws_url

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s  %(message)s',
)
log = logging.getLogger('hacktheskies')

HOST  = '0.0.0.0'
PORT  = int(os.environ.get('PORT', 8765))
STATIC_DIR = Path(__file__).parent / 'static'
TAGS_DIR   = Path(__file__).parent / 'tags'   # written by gen_tags.py
SCANS_DIR  = Path(__file__).parent / 'scans'  # body scans + thumbnails (gitignored)
CERT_DIR   = Path(__file__).parent / 'certs'  # self-signed HTTPS cert (gitignored)
HTTPS_PORT = int(os.environ.get('HTTPS_PORT', PORT + 1))

# How the target is told: 'clothes' (pre-game body scan at /scan) or 'tags' (printed bibs)
ID_MODE = os.environ.get('HTS_ID_MODE', 'tags')   # 'tags': printed AprilTag bibs (default) | 'clothes': scan station
# Where players are (positions, bases and flag): the game server, as the viewer's GAME_SERVER
GAME_SERVER = os.environ.get('GAME_SERVER', 'https://127.0.0.1:8443')

POSE_ARMED_THRESHOLD = 0.75   # pose bar colour on the phone (information only)
# One-handed finger gun (game_state.Gun): while loaded it fires on any frame
# with a finger-gun pose aimed at a tag, until one hits; then clap to reload.
# Detection flickers frame to frame (blur, distance), so the pose and the aim
# each still count for TRIGGER_WINDOW_S (not necessarily the same frame), and
# tags that drop out are remembered at their last position for TAG_MEMORY_S.
TRIGGER_WINDOW_S     = 0.8
TAG_MEMORY_S         = 0.8
# People are remembered for less: a chest camera swings them across the frame
# as its wearer turns, so an old position soon points at someone else.
PERSON_MEMORY_S      = 0.3
MAX_DESCRIBED        = 2      # people near the finger's line matched per frame (~12 ms each)
MAX_FRAME_BYTES      = 2 * 1024 * 1024
STATE_BROADCAST_INTERVAL = 0.25   # also how often the objectives run (respawns, the flag)
HOST_SEND_TIMEOUT_S  = 1.0   # a host screen that can't take a message this long is hung up on
DIAG_LOG_INTERVAL    = 1.0   # seconds between per-player "why didn't it fire" log lines
# Set HTS_DEBUG_FRAMES=<dir> to save ~2 raw frames/sec per player (filename has the status)
DEBUG_FRAME_DIR      = os.environ.get('HTS_DEBUG_FRAMES')

# ── Thread-local detectors ────────────────────────────────────────────────────
_tlocal = threading.local()

def _hand_detector() -> HandDetector:
    if not hasattr(_tlocal, 'hand'):
        _tlocal.hand = HandDetector()
    return _tlocal.hand

def _tag_detector() -> TagDetector:
    if not hasattr(_tlocal, 'tag'):
        # Full resolution: decimate=2 missed every on-screen tag in real webcam
        # frames; 1.0 costs ~35 ms/frame at 720p.
        _tlocal.tag = TagDetector(quad_decimate=1.0)
    return _tlocal.tag

def _person_detector() -> PersonDetector:
    if not hasattr(_tlocal, 'person'):
        _tlocal.person = PersonDetector()
    return _tlocal.person

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='frame-worker')

game         = GameState()
gallery      = Gallery(SCANS_DIR / 'gallery.json')   # everyone's body scan, kept across restarts
host_sockets: Set[web.WebSocketResponse] = set()
feed         = PositionFeed(ws_url(GAME_SERVER), on_points=game.objectives.set_points)


# ── Frame processing (runs in thread pool) ────────────────────────────────────

def _find_people(rgb: np.ndarray, hands: list[dict], aim, matcher: PeopleMatcher | None) -> list[dict]:
    """
    The people in view as targets, shaped like tags: {'id' (None = not
    recognised), 'box', 'center', 'corners', 'kind': 'person'}.  Only the
    MAX_DESCRIBED nearest the finger's line (aim = (knuckle, tip)) are matched
    against the scans; their scores go in 'match' for the log.
    """
    h, w = rgb.shape[:2]
    detector = _person_detector()
    people = []
    for p in detector.detect(rgb):
        if is_own_arm(p['box'], w, h):
            continue
        x0, y0, x1, y1 = p['box']
        people.append({'id': None, 'box': p['box'], 'center': ((x0 + x1) / 2, (y0 + y1) / 2),
                       'corners': box_corners(p['box']), 'kind': 'person'})
    near = aim_candidates(people, *aim)[:MAX_DESCRIBED] if aim and matcher else []
    if not near:
        return people

    # Your own hand in front of them isn't part of their clothes
    mask = hands_mask([hd['landmarks'] for hd in own_hands(hands, w, h)], w, h)
    described, scored = [], []
    for p in near:
        desc = detector.describe(rgb, p['box'], mask)
        if desc is None:
            p['match'] = 'no body'
            continue
        scores = gallery.scores(desc, exclude={matcher.player_id})
        best = sorted(scores.items(), key=lambda kv: -kv[1])[:2]
        p['match'] = ' '.join(f'P{pid}:{s:.2f}' for pid, s in best) or 'nobody scanned'
        described.append(p)
        scored.append(scores)
    for p, pid in zip(described, matcher.identify(scored, time.monotonic())):
        p['id'] = pid
    return people


def _process_frame(jpeg_bytes: bytes, remembered: list[dict], people: PeopleMatcher | None = None) -> dict:
    """remembered: targets seen in recent frames but possibly missed in this
    one (blur etc.); used for aiming if not re-detected, marked stale=True.
    people: this phone's PeopleMatcher (ID_MODE 'clothes')."""
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        return {'error': 'jpeg_decode_failed'}

    h, w = bgr.shape[:2]
    rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)

    hand_result = _hand_detector().process(rgb)

    # One hand shoots: the one closest to the camera (biggest in the frame).
    # Its index finger aims along the knuckle -> fingertip ray.
    hands = hand_result['hands']
    shooter = shooting_hand(hands, w, h)
    aim = None
    if shooter and shooter['criteria']['index_extended']:
        lms = shooter['landmarks']
        aim = ((lms[INDEX_MCP][0] * w, lms[INDEX_MCP][1] * h),    # knuckle
               (lms[INDEX_TIP][0] * w, lms[INDEX_TIP][1] * h))    # fingertip

    unknown = []
    if ID_MODE == 'tags':
        # Only look for player tags (IDs 1-99)
        gray  = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        fresh = [t for t in _tag_detector().detect(gray) if t['role'] == 'player']
    else:
        # Only while a hand of yours is in view: no aim without one, and it saves CPU
        found   = _find_people(rgb, hands, aim, people) if shooter else []
        fresh   = [p for p in found if p['id'] is not None]
        unknown = [p for p in found if p['id'] is None]
    fresh_ids = {t['id'] for t in fresh}
    # A remembered person under a fresh box is that box now (which may be someone else)
    boxes = [p['box'] for p in fresh + unknown if 'box' in p]
    tags = fresh + [{**t, 'stale': True} for t in remembered
                    if t['id'] not in fresh_ids
                    and not ('box' in t and any(box_iou(t['box'], b) > 0.3 for b in boxes))]

    tag, aimed_unknown = None, False
    if aim and ID_MODE == 'tags':
        tag = pick_aimed_tag(tags, *aim)
    elif aim:
        # Pointing at someone not recognised (yet) is never a shot at anyone else
        aimed = pick_aimed_person(tags + unknown, *aim)
        tag = aimed if aimed and aimed['id'] is not None else None
        aimed_unknown = aimed is not None and aimed['id'] is None

    return {
        'pose':      shooter['confidence'] if shooter else 0.0,
        'armed':     bool(shooter and shooter['armed']),
        'ready':     bool(shooter and shooter['ready']),
        'shooting':  shooter is not None,
        'own_palms': own_palms(hands, w, h),   # 2 entries = both your hands: no shooting
        'n_hands':   len(hands),
        'detected':  hand_result['detected'],
        'landmarks': shooter['landmarks'] if shooter else None,
        'other_landmarks': [hd['landmarks'] for hd in hands if hd is not shooter],
        'criteria':  shooter['criteria'] if shooter else {},
        'tag':       tag,
        'aimed_unknown': aimed_unknown,
        'fresh_tags': fresh,
        'tags_seen': [{'id': t['id'], 'corners': t['corners'], 'stale': t.get('stale', False),
                       'kind': t.get('kind', 'tag')} for t in tags + unknown],
        'matches':   [f'{p["id"] or "?"}=[{p["match"]}]' for p in fresh + unknown if 'match' in p],
        'frame_w':   w,
        'frame_h':   h,
    }


def _scan_frame(jpeg_bytes: bytes, want_view: bool) -> dict:
    """
    One frame from the scan station: the person being scanned ('box') or why
    the frame won't do ('hint', a ScanSession.HINTS key), and when the scan
    wants another view, their clothing descriptor ('desc') + RGB crop ('thumb').
    """
    bgr = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8), cv2.IMREAD_COLOR)
    if bgr is None:
        return {'error': 'jpeg_decode_failed'}
    h, w = bgr.shape[:2]
    rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    detector = _person_detector()
    person, hint = ScanSession.pick(detector.detect(rgb), w, h)
    out = {'box': person['box'] if person else None, 'hint': hint, 'desc': None, 'thumb': None,
           'frame_w': w, 'frame_h': h}
    if person and want_view:
        desc = detector.describe(rgb, person['box'])
        if desc is None:
            out['hint'] = 'no_body'
        elif desc['lower'] is None:
            out['hint'] = 'step_back'
        else:
            x0, y0, x1, y1 = (int(v) for v in person['box'])
            out['desc'], out['thumb'] = desc, rgb[y0:y1, x0:x1].copy()
    return out


# ── Broadcast helpers ─────────────────────────────────────────────────────────

async def _settle_claps():
    """Reload whoever's clap it was (the phone that heard it loudest)."""
    now = time.monotonic()
    for clapper, others in game.claps.settle(now):
        p = game.players.get(clapper)
        if p and p.alive and p.gun.state(now) == 'reload':
            p.gun.update(True, now)
            log.info(f'CLAP  P{clapper} reloaded (mic)'
                     + (f'  — also heard by P{", P".join(map(str, others))}, ignored there' if others else ''))
        elif others:
            log.info(f'CLAP  by P{clapper} (no reload needed) — not counted for P{", P".join(map(str, others))}')


def _snapshot() -> dict:
    """game.snapshot() plus whether each player has been scanned, and whether
    the game server (positions) is connected."""
    snap = game.snapshot()
    for pid, p in snap['players'].items():
        p['scanned'] = int(pid) in gallery
    snap['id_mode'] = ID_MODE
    snap['objectives']['tracking'] = feed.connected
    return snap


def _owns(player, ws) -> bool:
    """Does this socket still speak for this player?  Not once another phone has
    registered as them, nor if their record was replaced (same ID, other team)."""
    return player is not None and player.ws is ws and game.players.get(player.player_id) is player


async def _send_host(ws, data: dict) -> bool:
    try:
        await asyncio.wait_for(ws.send_json(data), HOST_SEND_TIMEOUT_S)
        return True
    except Exception:
        return False


async def _broadcast_host(data: dict):
    # Each with a time limit: a host screen that stopped reading (a spectator's
    # phone locked or out of Wi-Fi) would otherwise hold up the match itself
    # (_state_broadcaster).  It's hung up on, so it reconnects.
    for ws in list(host_sockets):
        if not await _send_host(ws, data) and ws in host_sockets:
            host_sockets.discard(ws)
            asyncio.ensure_future(ws.close())


# ── WebSocket — Phone ─────────────────────────────────────────────────────────

async def ws_phone(request: web.Request) -> web.WebSocketResponse:
    # heartbeat: notice a phone that silently dropped off the Wi-Fi (marked offline)
    ws     = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=10.0)
    await ws.prepare(request)

    player = None
    loop   = asyncio.get_event_loop()
    last_diag = last_dump = 0.0
    tag_memory: dict[int, tuple[dict, float]] = {}   # tag id -> (tag, last seen)
    last_ready_at = 0.0                              # last frame with a finger-gun pose
    last_aim: tuple[int, float] | None = None        # (tag id, when) last aimed at
    seen_deaths = 0                                  # player.deaths at the last frame
    claps = ClapTracker()                            # clap = reload, seen across frames
    people = PeopleMatcher()                         # who's who in this phone's camera (ID_MODE 'clothes')
    memory_s = TAG_MEMORY_S if ID_MODE == 'tags' else PERSON_MEMORY_S

    log.info(f'Phone connected from {request.remote}')

    try:
        async for msg in ws:
            # ── Text: register ────────────────────────────────────────────
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                # Another phone has since registered as this player (it rejoined,
                # or someone re-joined with this ID on the other team): this socket
                # no longer speaks for them
                owner = _owns(player, ws)

                if data.get('type') == 'register':
                    try:
                        pid  = int(data['player_id'])
                        team = str(data['team']).lower()
                    except (KeyError, TypeError, ValueError):
                        await ws.send_json({'error': 'bad_register'})
                        continue
                    if team not in ('red', 'blue'):
                        await ws.send_json({'error': 'invalid_team'})
                        continue
                    if not 1 <= pid <= MAX_PLAYER_ID:
                        # no tag exists for it, so nobody could ever shoot this player
                        await ws.send_json({'error': 'invalid_player_id', 'max_player_id': MAX_PLAYER_ID})
                        continue
                    player = await game.add_player(pid, team, ws)
                    people.player_id = pid
                    people.reset()
                    await ws.send_json({'type': 'registered', 'player_id': pid, 'team': team,
                                        'id_mode': ID_MODE, 'scanned': pid in gallery})
                    await _broadcast_host({'type': 'player_joined', 'player': player.to_dict()})

                elif data.get('type') == 'mic' and owner:
                    log.info(f'MIC   P{player.player_id}: {str(data.get("state"))[:60]}')

                # The phone's microphone heard a clap.  Other phones nearby hear
                # the same clap; wait CLAP_MATCH_S for their reports, then it
                # belongs to the loudest (see ClapReferee / _settle_claps).
                # Camera claps are detected in the frame loop below.
                elif data.get('type') == 'clap' and owner:
                    try:
                        peak = float(data.get('peak', 0.0))
                    except (TypeError, ValueError):
                        continue
                    log.info(f'clap heard by P{player.player_id}  peak={peak:.2f}')
                    game.claps.heard(player.player_id, peak, time.monotonic())
                    loop.call_later(CLAP_MATCH_S + 0.02, lambda: asyncio.ensure_future(_settle_claps()))
                continue

            # ── Binary: JPEG frame ────────────────────────────────────────
            if msg.type == WSMsgType.BINARY:
                if player is None:
                    await ws.send_json({'error': 'not_registered'})
                    continue
                if not _owns(player, ws):
                    # Another phone took this player over: ignore this one's frames
                    # (but reply, so its send loop doesn't wait for nothing)
                    await ws.send_json({'error': 'replaced_by_another_phone'})
                    continue
                if not game.game_active:
                    last_ready_at, last_aim = 0.0, None     # no aim carries into the next game
                    people.reset()
                    await ws.send_json({'type': 'idle', 'scores': game.scores()})
                    continue

                now = time.monotonic()
                remembered = [t for t, seen in tag_memory.values() if now - seen <= memory_s]
                det = await loop.run_in_executor(_executor, _process_frame, msg.data, remembered, people)

                if 'error' in det:
                    # Reply anyway: the phone waits for one reply per frame
                    await ws.send_json({'error': det['error']})
                    continue

                now  = time.monotonic()
                pose = det['pose']
                tag  = det['tag']   # player-role tag (possibly remembered) or None
                armed = pose >= POSE_ARMED_THRESHOLD

                for t in det['fresh_tags']:
                    tag_memory[t['id']] = (t, now)
                # One-handed only: your second hand in view cancels any pose/aim
                # in progress, so the two hands can't combine into a shot.
                two_hands = len(det['own_palms']) >= 2
                # Being down cancels them too, and nothing seen while down counts:
                # an aim from before you died must not fire the moment you respawn
                # (which can be before your next frame, when shot in your own base)
                down = not player.alive or player.deaths != seen_deaths
                seen_deaths = player.deaths
                if two_hands or down:
                    last_ready_at, last_aim = 0.0, None
                if det['ready'] and not down:
                    last_ready_at = now
                if tag and not down:
                    last_aim = (tag['id'], now)
                elif det.get('aimed_unknown'):
                    # Now pointing at someone not recognised: the aim at whoever
                    # was recognised a moment ago must not fire at this person
                    last_aim = None
                fire_ready = now - last_ready_at <= TRIGGER_WINDOW_S
                aim_ready  = last_aim is not None and now - last_aim[1] <= TRIGGER_WINDOW_S
                target_id  = last_aim[0] if aim_ready else None

                # Camera clap (backup to the mic).  No reloading while down, same
                # as a mic clap (_settle_claps).
                clapped = claps.update(det['own_palms'], now) and player.alive
                reloading = player.gun.state(now) == 'reload'
                player.gun.update(clapped, now)
                if clapped and reloading:
                    log.info(f'CLAP  P{player.player_id} reloaded  own={len(det["own_palms"])} gap={claps.last_gap}')
                gun = player.gun.state(now)   # 'reload' | 'arming' | 'ready'

                shot_event = None

                # Why this frame did / didn't fire — shown on the phone and logged.
                # A loaded gun fires on every aimed finger-gun frame until one hits.
                if not player.alive:
                    status = 'dead'
                elif gun != 'ready':
                    status = gun
                elif two_hands:
                    status = 'two_hands'
                elif fire_ready and aim_ready:
                    shot_event, status = await game.process_shot(player.player_id, target_id, now)
                elif not det['shooting']:
                    status = 'no_hand'
                elif not det['tags_seen']:
                    status = 'no_tag_visible'
                elif det.get('aimed_unknown'):
                    status = 'not_identified'
                elif not aim_ready:
                    status = 'not_aimed'
                else:
                    status = 'pose_not_ready'
                gun = player.gun.state(now)   # a hit just emptied it

                # ~2 frames/s, but EVERY frame while waiting for a reload clap
                if DEBUG_FRAME_DIR and (status == 'reload' or now - last_dump >= 0.5):
                    last_dump = now
                    name = f'p{player.player_id}_{time.strftime("%H%M%S")}_{int(now * 1000) % 1000:03d}_{status}.jpg'
                    Path(DEBUG_FRAME_DIR, name).write_bytes(msg.data)

                # Every frame while waiting for a reload clap (to tune clap detection)
                if status in ('hit', 'reload') or now - last_diag >= DIAG_LOG_INTERVAL:
                    last_diag = now
                    seen = [f'{"?" if t["id"] is None else t["id"]}{"(mem)" if t["stale"] else ""}'
                            for t in det['tags_seen']]
                    log.info(f'P{player.player_id}  status={status:<18} gun={gun:<7} '
                             f'hands={det["n_hands"]} own={len(det["own_palms"])} gap={claps.last_gap}  '
                             f'tags_seen={seen}  '
                             f'aimed={tag["id"] if tag else ("?" if det.get("aimed_unknown") else None)}  '
                             f'window_target={target_id}'
                             + (f'  matches={" ".join(det["matches"])}' if det.get('matches') else ''))

                if shot_event:
                    log.info(f'SHOT  P{player.player_id} -> P{target_id}')
                    victim = game.players.get(target_id)
                    if victim and victim.ws and not victim.ws.closed:
                        try:
                            await victim.ws.send_json(shot_event)
                        except Exception:
                            pass
                    await _broadcast_host(shot_event)

                await ws.send_json({
                    'type':          'frame_result',
                    'id_mode':       ID_MODE,
                    'scanned':       player.player_id in gallery,   # clothes mode: can others recognise you
                    'time_left':     game.scores()['time_left'],     # null: no time limit
                    'pose':          round(pose, 3),
                    'armed':         armed,
                    'aiming_tag_id': tag['id'] if tag else target_id,
                    'landmarks':     det['landmarks'],
                    'other_landmarks': det['other_landmarks'],
                    'criteria':      det['criteria'],
                    'tags_seen':     det['tags_seen'],
                    'status':        status,
                    'gun':           gun,
                    'reloads':       player.gun.reloads,   # +1 = reloaded: phone plays the reload sound
                    'arming_left':   round(player.gun.arming_left(now), 1),
                    'clapped':       clapped,
                    'fire_ready':    fire_ready,
                    'n_hands':       det['n_hands'],
                    'kills':         player.kills,
                    'hp':            player.hp,
                    'max_hp':        MAX_HP,
                    'shot_killed':   bool(shot_event and shot_event['killed']),
                    'target_hp':     shot_event['hp'] if shot_event else None,
                    'shot_fired':    shot_event is not None,
                    'alive':         player.alive,
                    'respawn_at':    player.respawn_at if not player.alive else None,
                    'remaining':     round(player.seconds_until_respawn(), 1),
                    'respawn_mode':  player.respawn_mode,
                    'flag':          game.objectives.brief(),
                    'scores':        game.scores(),
                    'frame_w':       det.get('frame_w'),
                    'frame_h':       det.get('frame_h'),
                })
                continue

            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break

    except Exception as exc:
        log.exception(f'Phone WS error: {exc}')
    finally:
        left = await game.remove_player(player.player_id, ws) if player else None
        if left:
            # 'offline': mid-game, kept (kills still count; the phone can rejoin)
            await _broadcast_host({'type': 'player_left', 'player_id': player.player_id,
                                   'kept': left == 'offline'})
            log.info(f'Player {player.player_id} disconnected ({left})')

    return ws


# ── WebSocket — Scan station ──────────────────────────────────────────────────

def _thumbnail_url(pid: int) -> str:
    # Relative, so it also works behind the viewer's /combat/ proxy; ?v=: a rescan shows at once
    return f'scans/p{pid}.jpg?v={int(gallery.scanned_at.get(pid, 0))}'


async def _finish_scan(session: ScanSession) -> dict:
    pid = session.player_id
    gallery.set(pid, session.views)
    if session.thumbnail is not None:
        th = session.thumbnail
        th = cv2.resize(th, (max(1, round(th.shape[1] * 240 / th.shape[0])), 240))
        cv2.imwrite(str(SCANS_DIR / f'p{pid}.jpg'), cv2.cvtColor(th, cv2.COLOR_RGB2BGR))
    looks_like = await asyncio.get_event_loop().run_in_executor(_executor, gallery.lookalikes, pid)
    log.info(f'SCAN  P{pid} scanned ({len(session.views)} views)'
             + (f' — looks like {", ".join(f"P{o} ({r:.0%})" for o, r in looks_like)}' if looks_like else ''))
    return {'type': 'scan_done', 'player_id': pid, 'views': len(session.views),
            'thumbnail': _thumbnail_url(pid),
            'looks_like': [{'player_id': o, 'share': r} for o, r in looks_like]}


async def ws_scan(request: web.Request) -> web.WebSocketResponse:
    """The scan station: one player at a time turns around in front of its camera."""
    ws      = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES, heartbeat=10.0)
    await ws.prepare(request)
    loop    = asyncio.get_event_loop()
    session: ScanSession | None = None
    log.info(f'Scan station connected from {request.remote}')

    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if not isinstance(data, dict):
                continue
            if data.get('type') == 'scan_start':
                try:
                    pid = int(data['player_id'])
                except (KeyError, TypeError, ValueError):
                    await ws.send_json({'error': 'bad_player_id'})
                    continue
                if not 1 <= pid <= MAX_PLAYER_ID:
                    await ws.send_json({'error': 'invalid_player_id', 'max_player_id': MAX_PLAYER_ID})
                    continue
                session = ScanSession(pid)
                log.info(f'SCAN  P{pid} started')
                await ws.send_json({'type': 'scan_started', 'player_id': pid, 'needed': SCAN_SAMPLES})
            elif data.get('type') == 'scan_stop':
                session = None
                await ws.send_json({'type': 'scan_stopped'})

        elif msg.type == WSMsgType.BINARY:
            # Reply to every frame: the page sends the next one when this one's answered
            if session is None:
                await ws.send_json({'type': 'scan_idle'})
                continue
            now = time.monotonic()
            res = await loop.run_in_executor(_executor, _scan_frame, msg.data, session.wants_view(now))
            if 'error' in res:
                await ws.send_json({'error': res['error']})
                continue
            if res['desc'] is not None:
                session.add(res['desc'], res['thumb'], now)
            if session.done:
                await ws.send_json(await _finish_scan(session))
                session = None
                continue
            await ws.send_json({
                'type':     'scan_progress',
                'player_id': session.player_id,
                'samples':  len(session.views),
                'needed':   SCAN_SAMPLES,
                'took':     res['desc'] is not None,
                'box':      res['box'],
                'hint':     ScanSession.HINTS.get(res['hint']),
                'frame_w':  res['frame_w'],
                'frame_h':  res['frame_h'],
            })

        elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
            break

    log.info(f'Scan station disconnected ({request.remote})')
    return ws


# ── WebSocket — Host ──────────────────────────────────────────────────────────

async def ws_host(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    host_sockets.add(ws)
    log.info(f'Host dashboard connected from {request.remote}')
    await ws.send_json({'type': 'state_update', **_snapshot()})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(data, dict):
                    continue
                t = data.get('type')
                if t == 'start_game':
                    await _start(data.get('win_s'), data.get('match_s'))
                elif t == 'stop_game':
                    scores = await game.stop_game()
                    await _broadcast_host({'type': 'game_over', 'scores': scores})
                elif t == 'respawn_player':
                    try:
                        pid = int(data.get('player_id'))
                    except (TypeError, ValueError):
                        continue
                    if await game.respawn_player(pid):
                        log.info(f'Player {pid} back in (by the host)')
                        await _broadcast_host({'type': 'respawned', 'player_id': pid, 'how': 'host'})
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        host_sockets.discard(ws)

    return ws


# ── REST ──────────────────────────────────────────────────────────────────────

async def _start(win_s=None, match_s=None):
    """match_s: match length in seconds (most kills when it ends wins); none: no limit."""
    await game.start_game(win_s, match_s)
    await _broadcast_host({'type': 'game_started', 'win_s': game.objectives.win_s,
                           'match_s': game.scores()['match_s']})


async def api_start(request):
    try:
        body = await request.json() if request.can_read_body else {}
    except json.JSONDecodeError:
        body = {}
    try:
        body = body if isinstance(body, dict) else {}
        await _start(body.get('win_s'), body.get('match_s'))
        return web.json_response({'ok': True, 'win_s': game.objectives.win_s})
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=500)

async def api_stop(request):
    try:
        scores = await game.stop_game()
        await _broadcast_host({'type': 'game_over', 'scores': scores})
        return web.json_response({'ok': True, 'scores': scores})
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=500)

async def api_state(request):
    return web.json_response(_snapshot())


async def api_scans(request):
    pairs = await asyncio.get_event_loop().run_in_executor(_executor, gallery.confusable)
    return web.json_response({
        'id_mode': ID_MODE,
        'needed':  SCAN_SAMPLES,
        'scans':   [{'player_id': pid, 'views': len(gallery.views(pid)), 'thumbnail': _thumbnail_url(pid)}
                    for pid in gallery.players()],
        'lookalikes': [{'a': a, 'b': b, 'share': r} for a, b, r in pairs],
    })


async def api_scan_delete(request):
    try:
        pid = int(request.match_info['pid'])
    except ValueError:
        raise web.HTTPBadRequest(text='bad player id')
    removed = gallery.remove(pid)
    (SCANS_DIR / f'p{pid}.jpg').unlink(missing_ok=True)
    if removed:
        log.info(f'SCAN  P{pid} deleted')
    return web.json_response({'ok': removed})

def _is_private(ip: str) -> bool:
    a = [int(x) for x in ip.split('.')]
    return a[0] == 10 or (a[0] == 192 and a[1] == 168) or (a[0] == 172 and 16 <= a[1] <= 31)


def _lan_ip() -> str:
    """
    The address phones on the same Wi-Fi can reach.  The default-route address
    is wrong when a VPN is up (e.g. HotspotShield hands out 100.x), so prefer a
    private LAN address from the machine's interfaces.
    """
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        route_ip = s.getsockname()[0]
        s.close()
    except Exception:
        route_ip = '127.0.0.1'
    if _is_private(route_ip):
        return route_ip
    try:
        addrs = {ai[4][0] for ai in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
    except Exception:
        addrs = set()
    private = sorted(a for a in addrs if _is_private(a))
    return private[0] if private else route_ip


async def api_network_info(request):
    ip = _lan_ip()
    return web.json_response({'ip': ip, 'port': PORT, 'https_port': HTTPS_PORT,
                              'ws': f'ws://{ip}:{PORT}/ws/phone'})


def _tag_files() -> list[Path]:
    return sorted(TAGS_DIR.glob('*.png')) if TAGS_DIR.is_dir() else []


async def tags_index(request):
    """Gallery of generated tags (run gen_tags.py first) with download links."""
    files = _tag_files()
    if not files:
        body = '<p>No tags found. Run <code>python gen_tags.py</code> on the host, then reload.</p>'
    else:
        pdf = TAGS_DIR / 'print_tags.pdf'
        body = (('<p><a class="zip" href="/tags/print_tags.pdf" download>🖨 Print PDF (front + back)</a></p>'
                 if pdf.is_file() else '') +
                '<p><a class="zip" href="/tags.zip" download>⬇ Download all (zip)</a></p>'
                '<div class="grid">' + ''.join(
                    f'<a href="/tags/{f.name}" download="{f.name}">'
                    f'<img src="/tags/{f.name}" alt="{f.stem}"><span>{f.stem}</span></a>'
                    for f in files) + '</div>')
    html = f'''<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>HackTheSkies Tags</title>
<style>
  body{{font-family:system-ui,sans-serif;background:#0f172a;color:#e2e8f0;margin:0;padding:16px}}
  a{{color:#e2e8f0;text-decoration:none}}
  .zip{{display:inline-block;background:#16a34a;color:#fff;padding:10px 16px;border-radius:8px;font-weight:600}}
  .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:12px}}
  .grid a{{background:#1e293b;border-radius:8px;padding:8px;text-align:center}}
  .grid img{{width:100%;background:#fff;border-radius:4px}}
  .grid span{{display:block;margin-top:6px;font-size:14px}}
</style></head><body>
<h1>🏷 Player tags</h1>
<p>Tap a tag to download it. To test without a printer, open it full-screen on another
device so the white border shows on all four sides.</p>
{body}</body></html>'''
    return web.Response(text=html, content_type='text/html')


async def tags_zip(request):
    files = _tag_files()
    if not files:
        raise web.HTTPNotFound(text='No tags generated yet; run gen_tags.py')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            zf.write(f, f.name)
    return web.Response(body=buf.getvalue(), content_type='application/zip', headers={
        'Content-Disposition': 'attachment; filename="hacktheskies_tags.zip"'})


# ── Background broadcaster ────────────────────────────────────────────────────

async def _state_broadcaster():
    """Runs the match (respawns at the bases, the flag, a win) and keeps the host screens up to date."""
    while True:
        await asyncio.sleep(STATE_BROADCAST_INTERVAL)
        for event in await game.tick(feed.positions(time.monotonic())):
            await _broadcast_host(event)
        if host_sockets:
            snap = _snapshot()
            snap['type'] = 'state_update'
            await _broadcast_host(snap)


# ── App lifecycle ─────────────────────────────────────────────────────────────

async def on_startup(app):
    app['broadcaster'] = asyncio.create_task(_state_broadcaster())
    app['feed'] = asyncio.create_task(feed.run()) if feed.url else None
    if DEBUG_FRAME_DIR:
        Path(DEBUG_FRAME_DIR).mkdir(parents=True, exist_ok=True)
        log.info(f'Saving debug frames to {DEBUG_FRAME_DIR}')

async def on_cleanup(app):
    app['broadcaster'].cancel()
    if app['feed']:
        app['feed'].cancel()
    _executor.shutdown(wait=False)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get('/ws/phone', ws_phone)
    app.router.add_get('/ws/host',  ws_host)
    app.router.add_get('/ws/scan',  ws_scan)
    app.router.add_post('/api/start',        api_start)
    app.router.add_post('/api/stop',         api_stop)
    app.router.add_get ('/api/state',        api_state)
    app.router.add_get ('/api/network-info', api_network_info)
    app.router.add_get ('/api/scans',        api_scans)
    app.router.add_delete('/api/scans/{pid}', api_scan_delete)
    SCANS_DIR.mkdir(exist_ok=True)
    app.router.add_static('/scans', SCANS_DIR)
    app.router.add_static('/static', STATIC_DIR)
    app.router.add_get('/tags',     tags_index)
    app.router.add_get('/tags/',    tags_index)
    app.router.add_get('/tags.zip', tags_zip)
    TAGS_DIR.mkdir(exist_ok=True)
    app.router.add_static('/tags', TAGS_DIR)
    app.router.add_get('/',      lambda r: web.FileResponse(STATIC_DIR / 'host.html'))
    app.router.add_get('/phone', lambda r: web.FileResponse(STATIC_DIR / 'phone.html'))
    app.router.add_get('/scan',  lambda r: web.FileResponse(STATIC_DIR / 'scan.html'))
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def _ssl_context(lan_ip: str):
    """
    Phone browsers only allow camera access on HTTPS (or localhost), so also
    serve HTTPS with a self-signed cert, generated with openssl on first run.
    Phones will warn about the cert once; tap through ("Advanced" -> proceed).
    """
    import shutil, ssl, subprocess
    cert, key = CERT_DIR / 'cert.pem', CERT_DIR / 'key.pem'
    if not (cert.is_file() and key.is_file()):
        openssl = shutil.which('openssl') or next(
            (p for p in (r'C:\Program Files\Git\usr\bin\openssl.exe',
                         r'C:\Program Files\Git\mingw64\bin\openssl.exe') if Path(p).is_file()), None)
        if not openssl:
            log.warning('openssl not found: HTTPS disabled, phone cameras will be blocked')
            return None
        CERT_DIR.mkdir(exist_ok=True)
        subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                        '-keyout', str(key), '-out', str(cert), '-days', '365',
                        '-subj', '/CN=hacktheskies',
                        '-addext', f'subjectAltName=IP:{lan_ip},IP:127.0.0.1,DNS:localhost'],
                       check=True, capture_output=True)
        log.info(f'Generated self-signed cert in {CERT_DIR}')
    ctx = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    ctx.load_cert_chain(cert, key)
    return ctx


async def _serve():
    lan_ip  = _lan_ip()
    ssl_ctx = _ssl_context(lan_ip)
    runner  = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, HOST, PORT).start()
    if ssl_ctx:
        await web.TCPSite(runner, HOST, HTTPS_PORT, ssl_context=ssl_ctx).start()
    log.info('=' * 60)
    log.info(f'  HackTheSkies  (players told apart by {ID_MODE}; positions from {GAME_SERVER})')
    log.info(f'  Host dashboard:  http://{lan_ip}:{PORT}/')
    if ssl_ctx:
        log.info(f'  Phone client:    https://{lan_ip}:{HTTPS_PORT}/phone   (camera needs https)')
    else:
        log.info(f'  Phone client:    http://{lan_ip}:{PORT}/phone   (no https: camera blocked)')
    if ID_MODE == 'tags':
        log.info(f'  Tag downloads:   http://{lan_ip}:{PORT}/tags')
    elif ssl_ctx:
        log.info(f'  Scan station:    https://{lan_ip}:{HTTPS_PORT}/scan   ({len(gallery.players())} scanned)')
    else:
        log.info(f'  Scan station:    http://localhost:{PORT}/scan   ({len(gallery.players())} scanned)')
    log.info('=' * 60)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


if __name__ == '__main__':
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        pass
