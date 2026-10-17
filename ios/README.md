# iPhone app

Safari has no web AR, so iPhones run camera tracking (ARKit) in a native app.
Android phones use `web/phone.html` in Chrome instead.

## HTS Player (`Player.swiftpm`)

Tracks where you walk with ARKit (camera + motion sensors) and streams it to
the game server together with step counts, a gyro heading and the compass.
The screen also draws your own walked trail, for checking accuracy.

### Run it on your iPhone

1. Install **Xcode** from the Mac App Store.
2. Open `ios/Player.swiftpm` in Xcode (double-click it, or File → Open).
3. Plug in your iPhone and pick it as the run destination at the top of the window.
4. Signing: click the **Player** package in the left sidebar → target **HTS Player** → *Signing & Capabilities* → set **Team** to your Apple ID (add it under Xcode → Settings → Accounts; a free account works).
5. Press **Run** (⌘R).
6. First time only:
   - On the iPhone, turn on Settings → Privacy & Security → **Developer Mode** and restart.
   - Trust your developer certificate under Settings → General → VPN & Device Management.
   - Allow camera and local network access when asked.

Apps installed with a free Apple ID stop opening after 7 days; just run them from Xcode again.

### Join a game

1. Start the server on the laptop (`.venv/bin/python run.py`); it prints the laptop's address.
2. In the app, enter your name and that address (e.g. `10.110.0.189:8443`), then tap **Join**.
3. The host places you on the map: on the laptop's host page, click your name, click where you're standing, then click the direction you're facing.
