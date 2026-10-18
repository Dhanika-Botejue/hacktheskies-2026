# HackTheSkies 2026 🔫

Real-life, phone-based shooter + territory game. No special hardware — just phones and a laptop.

> Part of the main project: `run.py` at the repo root starts this server alongside the position
> tracking, and the **HTS Player** iPhone app streams its camera here (see `../SETUP.md`). The
> commands below run it on its own: run them from this `combat/` folder, with the dependencies
> from the root `requirements.txt`.

## How it works

```
Phone camera (rear-facing, chest-mounted)
   │  JPEG frames @ 10fps
   ▼
Python server  ──► MediaPipe Hands  →  finger-gun confidence (0–1)
               ──► people + clothes →  target player ID (matched against pre-game body scans)
               ──► shot logic       →  mark victim dead, notify both sides
               ──► WebSocket push   →  host dashboard + victim's phone
```

All detection runs on the **server**. The phone is a dumb camera terminal. Players are told apart
by what they're wearing (`HTS_ID_MODE=clothes`, the default), or by printed AprilTag bibs
(`HTS_ID_MODE=tags`, see "Tag ID convention").

## Quick start

```bash
# 1. Install dependencies (Python 3.10+)
pip install -r requirements.txt

# 2. Run server on host laptop
python server.py

# 3. Host dashboard  → http://<LAN-IP>:8765/
# 4. Scan station     → https://<LAN-IP>:8766/scan    (scan every player once, see below)
# 5. Each player opens → https://<LAN-IP>:8766/phone   (https: phone cameras need it;
#    tap through the self-signed certificate warning once)
```

All devices must be on the same Wi-Fi network. The server prints the exact
addresses when it starts.

## Testing

No camera, phone or running server needed — each test starts its own server:

```bash
python test_smoke.py   # unit tests: detectors, aiming, the gun, claps, scoring, clothes matching
python test_gun.py     # the whole gun end to end over real WebSockets: hits, HP,
                       # kills, mic + camera reload, whose clap it was, reconnects
python test_api.py     # real camera pipeline on real JPEGs (printed tag -> hit),
                       # web pages and downloads, the scan station, clothes -> hit
```

## Respawning (for the spawn-point code)

Respawning is a placeholder: `GameState.check_auto_respawns` brings a player back
10 s after dying. Replace it with the walk-back-to-your-spawn-point logic, and when
a player should come back call `player.respawn()` — it refills HP and reloads the
gun, and the phone plays its "Back in" sound by itself when it sees the player
alive again.

## File map

| File | Purpose |
|------|---------|
| `server.py` | aiohttp server: HTTP routes, WebSocket endpoints, frame pipeline |
| `hand_detector.py` | MediaPipe Hands → finger-gun confidence score |
| `person_id.py` | People (EfficientDet-Lite2) + their clothes (pose landmarker mask) → player ID from the body scans |
| `tag_detector.py` | pupil-apriltags → player/base/flag ID from bib tags (`HTS_ID_MODE=tags`) |
| `game_state.py` | Players, flags, shot resolution, scoring |
| `static/phone.html` | Phone client (JS only does camera → JPEG → WS) |
| `static/scan.html` | Scan station: one player at a time turns around in front of it |
| `static/host.html` | Host dashboard (scoreboard, map, player list) |

## Recognising players by their clothes

**Before the game**, every player is scanned at the scan station (`/scan`, `static/scan.html`):
prop a phone up at chest height (an iPhone: the same camera as in the game, which matters more
than anything for colours), enter the player's number, and the player turns around slowly 2–4 m
away. The server keeps `SCAN_SAMPLES` (24) views of their clothes, about 3 a second, then warns
when two players look alike: when a quarter or more of one player's views would be confused with
the other (`Gallery.lookalikes`). Scans are saved in `scans/` (kept across restarts;
`DELETE /api/scans/<id>` or the page's Delete button forgets one).

**In the game**, while one of your hands is in view, the server finds the people in your frame
(MediaPipe ObjectDetector, EfficientDet-Lite2) and drops your own arm (a box rising from the frame
bottom that starts below its middle, `OWN_ARM_TOP`). The `MAX_DESCRIBED` (2) people nearest your
finger's line are described: the pose landmarker runs on each one's crop, its segmentation mask
keeps only their own pixels (and your hand is masked out), and its shoulder/hip/knee landmarks
split them into shirt and trousers. Each part is a colour histogram (12 soft hue bins + black /
grey / white), compared by Bhattacharyya coefficient against everyone's scan (mean of the 3 best
views, `TOP_K`).

A person gets a player ID only when the best match is at least `MIN_SIM` (0.80), at least
`MIN_MARGIN` (0.05) ahead of the runner-up, **and** the same player matched clearly within
`CONFIRM_S` (0.4 s) before — a loaded gun fires on the first aimed frame, so one misread frame
must never be enough. Otherwise they're "?" on the phone, and pointing at them gives
`not_identified`: never a shot at anyone, and it also cancels an aim at someone else from a
moment ago. Everyone scanned except the shooter is a candidate, teammates included.

Tuning: every second (and on every hit) the server logs `matches=` with the two best scores of
each person described, e.g. `12=[P12:0.97 P11:0.84]` (recognised as P12) or
`?=[P3:0.86 P5:0.84]` (too close to call). Constants are at the top of `person_id.py`.

Limits: two players dressed alike (black hoodie + jeans) can't be told apart by colour — the scan
station warns; give one of them something distinctive. Someone who takes off a jacket needs a
rescan. People cut off at the frame edge, or more than ~15 m away, often can't be described.

## Shot detection logic

```
pose ≥ 0.75  →  reticle turns yellow ("armed")
finger gun (index out + middle OR ring tucked)  ┐  both within
index finger points at a recognised player      ┘  0.8 s   →  fires (every frame)
first real hit                                  →  gun empty
clap (both hands seen together)                 →  reloaded, fires again 1 s later
```

One-handed only. Your hands = the closest (biggest) hand in the frame, plus a second
of similar size (`PAIR_SIZE_RATIO`); hands too small to be yours are ignored
(`OWN_HAND_MIN_PALM`), so other players can't shoot or reload for you. With one of
your hands in view, it shoots. With both in view nothing fires, and any pose/aim in
progress is cancelled: the second hand only comes in to clap. Misses (teammate, a player
already down) don't empty the gun; only a hit does. The gun is `game_state.Gun`
(`ARM_DELAY_S`).

Reload claps are heard by the phone's **microphone** (`listenForClap` in
`static/phone.html`): a loud, sharp peak well above the room's background level
(`CLAP_MIN_PEAK`, `CLAP_MIN_CREST`, `CLAP_OVER_BG`). The phone's own sounds can't
reload it: the sharp parts of them (gunshot/impact noise, speech, the first 50 ms of
each note) block claps until 0.3 s after they end, measured by rendering every sound.
Every phone reports every clap with its loudness (`{"type":"clap","peak":…}`), and
the server gives each clap to the phone that heard it loudest
(`game_state.ClapReferee`), so a clap next to you reloads the clapper, not you. The
camera can also see a clap (backup, either one reloads) — on camera alone most
claps were missed, because pressed palms read as one hand and the clap itself is
motion-blurred. Camera claps are detected across frames by `hand_detector.ClapTracker`:
two hands close together, or two hands approaching that then merge into one hand or
blur out within 0.8 s. Tune with `CLAP_MAX_PALMS` / `CLAP_NEAR_PALMS` /
`CLAP_WINDOW_S`. While a player is waiting to reload, the server logs every frame
(`hands=`, `own=`, `gap=`) to show why a clap did or didn't register. Nobody can
reload while down.

Phones dropping off the Wi-Fi: a phone that rejoins as the same player (same ID and
team) gets that player back exactly as they were — HP, gun, kills, down or alive —
never a fresh one. A phone that disconnects mid-game stays in the game marked
offline, so its kills still count; it's noticed within ~15 s even if it vanished
silently. The next game drops players whose phones never came back.

Detection flickers frame to frame (blur, distance), so an aim at a player still counts
for `TRIGGER_WINDOW_S` (`server.py`), and a player that drops out is remembered at their
last position for `PERSON_MEMORY_S` (0.3 s; a tag for `TAG_MEMORY_S`, 0.8 s). A remembered
position that a person in this frame now covers belongs to that person. Going down, raising
your second hand, pointing at someone not recognised, or the game stopping cancels any aim
in progress.

Aiming: a 2D ray from the index knuckle through the fingertip is cast across the
frame. A person is hit when the ray passes through their body box, or close beside it
(`PERSON_AIM_SLACK`, `AIM_MIN_ANGLE_DEG` in `person_id.py`), and their middle is ahead of the
fingertip; a ray through the middle of a body beats one that grazes another, and of two
people on the ray the closer (bigger) one is in front. Tags: the tag closest to that ray
(relative to its on-screen size) is the target; tune `AIM_TOLERANCE` / `AIM_MIN_FINGER_PX` in
`tag_detector.py`. If the finger points straight at the camera, the person / tag under the
fingertip wins.

Per-finger checks shown on the phone (the pose bar; only "index extended" gates
aiming). Checked on 3D world landmarks, so any hand orientation works:
- Index finger extended
- Thumb raised away from the fist
- Middle finger curled
- Ring finger curled
- Pinky curled

## Tag ID convention

Only with `HTS_ID_MODE=tags`: players wear printed AprilTag bibs instead of being scanned.

| ID range | Meaning |
|----------|---------|
| 1–34 | Player bibs (ID = player number; tag25h9 has no tags above 34, so the server refuses higher IDs — nobody could shoot them) |
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