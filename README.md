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
index out + middle OR ring tucked             ┐  both within the last
index finger points roughly at a player tag   ┘  0.8 s  →  SHOT FIRED
```

Detection flickers frame to frame (blur, distance), so the pose and the aim don't
have to land in the same frame (`TRIGGER_WINDOW_S` in `server.py`), and a tag that
drops out is remembered at its last position for `TAG_MEMORY_S`. The thumb and pinky
are shown on the phone but not required (`ready_to_fire` in `hand_detector.py`) —
they read noisily when the hand is edge-on to the camera.

Aiming: a 2D ray from the index knuckle through the fingertip is cast across the
frame; the player tag closest to that ray (relative to its on-screen size) is the
target. If the finger points straight at the camera, the tag under the fingertip wins.
Tune `AIM_TOLERANCE` / `AIM_MIN_FINGER_PX` in `tag_detector.py`.

Criteria for "finger gun" — thumbs-up with the index finger out (each worth 0.2).
Checked on 3D world landmarks, so any hand orientation works:
- Index finger extended
- Thumb raised away from the fist
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

The game uses the **tag25h9** family (`TAG_FAMILY` in `tag_detector.py`): bigger cells
than tag36h11 at the same print size, so it reads ~25% farther under motion blur —
which matters indoors. It has IDs 0–34, so only player bibs fit (no bases/flags).

Generate printable bibs (verified against the detector):

```bash
python gen_tags.py                     # players 1-10 -> ./tags/
python gen_tags.py --ids 1 2 3 --size 2400   # print resolution
```

Print as large as people can wear (A3 / tabloid), keep the white border, mount on
card so the tag stays flat, and use matte paper to avoid glare from indoor lights.

## Tuning

Edit the constants at the top of `server.py`:

```python
POSE_ARMED_THRESHOLD = 0.75   # show armed reticle
TRIGGER_WINDOW_S     = 0.8    # pose and aim may be this far apart in time
TAG_MEMORY_S         = 0.8    # how long a missed tag is still aimable
```

Edit `game_state.py` for:
```python
RESPAWN_DURATION = 20.0   # seconds dead
SHOT_COOLDOWN    = 2.5    # seconds between shots
```