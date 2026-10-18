# Setup & how to run

Real-world capture the flag: scan the play area with an iPhone into a 3D Gaussian-splat map, then
watch players move through it live on the laptop, first-person like a shooter map. Players wear
a phone on their chest and shoot each other with finger guns; a quick body scan before the game
lets the cameras recognise who's who by their clothes.

```
HackTheSkies app ──scan──▶ viewer (Node, :5173) ──trains──▶ 3D map ──▶ game map
HTS Player app   ──pose───▶ game server (Python, :8443) ──live positions──┐
                 ──camera─▶ shooting server (Python, :8765/:8766)         ├──▶ viewer (spectator)
                            finger gun pointed at a recognised player = hit┘
```

The laptop is the spectator: it shows the scanned map, every player on it, their hit points, shots
as tracers between shooter and target, the score, and the game: each team's base, where its downed
players come back in, and the flag in the middle that teams fight to hold.

## One-time setup

### Laptop (Mac, Apple Silicon)

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # game + shooting servers
tools/setup.sh                                                       # Brush splat trainer -> tools/bin
cd viewer && npm install                                             # viewer
```

The servers need **Python 3.10 or newer** (`python3 --version`). A Mac's built-in `python3` may be
Xcode's 3.9: then make the venv with [uv](https://docs.astral.sh/uv/) instead:
`uv venv --python 3.12 && uv pip install -r requirements.txt`.

You also need **Xcode** (Mac App Store) to install the iPhone apps.

### Chest tags

Every player wears a printed AprilTag on their chest: its number is their player number, and it's
what other players' cameras aim at. Print `combat/tags/print_tags.pdf` (players 1–10; also in the
viewer's **Get ready → Print tags**) as large as people can wear, on matte paper, mounted on card so
it stays flat. More numbers (up to 34): `cd combat && ../.venv/bin/python gen_tags.py --ids 11 12 --pdf`.

Recognising players by their clothes instead (no tags, a scan station before the game) is also
built in: start the servers with `HTS_ID_MODE=clothes`. It's less reliable when outfits are alike.

### iPhone apps

There are two apps. Build each from Xcode with the iPhone plugged in.

| App | Open in Xcode | Used for |
|---|---|---|
| **HackTheSkies** | `app-ios/HackTheSkies.xcodeproj` | Scanning the map |
| **HTS Player** | `ios/Player.swiftpm` | Playing: streams your position and camera, hears reload claps |

For each app:

1. Open it in Xcode.
2. Signing & Capabilities → **Team** = your Apple ID (Xcode → Settings → Accounts; a free account works). If the bundle ID is taken, change it to something unique.
3. Pick your iPhone as the run destination and press **⌘R**.

First time only, on the iPhone:

- Settings → Privacy & Security → **Developer Mode** on, then restart.
- Settings → General → VPN & Device Management → trust your developer certificate.
- Allow **camera**, **microphone** (HTS Player: reload claps) and **local network** access when asked.

Apps signed with a free Apple ID stop opening after 7 days. Run them from Xcode again to fix that.

> **Don't commit your signing settings.** Choosing your Team or changing the bundle ID in Xcode
> edits a checked-in file (for HTS Player: `ios/Player.swiftpm/Package.swift`, which stores
> `teamIdentifier` and `bundleIdentifier`). Pushing that breaks everyone else's build. Tell git to
> ignore your local edits to it, once per clone:
>
> ```sh
> git update-index --skip-worktree ios/Player.swiftpm/Package.swift
> ```
>
> It then stays out of `git status`, `git add -A` and `git commit -a`. Before pulling a teammate's
> real change to that file, undo it with `git update-index --no-skip-worktree ios/Player.swiftpm/Package.swift`,
> pull, and set your Team again. (The scanner app has the same issue in
> `app-ios/HackTheSkies.xcodeproj/project.pbxproj`: use the same command on that file.)

A LiDAR iPhone (12 Pro or newer Pro) gives the best scans; other iPhones still work.

## Every session

The phone and laptop must be on the **same Wi-Fi**. Guest and campus networks often block devices
from reaching each other; a phone hotspot works as a fallback.

Start both servers, each in its own terminal:

```sh
.venv/bin/python run.py          # game + shooting servers; prints the address phones join with, e.g. 10.110.0.189:8443
cd viewer && npm run dev         # viewer; open http://localhost:5173 on the laptop
```

`run.py` starts the shooting server (`combat/server.py`) too, on ports 8765 (viewer) and 8766
(phones). `--no-combat` skips it.

If macOS asks whether Python or Node may accept incoming connections, click **Allow**.

## 1. Scan the map

1. Open HackTheSkies and connect it to the laptop, either way:
   - **QR code:** point the regular Camera app at the QR code in the viewer (top right), tap the yellow `hacktheskies://` banner, then tap **Open**.
   - **By hand:** tap **Enter address manually** and type the URL shown under the QR code, e.g. `http://10.110.0.189:5173`.

   A green dot next to the address means it's connected.
2. Tap **Scan Map** and walk the whole play area slowly. Cover the floor and walls, and slow down when the app says so.
3. Tap **Done**. The scan uploads and trains on the laptop. A rough preview appears in about a
   minute and sharpens until training finishes (15,000 steps, several minutes).

## 2. Make it the game map

Keep the viewer (http://localhost:5173) open with the scan selected. Within a few seconds the feed says
**Game map is now "…"**: the game server now uses the scan's floor, and the 2D host page shows it as
"from 3D scan".

If the map looks wrong, press **Esc** and use **Adjust map**:

- **Flip** if it's upside down.
- **Floor ▲ / ▼** if the eye height is off.
- **Rotate 90°** to turn it.

Do this before placing players. Flip and Rotate reset player positions and walls.

## 3. Join and place players

0. **Tags:** every player wears their printed chest tag (see **Chest tags**), each a different
   number. (Only with `HTS_ID_MODE=clothes`: scan each player at `https://<laptop IP>:8766/scan`
   instead; they stand 2–4 m away and turn around slowly.)
1. Open **HTS Player** and enter:
   - a name;
   - the game server address from `run.py` (e.g. `10.110.0.189:8443`);
   - **Tag #**: the number on the tag you're wearing (1–34);
   - your **team** (Red or Blue).

   Tap **Join**. The second line of the screen shows the shooting server: hearts, gun state and a
   green dot once it's joined.
2. Strap the phone to your chest, screen facing you, back camera forward.
   Stand still at a spot that's easy to recognise in the scan, like a corner or a doorway.
3. In the viewer's **Players** panel, click **Place**. On the top-down map, click where the player stands, then click the direction they face.
   - Or: walk to their spot in the 3D view, look the way they face, and press **P**.
4. The phone shows **In game · camera**, and the player moves live on the laptop in their team's colour.

A player with no tag or team shows "can't be shot" in the Players panel.

## 4. Play

**The game:** two teams, a base each. The team with the most kills when the time runs out wins
(30 s by default).

1. Put down the bases: in the viewer's **Bases** panel click **Place** next to **Red base**, then
   click the top-down map where it goes. Same for **Blue base**. **−/+** changes a radius: make them
   generous, since tracked positions can be a metre or so off. **Move** puts one somewhere else.
2. In the **Match** panel, set the **match length** (default 30 s), then click **Start match**. Every
   phone says "Go!", the panel counts down, and the phones say "10 seconds left". The panel warns if a
   base is missing or the shooting server can't see positions.
3. Shoot: make a finger gun in front of your chest camera, one hand, and point it at another
   player's chest tag. A hit takes one of their 3 hearts and empties your gun; **clap** near your
   phone to reload.
   The phone says what happened ("Hit! Clap to reload", "You're down. Back to your base", "Back in"),
   since you can't see its screen.
4. **Down?** Walk back into your team's base: you're back in the moment you step inside. (Players
   the game can't locate come back after 10 s. If someone's position is wrong and they can't get
   back in, click **Respawn** next to them in the Players panel.)
5. **Time's up:** the team with more kills wins (equal kills: a tie), and every phone says who won.
6. On the laptop: shots fly as tracers between the two players, hit players flash, downed ones lie
   down, and the Match panel shows the countdown and the kills. Hold **Tab** for the scoreboard.
   **End match** ends it early: the team with more kills wins.

(The shooting server still supports capture the flag, for later: see `combat/README.md`.)

## Viewer controls

| Key | Action |
|---|---|
| Click | Play (captures the mouse) · **Esc** brings back the menus |
| WASD · Space · Shift | Move · jump · sprint |
| F | Fly / walk |
| M | Top-down map (drag to pan, scroll to zoom) |
| 1–9 | Watch a player through their eyes · **V** over the shoulder · WASD takes over |
| Tab (hold) | Scoreboard: team, HP, kills/deaths |
| − / = | Minimap zoom |
| P | Place the selected player where you stand |

The 2D host page is at `https://localhost:8443/web/host.html` (accept the certificate warning). Use it
to draw walls, which help tracking when it falls back to step counting.

## Testing without phones

```sh
# Fake scan in the format the app uploads (needs: pip install pillow numpy)
python3 tools/fake_scan.py /tmp/fakeroom --zip
curl -X POST --data-binary @/tmp/fakeroom.zip "localhost:5173/api/scans?name=Fake%20room"

# Simulated players (they walk the demo floor's coordinates)
.venv/bin/python -m sim.simulate --players 3
# ...on teams, joined to the shooting server: they count for the flag (put it on their path)
.venv/bin/python -m sim.simulate --players 4 --teams

# Tests
.venv/bin/python -m pytest -q
(cd combat && ../.venv/bin/python test_smoke.py && ../.venv/bin/python test_gun.py && ../.venv/bin/python test_api.py && ../.venv/bin/python test_objectives.py)
(cd viewer && npm run typecheck)
```

The shooting server also has its own phone page for Android or testing, with no position
tracking: `https://<laptop-ip>:8766/phone`. See `combat/README.md` for how shooting works and how to
tune it.

For quicker training while testing, start the viewer with `TRAIN_STEPS=2000 EXPORT_EVERY=500 npm run dev`.

## Troubleshooting

| Problem | Fix |
|---|---|
| HackTheSkies says "Not connected to a laptop" | The QR link didn't reach the app. Use **Enter address manually** with the URL under the QR code. |
| Red dot next to the laptop address | Same Wi-Fi? Is the viewer running? Is Local Network allowed for the app in iPhone Settings? Check that `http://<laptop-ip>:5173/api/maps` loads in the iPhone's Safari. |
| HTS Player won't connect | The address must end in `:8443`, `run.py` must be running, and the app needs Local Network permission. |
| Viewer footer says "game server offline" | Start `run.py`. If it runs elsewhere, set `GAME_SERVER=https://host:8443` before `npm run dev`. |
| Scan finished but the game map didn't change | Open or refresh http://localhost:5173 with the scan selected. The viewer sends the map to the game server. |
| Player drifts or faces the wrong way | Place them again. Drawing walls on the 2D host page also helps the step-counting fallback. |
| Training failed | Click the map, check the error, and make sure `tools/setup.sh` ran (`tools/bin/brush_app` exists). |
| `run.py` crashes with `unsupported operand type(s) for \|` | The venv is on Python 3.9. Rebuild it on 3.10+ (see One-time setup). |
| Match panel says "Shooting server offline" | `run.py` prints why it didn't start it (missing packages: `pip install -r requirements.txt`). |
| Red dot on HTS Player's shooting line | The shooting server is on the same laptop at port 8766; check `run.py` started it and the address is right. |
| Shots never land | The target must be scanned (the combat dashboard at http://localhost:8765 marks unscanned players ⚠️) and in view of the shooter's camera, finger pointing at them. The web player page's status line says what's missing ("no player in view", "can't tell who that is yet", "curl middle/ring to fire"); the shooting server's log shows each match score (`matches=`). |
| Shots land on the wrong player | Their outfits are too alike: rescan one of them wearing something distinctive. The scan station warns about look-alikes. |
| Clap doesn't reload | Allow the microphone for HTS Player; clap right next to the phone. The camera also sees claps as a backup. |
