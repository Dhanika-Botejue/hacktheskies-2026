"""
server.py  (simplified — shoot-only)
======================================
HackTheSkies: finger-gun + AprilTag shooting game.

WebSocket protocol — phone (/ws/phone)
---------------------------------------
  Phone -> Server:  JSON  {"type":"register","player_id":7,"team":"red"}
                    (player_id 1..MAX_PLAYER_ID, the IDs that have a tag; re-registering
                    the same ID + team, e.g. after a Wi-Fi drop, rejoins as that player)
  Phone -> Server:  JSON  {"type":"clap","peak":0.83}   (mic heard a clap; sent for
                    every clap, so the server can tell whose it was — see ClapReferee)
  Phone -> Server:  JSON  {"type":"mic","state":"listening"}   (for the log)
  Phone -> Server:  BINARY  <raw JPEG bytes>  (~10 fps)
  Server -> Phone:  JSON  registered | {"error": ...}   (reply to register)
  Server -> Phone:  JSON  frame_result | idle | {"error": ...}   (reply to every frame)
  Server -> Phone:  JSON  shot_event     (push, when this player is hit)

WebSocket protocol — host (/ws/host)
--------------------------------------
  Host -> Server:  JSON  {"type":"start_game"} | {"type":"stop_game"}
  Server -> Host:  JSON  state_update  (every 500 ms)
  Server -> Host:  JSON  shot_event    (push)
  Server -> Host:  JSON  game_started | game_over

REST
-----
  POST /api/start         Start game
  POST /api/stop          Stop game
  GET  /api/state         Full game snapshot
  GET  /api/network-info  Returns LAN IP for phone auto-fill
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
from hand_detector import HandDetector, INDEX_MCP, INDEX_TIP, ClapTracker, own_palms, shooting_hand
from tag_detector import MAX_PLAYER_ID, TagDetector, pick_aimed_tag

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s  %(message)s',
)
log = logging.getLogger('hacktheskies')

HOST  = '0.0.0.0'
PORT  = int(os.environ.get('PORT', 8765))
STATIC_DIR = Path(__file__).parent / 'static'
TAGS_DIR   = Path(__file__).parent / 'tags'   # written by gen_tags.py
CERT_DIR   = Path(__file__).parent / 'certs'  # self-signed HTTPS cert (gitignored)
HTTPS_PORT = int(os.environ.get('HTTPS_PORT', PORT + 1))

POSE_ARMED_THRESHOLD = 0.75   # pose bar colour on the phone (information only)
# One-handed finger gun (game_state.Gun): while loaded it fires on any frame
# with a finger-gun pose aimed at a tag, until one hits; then clap to reload.
# Detection flickers frame to frame (blur, distance), so the pose and the aim
# each still count for TRIGGER_WINDOW_S (not necessarily the same frame), and
# tags that drop out are remembered at their last position for TAG_MEMORY_S.
TRIGGER_WINDOW_S     = 0.8
TAG_MEMORY_S         = 0.8
MAX_FRAME_BYTES      = 2 * 1024 * 1024
STATE_BROADCAST_INTERVAL = 0.5
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

_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='frame-worker')

game         = GameState()
host_sockets: Set[web.WebSocketResponse] = set()


# ── Frame processing (runs in thread pool) ────────────────────────────────────

def _process_frame(jpeg_bytes: bytes, remembered: list[dict]) -> dict:
    """remembered: player tags seen in recent frames but possibly missed in this
    one (blur etc.); used for aiming if not re-detected, marked stale=True."""
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        return {'error': 'jpeg_decode_failed'}

    h, w = bgr.shape[:2]
    rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    hand_result = _hand_detector().process(rgb)
    # Only look for player tags (IDs 1-99)
    fresh = [t for t in _tag_detector().detect(gray) if t['role'] == 'player']
    fresh_ids = {t['id'] for t in fresh}
    tags = fresh + [{**t, 'stale': True} for t in remembered if t['id'] not in fresh_ids]

    # One hand shoots: the one closest to the camera (biggest in the frame).
    # Its index finger aims along the knuckle -> fingertip ray.
    hands = hand_result['hands']
    shooter = shooting_hand(hands, w, h)
    tag = None
    if shooter and shooter['criteria']['index_extended']:
        lms     = shooter['landmarks']
        knuckle = (lms[INDEX_MCP][0] * w, lms[INDEX_MCP][1] * h)
        tip     = (lms[INDEX_TIP][0] * w, lms[INDEX_TIP][1] * h)
        tag     = pick_aimed_tag(tags, knuckle, tip)

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
        'fresh_tags': fresh,
        'tags_seen': [{'id': t['id'], 'corners': t['corners'], 'stale': t.get('stale', False)}
                      for t in tags],
        'frame_w':   w,
        'frame_h':   h,
    }


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


def _owns(player, ws) -> bool:
    """Does this socket still speak for this player?  Not once another phone has
    registered as them, nor if their record was replaced (same ID, other team)."""
    return player is not None and player.ws is ws and game.players.get(player.player_id) is player


async def _broadcast_host(data: dict):
    dead = set()
    for ws in list(host_sockets):
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    host_sockets.difference_update(dead)


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
    claps = ClapTracker()                            # clap = reload, seen across frames

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
                    await ws.send_json({'type': 'registered', 'player_id': pid, 'team': team})
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
                    await ws.send_json({'type': 'idle', 'scores': game.scores()})
                    continue

                now = time.monotonic()
                remembered = [t for t, seen in tag_memory.values() if now - seen <= TAG_MEMORY_S]
                det = await loop.run_in_executor(_executor, _process_frame, msg.data, remembered)

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
                # Being down cancels them too: an aim from before you died must
                # not fire the moment you respawn
                if two_hands or not player.alive:
                    last_ready_at, last_aim = 0.0, None
                if det['ready']:
                    last_ready_at = now
                if tag:
                    last_aim = (tag['id'], now)
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
                    seen = [f'{t["id"]}{"(mem)" if t["stale"] else ""}' for t in det['tags_seen']]
                    log.info(f'P{player.player_id}  status={status:<18} gun={gun:<7} '
                             f'hands={det["n_hands"]} own={len(det["own_palms"])} gap={claps.last_gap}  '
                             f'tags_seen={seen}  '
                             f'aimed={tag["id"] if tag else None}  window_target={target_id}')

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


# ── WebSocket — Host ──────────────────────────────────────────────────────────

async def ws_host(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    host_sockets.add(ws)
    log.info(f'Host dashboard connected from {request.remote}')
    await ws.send_json({'type': 'state_update', **game.snapshot()})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                t = data.get('type')
                if t == 'start_game':
                    await game.start_game()
                    await _broadcast_host({'type': 'game_started'})
                elif t == 'stop_game':
                    scores = await game.stop_game()
                    await _broadcast_host({'type': 'game_over', 'scores': scores})
            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break
    finally:
        host_sockets.discard(ws)

    return ws


# ── REST ──────────────────────────────────────────────────────────────────────

async def api_start(request):
    try:
        await game.start_game()
        await _broadcast_host({'type': 'game_started'})
        return web.json_response({'ok': True})
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
    return web.json_response(game.snapshot())

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
    while True:
        await asyncio.sleep(STATE_BROADCAST_INTERVAL)
        await game.check_auto_respawns()
        if host_sockets:
            snap = game.snapshot()
            snap['type'] = 'state_update'
            await _broadcast_host(snap)


# ── App lifecycle ─────────────────────────────────────────────────────────────

async def on_startup(app):
    app['broadcaster'] = asyncio.create_task(_state_broadcaster())
    if DEBUG_FRAME_DIR:
        Path(DEBUG_FRAME_DIR).mkdir(parents=True, exist_ok=True)
        log.info(f'Saving debug frames to {DEBUG_FRAME_DIR}')

async def on_cleanup(app):
    app['broadcaster'].cancel()
    _executor.shutdown(wait=False)


def build_app() -> web.Application:
    app = web.Application()
    app.router.add_get('/ws/phone', ws_phone)
    app.router.add_get('/ws/host',  ws_host)
    app.router.add_post('/api/start',        api_start)
    app.router.add_post('/api/stop',         api_stop)
    app.router.add_get ('/api/state',        api_state)
    app.router.add_get ('/api/network-info', api_network_info)
    app.router.add_static('/static', STATIC_DIR)
    app.router.add_get('/tags',     tags_index)
    app.router.add_get('/tags/',    tags_index)
    app.router.add_get('/tags.zip', tags_zip)
    TAGS_DIR.mkdir(exist_ok=True)
    app.router.add_static('/tags', TAGS_DIR)
    app.router.add_get('/',      lambda r: web.FileResponse(STATIC_DIR / 'host.html'))
    app.router.add_get('/phone', lambda r: web.FileResponse(STATIC_DIR / 'phone.html'))
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
    log.info(f'  HackTheSkies  (shoot-only mode)')
    log.info(f'  Host dashboard:  http://{lan_ip}:{PORT}/')
    if ssl_ctx:
        log.info(f'  Phone client:    https://{lan_ip}:{HTTPS_PORT}/phone   (camera needs https)')
    else:
        log.info(f'  Phone client:    http://{lan_ip}:{PORT}/phone   (no https: camera blocked)')
    log.info(f'  Tag downloads:   http://{lan_ip}:{PORT}/tags')
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
