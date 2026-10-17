# HackTheSkies 2026 🔫

Real-life, phone-based shooter + territory game. No special hardware — just phones and a laptop.

## How it works

```
Phone camera (rear-facing, chest-mounted)
   │  JPEG frames @ 10fps
   ▼
Python server  ──► MediaPipe Hands  →  finger-gun confidence (0–1)
               ──► AprilTag detect  →  target player ID (from bib)
               ──► shot logic       →  mark victim dead, notify both sides
               ──► WebSocket push   →  host dashboard + victim's phone
```

All detection runs on the **server**. The phone is a dumb camera terminal.

## Quick start

```bash
# 1. Install dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. Run server on host laptop
python server.py

# 3. Host dashboard  → http://<LAN-IP>:8765/
# 4. Each player opens → http://<LAN-IP>:8765/phone
```

All devices must be on the same Wi-Fi network.

## File map

| File | Purpose |
|------|---------|
| `server.py` | aiohttp server: HTTP routes, WebSocket endpoints, frame pipeline |
| `hand_detector.py` | MediaPipe Hands → finger-gun confidence score |
| `tag_detector.py` | pupil-apriltags → player/base/flag ID from bib tags |
| `game_state.py` | Players, flags, shot resolution, scoring |
| `static/phone.html` | Phone client (JS only does camera → JPEG → WS) |
| `static/host.html` | Host dashboard (scoreboard, map, player list) |

## Shot detection logic

```
pose ≥ 0.75  →  reticle turns yellow ("armed")
pose ≥ 0.85  AND  AprilTag visible  →  SHOT FIRED
```

Criteria for "finger gun" (each worth 0.2):
- Index finger extended
- Thumb extended
- Middle finger curled
- Ring finger curled
- Pinky curled

## Tag ID convention

| ID range | Meaning |
|----------|---------|
| 1–99 | Player bibs (ID = player number) |
| 100 | Red team base |
| 101 | Blue team base |
| 200+ | Flag markers (index = ID − 200) |

Print tag36h11 family tags. Generator: https://github.com/AprilRobotics/apriltag-imgs

## Tuning

Edit the constants at the top of `server.py`:

```python
POSE_ARMED_THRESHOLD = 0.75   # show armed reticle
POSE_FIRE_THRESHOLD  = 0.85   # actually fire
```

Edit `game_state.py` for:
```python
RESPAWN_DURATION = 20.0   # seconds dead
SHOT_COOLDOWN    = 2.5    # seconds between shots
```