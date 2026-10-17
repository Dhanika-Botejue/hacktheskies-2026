# hacktheskies-2026

Real-world capture the flag. Step 1: **scan the building with an iPhone → Gaussian-splat map → spectate it on the laptop.**

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

## Testing without a phone

```sh
python3 tools/fake_scan.py /tmp/fakeroom --zip      # synthetic room in the same format the app uploads
curl -X POST --data-binary @/tmp/fakeroom.zip "localhost:5173/api/scans?name=Fake%20room"
```
