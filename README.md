# hacktheskies-2026

Real-world capture the flag. Step 1: **scan the building with an iPhone → Gaussian-splat map → spectate it on the laptop.**

**New here? Follow [SETUP.md](SETUP.md)** for the full how-to-run guide: laptop, both iPhone apps, scanning, and playing.

```
iPhone app (ARKit)  ── Scan Map → Done → auto-upload ──▶  laptop server  ──▶  Brush trains a Gaussian splat (Metal)
 photos + camera poses + LiDAR points                        viewer/server        ──▶ map appears in the viewer, sharpening live
```

## Laptop (viewer + training server)

```sh
tools/setup.sh                 # once: downloads the Brush splat trainer into tools/bin
cd viewer && npm install
npm run dev                    # http://localhost:5173
```

The viewer shows a QR code (top right). Phone and laptop must be on the same Wi-Fi.

Keys: click to look · WASD move · Q/E down/up · Shift fast · scroll = speed · **F** walk/fly · **M** top-down overview (drag to pan, scroll to zoom).

Env vars: `TRAIN_STEPS` (default 15000), `EXPORT_EVERY` (preview interval, 2500), `MAX_RESOLUTION` (1440).

Maps are stored in `viewer/maps/` (gitignored). You can also drag a `.ply`/`.spz` from another app (e.g. Scaniverse) onto the viewer.

## iPhone app (`app-ios/`)

Needs Xcode and an iPhone with LiDAR (12 Pro or newer Pro) for the best results.

1. Open `app-ios/HackTheSkies.xcodeproj` in Xcode.
2. Target → Signing & Capabilities → pick your Team (if the bundle id is taken, change it).
3. Plug in the iPhone, select it, Run. (First time: on the iPhone, Settings → General → VPN & Device Management → trust your developer certificate, and enable Developer Mode.)
4. Scan the viewer's QR code with the iPhone Camera → opens the app connected to the laptop.
5. **Scan Map** → walk slowly → **Done**. The map shows up in the viewer.

After editing `app-ios/project.yml`, regenerate the project with XcodeGen (`xcodegen generate` in `app-ios/`).

## Playing on the scanned map

The viewer is also the host's game screen: it walks through the scan first-person like a shooter
map and shows every player live, positioned by the game server (`run.py`, see `ios/README.md`).

```sh
.venv/bin/python run.py                 # game server: phones connect here (https://<laptop>:8443)
cd viewer && npm run dev                # viewer at http://localhost:5173, proxies the game server at /game/
```

- The active map becomes the game map. The viewer sends the game server the scan's footprint
  and a top-down floor plan, so the phones, the 2D host page (`/web/host.html`) and the tracker
  all use the scan's floor coordinates (game `x, y` = viewer `X, Z` minus a fixed origin).
- Players join from the **HTS Player** app (`ios/Player.swiftpm`) or `/web/phone.html` on Android.
- Place each player once: **Place** in the Players panel, then click where they stand and the
  way they face on the top-down map. You can also walk to their spot in first person, look the
  way they face and press **P**.
- Walls drawn on the 2D host page help the step-counting fallback. They are kept when the map
  sharpens or the floor height changes; **Flip/Rotate** starts the game map over (place players again).

Keys: click to play · WASD · Space jump · Shift sprint · **F** fly · **M** top-down map ·
**1–9** watch a player (**V** over the shoulder, WASD takes over) · hold **Tab** for the
player list · **−/=** minimap zoom · Esc for the menus.

`GAME_SERVER` overrides where the viewer finds the game server (default `https://127.0.0.1:8443`).

## Shooting (`combat/`)

Finger guns and chest tags: each player wears a printed AprilTag (its number = their player
number) and a phone on their chest. The **HTS Player** app sends the same camera frames it tracks
with to the shooting server (`combat/server.py`, started by `run.py` on ports 8765/8766), which
finds a one-handed finger gun pointed at a tag and decides hits: 3 hits down a player, clap to
reload. How it works and how to tune it: [combat/README.md](combat/README.md).

The viewer joins the two servers by tag number (proxied at `/game/` and `/combat/`): avatars in
team colours with hearts, tracers from shooter to target, a kill feed, the score, and the
**Match** panel to start and end a match. **Key positions** (+ Add or **K**, then click the
top-down map) are stored with the game map and show which team is standing in them; scoring for
holding them is still to come. `COMBAT_SERVER` overrides where the viewer finds the shooting
server (default `http://127.0.0.1:8765`).

## Testing without a phone

```sh
python3 tools/fake_scan.py /tmp/fakeroom --zip      # synthetic room in the same format the app uploads
curl -X POST --data-binary @/tmp/fakeroom.zip "localhost:5173/api/scans?name=Fake%20room"
```
