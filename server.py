"""
server.py
=========
HackTheSkies — Main aiohttp WebSocket + HTTP server.

What it does
------------
  1. Serves phone.html to players' phones  (GET /phone)
  2. Serves host.html to the host laptop   (GET /)
  3. Accepts binary JPEG frames from each phone over WebSocket (/ws/phone)
  4. Runs MediaPipe Hands + AprilTag in a thread pool on each frame
  5. Detects shots; notifies shooter, victim, and host dashboard
  6. Pushes live game state to the host dashboard every 500 ms (/ws/host)

WebSocket protocol — phone
--------------------------
  Phone → Server:  JSON  {"type":"register","player_id":7,"team":"red"}
  Phone → Server:  BINARY  <raw JPEG bytes>  (repeated, ~10 fps)
  Server → Phone:  JSON  frame_result  (after every frame)
  Server → Phone:  JSON  shot_event    (push, when this player is hit)
  Server → Phone:  JSON  respawned     (push, after successful respawn)

WebSocket protocol — host
--------------------------
  Host → Server:   JSON  {"type":"add_flag","flag_id":0,"x":0.3,"y":0.4}
  Host → Server:   JSON  {"type":"start_game"}  |  {"type":"stop_game"}
  Server → Host:   JSON  state_update  (every 500 ms)
  Server → Host:   JSON  shot_event    (push)
  Server → Host:   JSON  game_started | game_over

HTTP REST
---------
  POST /api/start            Start game
  POST /api/stop             Stop game
  POST /api/flag             Body: {flag_id, x, y}
  GET  /api/state            Full game snapshot (JSON)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Dict, Set

import cv2
import numpy as np
from aiohttp import web, WSMsgType

from game_state import GameState
from hand_detector import HandDetector
from tag_detector import TagDetector

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(name)s  %(message)s',
)
log = logging.getLogger('hacktheskies')

# ── Config ───────────────────────────────────────────────────────────────────
HOST          = '0.0.0.0'
PORT          = int(os.environ.get('PORT', 8765))
STATIC_DIR    = Path(__file__).parent / 'static'

# Shot thresholds
POSE_ARMED_THRESHOLD = 0.75   # show "armed" indicator on phone
POSE_FIRE_THRESHOLD  = 0.85   # actually fire the shot

# Seconds of continuous finger-gun aim needed to capture a flag tag
FLAG_CAPTURE_DWELL   = 1.5

# How often (seconds) we push state to the host dashboard
STATE_BROADCAST_INTERVAL = 0.5

# Max JPEG size we'll accept (2 MB should be generous for any phone camera)
MAX_FRAME_BYTES = 2 * 1024 * 1024

# ── Thread-local detector instances ──────────────────────────────────────────
# MediaPipe and pupil-apriltags are not thread-safe, so each worker thread
# gets its own detector.

_tlocal = threading.local()

def _hand_detector() -> HandDetector:
    if not hasattr(_tlocal, 'hand'):
        _tlocal.hand = HandDetector(model_complexity=0)
    return _tlocal.hand

def _tag_detector() -> TagDetector:
    if not hasattr(_tlocal, 'tag'):
        _tlocal.tag = TagDetector(quad_decimate=2.0)
    return _tlocal.tag

# Thread pool for CPU-heavy frame processing
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix='frame-worker')

# ── Global state ─────────────────────────────────────────────────────────────
game         = GameState()
host_sockets: Set[web.WebSocketResponse] = set()


# ============================================================================
# Frame processing  (runs in the thread pool — not the event loop)
# ============================================================================

def _process_frame(jpeg_bytes: bytes) -> dict:
    """
    Decode JPEG → run MediaPipe Hands + AprilTag detection.

    Returns a dict with all detection results.
    This function MUST be thread-safe (no shared mutable state).
    """
    # Decode JPEG to BGR
    arr = np.frombuffer(jpeg_bytes, dtype=np.uint8)
    bgr = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if bgr is None:
        return {'error': 'jpeg_decode_failed'}

    h, w = bgr.shape[:2]

    # MediaPipe needs RGB; AprilTag needs grayscale
    rgb  = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)

    # Run detectors (thread-local instances).
    # No role filter — we detect player bibs, flag tags, AND base tags.
    # The WebSocket handler branches on tag['role'].
    hand_result = _hand_detector().process(rgb)
    tag         = _tag_detector().best_centered_tag(gray, w, h)

    return {
        'pose':      hand_result['confidence'],
        'armed':     hand_result['armed'],
        'detected':  hand_result['detected'],
        'landmarks': hand_result['landmarks'],
        'criteria':  hand_result['criteria'],
        'tag':       tag,   # full dict {id, role, center, corners, margin} or None
    }


# ============================================================================
# Broadcast helpers
# ============================================================================

async def _broadcast_host(data: dict):
    """Send JSON to every connected host dashboard, drop dead sockets."""
    dead = set()
    for ws in list(host_sockets):
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    host_sockets -= dead


# ============================================================================
# WebSocket — Phone  (/ws/phone)
# ============================================================================

async def ws_phone(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse(max_msg_size=MAX_FRAME_BYTES)
    await ws.prepare(request)

    player       = None
    loop         = asyncio.get_event_loop()

    # Per-connection flag-capture dwell state
    # { 'flag_id': int | None, 'aim_start': float }
    aim_state = {'flag_id': None, 'aim_start': 0.0}

    log.info(f'Phone connected from {request.remote}')

    try:
        async for msg in ws:

            # ── Text: registration or respawn request ─────────────────────
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get('type')

                if msg_type == 'register':
                    pid  = int(data['player_id'])
                    team = str(data['team']).lower()
                    if team not in ('red', 'blue'):
                        await ws.send_json({'error': 'invalid_team'})
                        continue
                    player = await game.add_player(pid, team, ws)
                    await ws.send_json({
                        'type':      'registered',
                        'player_id': pid,
                        'team':      team,
                    })
                    await _broadcast_host({
                        'type':   'player_joined',
                        'player': player.to_dict(),
                    })

                elif msg_type == 'respawn_request':
                    # Phone scanned base tag and asks to respawn
                    base_tag_id = int(data.get('base_tag_id', -1))
                    if player:
                        ok = await game.request_respawn(player.player_id, base_tag_id)
                        if ok:
                            await ws.send_json({'type': 'respawned'})
                        else:
                            remaining = player.seconds_until_respawn()
                            await ws.send_json({
                                'type':      'respawn_denied',
                                'remaining': round(remaining, 1),
                            })

                elif msg_type == 'flag_capture':
                    # Phone scanned a flag tag while standing in zone
                    if player:
                        flag_tag_id = int(data.get('flag_tag_id', -1))
                        flag_id     = flag_tag_id - 200  # see tag_detector.py
                        if flag_id >= 0:
                            await game.capture_flag(flag_id, player.team)
                            await _broadcast_host({
                                'type':    'flag_captured',
                                'flag_id': flag_id,
                                'team':    player.team,
                            })

                continue

            # ── Binary: JPEG frame ────────────────────────────────────────
            if msg.type == WSMsgType.BINARY:
                if player is None:
                    await ws.send_json({'error': 'not_registered'})
                    continue
                if not game.game_active:
                    # Still reply so the phone stays live; just skip detection
                    await ws.send_json({
                        'type':   'idle',
                        'scores': game.scores(),
                    })
                    continue

                # Off-load CPU work to thread pool
                det = await loop.run_in_executor(_executor, _process_frame, msg.data)

                if 'error' in det:
                    log.debug(f'Frame decode error from player {player.player_id}')
                    continue

                # ── Interpret detection results ─────────────────────────────
                pose = det['pose']
                tag  = det['tag']           # full tag dict or None
                role = tag['role'] if tag else None

                armed = pose >= POSE_ARMED_THRESHOLD
                fired = pose >= POSE_FIRE_THRESHOLD

                shot_event      = None
                capture_event   = None
                respawn_ok      = False
                capture_progress = 0.0   # 0.0–1.0, only relevant for 'flag' role

                # ── Branch on what the camera sees ──────────────────────────
                if tag and role == 'player' and player.alive and fired:
                    # ── SHOOT a player ──────────────────────────────────────
                    shot_event = await game.process_shot(player.player_id, tag['id'])
                    if shot_event:
                        log.info(
                            f'SHOT  player {player.player_id} -> player {tag["id"]}'
                            f'  (pose={pose:.2f})'
                        )
                        victim = game.players.get(tag['id'])
                        if victim and victim.ws and not victim.ws.closed:
                            try:
                                await victim.ws.send_json(shot_event)
                            except Exception:
                                pass
                        await _broadcast_host(shot_event)
                    aim_state = {'flag_id': None, 'aim_start': 0.0}

                elif tag and role == 'flag' and player.alive and armed:
                    # ── CAPTURE a flag (dwell mechanic) ─────────────────────
                    flag_id = tag['id'] - 200   # tag 200 -> flag 0, etc.
                    if aim_state['flag_id'] == flag_id:
                        # Continuing to aim at the same flag
                        elapsed          = time.time() - aim_state['aim_start']
                        capture_progress = min(1.0, elapsed / FLAG_CAPTURE_DWELL)
                        if elapsed >= FLAG_CAPTURE_DWELL:
                            await game.capture_flag(flag_id, player.team)
                            capture_event = {
                                'type':    'flag_captured',
                                'flag_id': flag_id,
                                'team':    player.team,
                                'by':      player.player_id,
                            }
                            log.info(
                                f'FLAG {flag_id} captured by {player.team} '
                                f'(player {player.player_id})'
                            )
                            await _broadcast_host(capture_event)
                            aim_state = {'flag_id': None, 'aim_start': 0.0}
                    else:
                        # Started aiming at a new flag — reset dwell timer
                        aim_state = {'flag_id': flag_id, 'aim_start': time.time()}
                        capture_progress = 0.0

                elif tag and role == 'base' and not player.alive and armed:
                    # ── RESPAWN at base ──────────────────────────────────────
                    respawn_ok = await game.request_respawn(player.player_id, tag['id'])
                    if respawn_ok:
                        log.info(f'Player {player.player_id} respawned at base {tag["id"]}')
                        await _broadcast_host({
                            'type':      'player_respawned',
                            'player_id': player.player_id,
                        })
                    aim_state = {'flag_id': None, 'aim_start': 0.0}

                else:
                    # Not aiming at anything actionable — reset flag dwell
                    if role != 'flag':
                        aim_state = {'flag_id': None, 'aim_start': 0.0}

                # ── Reply to this phone ──────────────────────────────────────
                alive = player.alive
                await ws.send_json({
                    'type':             'frame_result',
                    # Hand detection
                    'pose':             round(pose, 3),
                    'armed':            armed,
                    # What we're aiming at
                    'aiming_role':      role,        # 'player'|'flag'|'base'|None
                    'aiming_tag_id':    tag['id']      if tag else None,
                    'tag_corners':      tag['corners'] if tag else None,
                    'landmarks':        det['landmarks'],
                    # Action results
                    'shot_fired':       shot_event    is not None,
                    'capture_event':    capture_event,
                    'capture_progress': round(capture_progress, 3),
                    'respawned':        respawn_ok,
                    # Player status
                    'alive':            alive,
                    'respawn_at':       player.respawn_at if not alive else None,
                    'remaining':        round(player.seconds_until_respawn(), 1),
                    # Scores
                    'scores':           game.scores(),
                })
                continue

            # ── Connection closed ─────────────────────────────────────────
            if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break

    except Exception as exc:
        log.exception(f'Phone WS error: {exc}')

    finally:
        if player:
            await game.remove_player(player.player_id)
            await _broadcast_host({'type': 'player_left', 'player_id': player.player_id})
            log.info(f'Player {player.player_id} disconnected')

    return ws


# ============================================================================
# WebSocket — Host  (/ws/host)
# ============================================================================

async def ws_host(request: web.Request) -> web.WebSocketResponse:
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    host_sockets.add(ws)
    log.info(f'Host dashboard connected from {request.remote}')

    # Send current state immediately on connect
    await ws.send_json({'type': 'state_update', **game.snapshot()})

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue

                msg_type = data.get('type')

                if msg_type == 'add_flag':
                    await game.add_flag(
                        int(data['flag_id']),
                        float(data['x']),
                        float(data['y']),
                    )
                    await _broadcast_host({'type': 'flag_added', **data})

                elif msg_type == 'start_game':
                    await game.start_game()
                    await _broadcast_host({'type': 'game_started'})

                elif msg_type == 'stop_game':
                    scores = await game.stop_game()
                    await _broadcast_host({'type': 'game_over', 'scores': scores})

            elif msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break

    finally:
        host_sockets.discard(ws)
        log.info('Host dashboard disconnected')

    return ws


# ============================================================================
# REST endpoints
# ============================================================================

async def api_flag(request: web.Request) -> web.Response:
    body = await request.json()
    await game.add_flag(int(body['flag_id']), float(body['x']), float(body['y']))
    return web.json_response({'ok': True})

async def api_start(request: web.Request) -> web.Response:
    await game.start_game()
    await _broadcast_host({'type': 'game_started'})
    return web.json_response({'ok': True})

async def api_stop(request: web.Request) -> web.Response:
    scores = await game.stop_game()
    await _broadcast_host({'type': 'game_over', 'scores': scores})
    return web.json_response({'ok': True, 'scores': scores})

async def api_state(request: web.Request) -> web.Response:
    return web.json_response(game.snapshot())

async def api_network_info(request: web.Request) -> web.Response:
    """Returns the server's LAN IP so the phone can auto-fill the URL."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
    except Exception:
        ip = '127.0.0.1'
    return web.json_response({'ip': ip, 'port': PORT, 'ws': f'ws://{ip}:{PORT}/ws/phone'})


# ============================================================================
# Background task — periodic state broadcast + auto-respawn
# ============================================================================

async def _state_broadcaster():
    while True:
        await asyncio.sleep(STATE_BROADCAST_INTERVAL)
        await game.check_auto_respawns()
        if host_sockets:
            snap = game.snapshot()
            snap['type'] = 'state_update'
            await _broadcast_host(snap)


# ============================================================================
# App lifecycle
# ============================================================================

async def on_startup(app: web.Application):
    app['broadcaster'] = asyncio.create_task(_state_broadcaster())
    # Print access URLs
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        lan_ip = s.getsockname()[0]
        s.close()
    except Exception:
        lan_ip = 'localhost'

    log.info('═' * 55)
    log.info(f'  HackTheSkies server running')
    log.info(f'  Host dashboard:  http://{lan_ip}:{PORT}/')
    log.info(f'  Phone client:    http://{lan_ip}:{PORT}/phone')
    log.info('═' * 55)

async def on_cleanup(app: web.Application):
    app['broadcaster'].cancel()
    _executor.shutdown(wait=False)


def build_app() -> web.Application:
    app = web.Application()

    # WebSocket routes
    app.router.add_get('/ws/phone', ws_phone)
    app.router.add_get('/ws/host',  ws_host)

    # REST
    app.router.add_post('/api/flag',         api_flag)
    app.router.add_post('/api/start',        api_start)
    app.router.add_post('/api/stop',         api_stop)
    app.router.add_get ('/api/state',        api_state)
    app.router.add_get ('/api/network-info', api_network_info)

    # Static files
    app.router.add_static('/static', STATIC_DIR)

    # Convenience page routes
    app.router.add_get('/',      lambda r: web.FileResponse(STATIC_DIR / 'host.html'))
    app.router.add_get('/phone', lambda r: web.FileResponse(STATIC_DIR / 'phone.html'))

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    return app


if __name__ == '__main__':
    web.run_app(build_app(), host=HOST, port=PORT)
