import CoreLocation
import CoreMotion
import Foundation
import Observation

/// Connects to the game server and streams this phone's sensors to it: camera
/// poses (from Tracker), steps, a gyro heading and the compass. The server fuses them.
@Observable
final class GameClient: NSObject, CLLocationManagerDelegate {
    var connected = false
    var placed = false  // the server has a position for us
    var source = "–"  // what the server is tracking us with
    var steps = 0
    var error: String?

    private let link = WebSocketLink()
    private let motion = CMMotionManager()
    private let location = CLLocationManager()
    @ObservationIgnored private var detector: StepDetector!
    @ObservationIgnored private var gyro: Double?
    @ObservationIgnored private var compass: Double?
    @ObservationIgnored private var lastPoseSent = 0.0
    @ObservationIgnored private var headingTimer: Timer?
    @ObservationIgnored private var cameraRunning = false
    let playerId: String

    override init() {
        let defaults = UserDefaults.standard
        if let id = defaults.string(forKey: "playerId") {
            playerId = id
        } else {
            playerId = UUID().uuidString
            defaults.set(playerId, forKey: "playerId")
        }
        super.init()
        detector = StepDetector { [weak self] heading, length in
            guard let self else { return }
            steps = detector.count
            link.send(["t": "step", "heading": heading, "len": length, "src": cameraRunning ? "ar" : "gyro"])
        }
        link.onConnected = { [weak self] up in self?.connected = up }
        link.onMessage = { [weak self] msg in self?.handle(msg) }
        location.delegate = self
        location.headingOrientation = .portrait
    }

    /// tag: the number on your chest tag (1–34), which the shooting server knows you by.
    func connect(server: String, name: String, tag: Int, team: String) {
        let address = server.trimmingCharacters(in: .whitespaces)
        guard let url = URL(string: address.contains("://") ? address : "wss://\(address)/ws") else {
            error = "Bad server address"
            return
        }
        error = nil
        link.connect(url: url, hello: ["role": "player", "id": playerId, "name": name, "platform": "ios",
                                       "tag": tag, "team": team])
        startSensors()
    }

    func disconnect() {
        link.disconnect()
        motion.stopDeviceMotionUpdates()
        location.stopUpdatingHeading()
        headingTimer?.invalidate()
    }

    /// Called by Tracker for every camera frame. yaw is the camera heading in AR space.
    func cameraPose(x: Double, z: Double, yaw: Double, tracking: Bool, epoch: Int, time: Double) {
        cameraRunning = true
        detector.addHeading(yaw)
        guard time - lastPoseSent >= 1.0 / 30 else { return }
        lastPoseSent = time
        link.send(["t": "pose", "x": x, "z": z, "yaw": yaw, "tracking": tracking, "epoch": epoch])
    }

    // ---------- sensors ----------

    private func startSensors() {
        if motion.isDeviceMotionAvailable, !motion.isDeviceMotionActive {
            motion.deviceMotionUpdateInterval = 1.0 / 60
            // Gyro + accelerometer only: smooth, unaffected by magnets, arbitrary zero.
            motion.startDeviceMotionUpdates(using: .xArbitraryCorrectedZVertical, to: .main) { [weak self] m, _ in
                guard let self, let m else { return }
                gyro = Self.cameraHeading(m.attitude.quaternion)
                if !cameraRunning, let gyro { detector.addHeading(gyro) }
                let g = 9.81
                detector.addMotion(ax: (m.gravity.x + m.userAcceleration.x) * g,
                                   ay: (m.gravity.y + m.userAcceleration.y) * g,
                                   az: (m.gravity.z + m.userAcceleration.z) * g,
                                   t: m.timestamp)
            }
        }
        if CLLocationManager.headingAvailable() {
            location.startUpdatingHeading()
        }
        headingTimer?.invalidate()
        headingTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            guard let self else { return }
            var msg: [String: Any] = ["t": "heading"]
            if let gyro { msg["gyro"] = gyro }
            if let compass { msg["compass"] = compass }
            link.send(msg)
        }
    }

    func locationManager(_ manager: CLLocationManager, didUpdateHeading newHeading: CLHeading) {
        compass = newHeading.headingAccuracy >= 0 ? newHeading.magneticHeading : nil
    }

    /// Heading (degrees clockwise) the top of the phone points, from a CoreMotion attitude whose
    /// reference frame has z up. Same definition as Tracker's yaw: top edge + back camera, so it
    /// works upright, tilted and flat.
    static func cameraHeading(_ q: CMQuaternion) -> Double {
        let (x, y, z, w) = (q.x, q.y, q.z, q.w)
        // Device +y (top edge) plus device -z (out of the back), rotated into the reference frame.
        let hx = 2 * (x * y - w * z) - 2 * (x * z + w * y)
        let hy = 1 - 2 * (x * x + z * z) - 2 * (y * z - w * x)
        // Reference frame: x "north", y "west" (z up), so east = -y.
        return (atan2(-hy, hx) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
    }

    // ---------- server messages ----------

    private func handle(_ msg: [String: Any]) {
        guard msg["t"] as? String == "state", let players = msg["players"] as? [[String: Any]],
              let me = players.first(where: { $0["id"] as? String == playerId }) else { return }
        placed = me["calibrated"] as? Bool ?? false
        source = me["source"] as? String ?? "–"
    }
}

/// WebSocket with automatic reconnect. Trusts the dev server's self-signed certificate.
final class WebSocketLink: NSObject, URLSessionDelegate {
    var onConnected: ((Bool) -> Void)?
    var onMessage: (([String: Any]) -> Void)?

    private var session: URLSession!
    private var task: URLSessionWebSocketTask?
    private var url: URL?
    private var hello: [String: Any] = [:]
    private var connected = false

    override init() {
        super.init()
        session = URLSession(configuration: .default, delegate: self, delegateQueue: .main)
    }

    func connect(url: URL, hello: [String: Any]) {
        self.url = url
        self.hello = hello
        task?.cancel(with: .goingAway, reason: nil)
        open()
    }

    func disconnect() {
        url = nil
        task?.cancel(with: .goingAway, reason: nil)
        task = nil
        setConnected(false)
    }

    func send(_ msg: [String: Any]) {
        if connected { write(msg) }
    }

    /// Binary message (a camera frame for the shooting server).
    func sendData(_ data: Data) {
        if connected { task?.send(.data(data)) { _ in } }
    }

    private func open() {
        guard let url else { return }
        let t = session.webSocketTask(with: url)
        task = t
        t.resume()
        write(hello)
        receive(t)
    }

    private func write(_ msg: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: msg),
              let text = String(data: data, encoding: .utf8) else { return }
        task?.send(.string(text)) { _ in }
    }

    private func receive(_ t: URLSessionWebSocketTask) {
        t.receive { [weak self] result in
            guard let self, t === self.task else { return }
            switch result {
            case .success(let message):
                setConnected(true)
                if case .string(let text) = message,
                   let obj = try? JSONSerialization.jsonObject(with: Data(text.utf8)) as? [String: Any] {
                    onMessage?(obj)
                }
                receive(t)
            case .failure:
                setConnected(false)
                task = nil
                DispatchQueue.main.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                    if let self, task == nil { open() }
                }
            }
        }
    }

    private func setConnected(_ up: Bool) {
        guard up != connected else { return }
        connected = up
        onConnected?(up)
    }

    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }
}
