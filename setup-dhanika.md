# Setup notes (Dhanika's laptop)

How we got the project running on this Mac, and the problems we hit on the way. The full team guide is
[SETUP.md](SETUP.md); this file covers what was different here.

## What runs where

Everything runs from the **`main`** branch. Two servers have to run at the same time, each in its own terminal:

| Server | Command | Port | Used by |
|---|---|---|---|
| Game server (Python) | `.venv/bin/python run.py` | `https://<laptop-ip>:8443` | **HTS Player** app, `/web/host.html` |
| Viewer (Node/TypeScript) | `cd viewer && npm run dev` | `http://<laptop-ip>:5173` | **HackTheSkies** scanner app, laptop browser |

Find the laptop IP with `ipconfig getifaddr en0` (it was `10.110.0.193` on the event Wi-Fi). `run.py` and the
viewer also print it when they start.

## One-time setup

### 1. Python 3.10 or newer

The Mac's default `python3` is Xcode's **Python 3.9**, which is too old: `run.py` crashes with

```
TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
```

because the code uses `X | None` type hints (Python 3.10+). We installed [uv](https://docs.astral.sh/uv/) and
used it to build the virtualenv on Python 3.12:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh     # installs ~/.local/bin/uv
cd ~/Projects/hacktheskies-2026
rm -rf .venv
~/.local/bin/uv venv --python 3.12
~/.local/bin/uv pip install --python .venv/bin/python -r requirements.txt
```

### 2. Viewer and splat trainer

```sh
tools/setup.sh                 # downloads Brush into tools/bin (skip if tools/bin/brush_app exists)
cd viewer && npm install       # re-run after every pull that changes viewer/package.json
```

### 3. iPhone apps

Build from Xcode as described in [SETUP.md](SETUP.md#iphone-apps). On the iPhone, allow
**Settings → Privacy & Security → Local Network** for both apps.

## Every session

```sh
# Terminal 1: game server
cd ~/Projects/hacktheskies-2026
.venv/bin/python run.py

# Terminal 2: viewer
cd ~/Projects/hacktheskies-2026/viewer
npm run dev
```

Then:

- Laptop: open http://localhost:5173.
- **HackTheSkies** (scanning): `http://<laptop-ip>:5173`. Use `http`, not `https`, and port **5173**, not 8443.
- **HTS Player** (playing): `<laptop-ip>:8443`.

## Problems we hit

| Symptom | Cause | Fix |
|---|---|---|
| `run.py` crashes with `unsupported operand type(s) for \|` | `.venv` built on Python 3.9 | Rebuild `.venv` on Python 3.12 (see above) |
| HackTheSkies: "Can't reach the laptop. Same Wi-Fi? Is the viewer running?" | App pointed at `:8443`, or the viewer (`npm run dev`) wasn't running | Run **both** servers; use `http://<laptop-ip>:5173` in the app |
| `main` had no viewer / TypeScript | Viewer hadn't been merged into `main` yet | `git pull` (it's on `main` now) |
| Red dot stays red after fixing things | The app only re-checks when the address changes or the app restarts | Force-quit and reopen the app |
| Safari shows "This connection is not private" on `:8443` | `run.py` uses a self-signed certificate | Show Details → visit this website (expected) |

### Is it the network or the app?

Open `http://<laptop-ip>:5173/api/maps` in **Safari on the phone**.

- **Shows JSON:** the network and viewer are fine. The problem is in the app: wrong address, Local Network
  permission off, or a stale red dot (force-quit the app).
- **Doesn't load:** check that the phone is on the same Wi-Fi as the laptop. Event and campus Wi-Fi sometimes
  blocks devices from reaching each other; switch the laptop to the phone's Personal Hotspot and restart both
  servers (the IP changes).

## Scanning

Each scan creates a **separate new map** and makes it the active game map; scans don't merge. To cover more
area, re-scan the whole play area in one walk. After a new scan, place players again.
