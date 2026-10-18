import ARKit
import AVFoundation
import CoreImage
import Observation
import UIKit

/// Finger-gun shooting. Streams the camera to the shooting server (combat/server.py), which finds
/// your finger-gun pose and the chest tags in view and decides hits; hears reload claps on the
/// microphone; and tells you what happened by voice and vibration, since the phone is strapped to
/// your chest. Same protocol as the web player page, combat/static/phone.html.
@Observable
final class CombatClient: NSObject, AVSpeechSynthesizerDelegate {
    enum Flash { case hit, hurt, down }

    var connected = false
    var registered = false
    var inGame = false
    var alive = true
    var hp = 3
    var maxHp = 3
    var gun = "ready"  // "ready" | "arming" (just reloaded) | "reload" (clap to reload)
    var status = "Connecting…"
    var kills = 0
    var score = ""
    var error: String?
    var flash: Flash?  // brief full-screen colour after a hit
    var mic = "off"  // reload-clap microphone: "listening", "no mic permission", …

    /// HTTPS port of the shooting server: run.py starts it on --combat-port (8765) and +1.
    static let port = 8766
    private static let frameInterval = 0.1  // up to 10 frames a second
    private static let replyTimeout = 1.0  // one frame in flight; give up waiting after this
    private static let maxSide: CGFloat = 1280

    private let link = WebSocketLink()
    @ObservationIgnored private let clap = ClapDetector()
    @ObservationIgnored private let voice = AVSpeechSynthesizer()
    @ObservationIgnored private let encoder = DispatchQueue(label: "combat.jpeg", qos: .userInitiated)
    @ObservationIgnored private let ci = CIContext()
    @ObservationIgnored private var inFlightSince: TimeInterval?
    @ObservationIgnored private var lastSent: TimeInterval = 0
    @ObservationIgnored private var lastReloads: Int?
    @ObservationIgnored private var lastGun: String?

    override init() {
        super.init()
        voice.delegate = self
        clap.onState = { [weak self] state in self?.mic = state }
        link.onConnected = { [weak self] up in
            guard let self else { return }
            connected = up
            if !up {
                registered = false
                inFlightSince = nil
                status = "Reconnecting to the shooting server…"
            }
        }
        link.onMessage = { [weak self] msg in self?.handle(msg) }
    }

    /// server: the game server address from the join screen; the shooting server runs on the same laptop.
    func connect(server: String, tag: Int, team: String) {
        var host = server.trimmingCharacters(in: .whitespaces)
        if let scheme = host.range(of: "://") { host = String(host[scheme.upperBound...]) }
        host = String(host.prefix { $0 != ":" && $0 != "/" })
        guard let url = URL(string: "wss://\(host):\(Self.port)/ws/phone") else {
            error = "Bad server address"
            return
        }
        error = nil
        link.connect(url: url, hello: ["type": "register", "player_id": tag, "team": team])
        clap.start { [weak self] peak in
            // Every clap is reported with its loudness: the server gives it to the phone that heard it loudest.
            self?.link.send(["type": "clap", "peak": Double(peak)])
        }
    }

    func disconnect() {
        link.disconnect()
        clap.stop()
        inGame = false
    }

    // ---------- camera frames ----------

    /// Called for every camera frame; sends one every frameInterval while no other is in flight.
    func offer(_ frame: ARFrame) {
        guard registered else { return }
        let now = frame.timestamp
        if let since = inFlightSince, now - since < Self.replyTimeout { return }
        guard now - lastSent >= Self.frameInterval else { return }
        inFlightSince = now
        lastSent = now
        let pixels = frame.capturedImage  // keep the pixel buffer, not the ARFrame
        encoder.async { [weak self] in
            guard let self else { return }
            let jpeg = encode(pixels)
            DispatchQueue.main.async {
                if let jpeg { self.link.sendData(jpeg) } else { self.inFlightSince = nil }
            }
        }
    }

    private func encode(_ pixels: CVPixelBuffer) -> Data? {
        // The camera sensor is landscape; the phone is upright on your chest.
        var image = CIImage(cvPixelBuffer: pixels).oriented(.right)
        let scale = min(1, Self.maxSide / max(image.extent.width, image.extent.height))
        if scale < 1 { image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale)) }
        let quality = [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.7]
        return ci.jpegRepresentation(of: image, colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!, options: quality)
    }

    // ---------- server messages ----------

    private func handle(_ msg: [String: Any]) {
        switch msg["type"] as? String {
        case "registered":
            registered = true
            error = nil
            status = "Joined the shooting server"
        case "frame_result":
            inFlightSince = nil
            frameResult(msg)
        case "idle":
            inFlightSince = nil
            if inGame { say("Game over") }
            inGame = false
            lastGun = nil
            status = "Waiting for the host to start the match"
        case "shot_event":
            wasHit(msg)
        default:
            if let e = msg["error"] as? String {
                inFlightSince = nil  // a frame's reply can be an error too
                error = e == "invalid_player_id" ? "Tag number must be 1–34" : e.replacingOccurrences(of: "_", with: " ")
            }
        }
    }

    private func frameResult(_ d: [String: Any]) {
        let justStarted = !inGame
        let wasAlive = alive
        inGame = true
        alive = d["alive"] as? Bool ?? true
        hp = d["hp"] as? Int ?? hp
        maxHp = d["max_hp"] as? Int ?? maxHp
        kills = d["kills"] as? Int ?? kills
        if let s = d["scores"] as? [String: Any] {
            score = "Red \(s["red_kills"] as? Int ?? 0) – \(s["blue_kills"] as? Int ?? 0) Blue"
        }
        status = Self.describe(d)

        // You can't see the screen: every change you need to know about is spoken.
        let gunNow = d["gun"] as? String ?? "ready"
        let reloads = d["reloads"] as? Int ?? 0
        if justStarted {
            say("Go!")
        } else if !wasAlive && alive {
            say("Back in")
        } else if wasAlive && alive {
            if let last = lastReloads, reloads > last {
                say("Reloaded")
            } else if gunNow == "ready" && lastGun == "arming" {
                say("Ready")
            }
        }
        lastReloads = reloads
        lastGun = gunNow
        gun = gunNow

        if d["shot_fired"] as? Bool == true {
            let target = d["aiming_tag_id"] as? Int
            let killed = d["shot_killed"] as? Bool == true
            say(killed ? "Eliminated player \(target ?? 0)" : "Hit! Clap to reload")
            UINotificationFeedbackGenerator().notificationOccurred(.success)
            show(.hit)
        }
    }

    private func wasHit(_ d: [String: Any]) {
        hp = d["hp"] as? Int ?? hp
        if d["killed"] as? Bool == true {
            alive = false
            say("You're down")
            UINotificationFeedbackGenerator().notificationOccurred(.error)
            show(.down)
        } else {
            say("Hit! \(hp) left")
            UIImpactFeedbackGenerator(style: .heavy).impactOccurred()
            show(.hurt)
        }
    }

    private func show(_ f: Flash) {
        flash = f
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.35) { [weak self] in
            if self?.flash == f { self?.flash = nil }
        }
    }

    /// Why this frame did or didn't fire (the server's status), as on the web player page.
    static func describe(_ d: [String: Any]) -> String {
        let target = d["aiming_tag_id"] as? Int
        switch d["status"] as? String {
        case "hit": return "HIT player \(target ?? 0)!"
        case "dead": return "You're down"
        case "reload": return "Clap to reload"
        case "arming": return "Reloaded · ready in a moment"
        case "no_hand": return "Ready · hold your finger gun in view"
        case "two_hands": return "Shoot with one hand"
        case "no_tag_visible": return "Ready · no tag in view"
        case "not_aimed": return "Ready · point at the tag"
        case "pose_not_ready": return "Aiming at \(target ?? 0) · curl middle/ring to fire"
        case "target_dead": return "Player \(target ?? 0) is already down"
        case "same_team": return "Player \(target ?? 0) is on your team"
        case "target_not_in_game": return "Player \(target ?? 0) hasn't joined"
        case let s?: return s
        case nil: return "–"
        }
    }

    // ---------- voice ----------

    private func say(_ text: String) {
        let u = AVSpeechUtterance(string: text)
        u.rate = 0.55
        u.volume = 1
        voice.stopSpeaking(at: .immediate)  // the newest news matters most
        voice.speak(u)
    }

    // Our own voice must not count as a clap (or its echo, a moment later).
    func speechSynthesizer(_ s: AVSpeechSynthesizer, didStart _: AVSpeechUtterance) {
        clap.speaking = true
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didFinish _: AVSpeechUtterance) {
        clap.speaking = false
        clap.mutedUntil = CACurrentMediaTime() + ClapDetector.echo
    }

    func speechSynthesizer(_ s: AVSpeechSynthesizer, didCancel _: AVSpeechUtterance) {
        clap.speaking = false
        clap.mutedUntil = CACurrentMediaTime() + ClapDetector.echo
    }
}

/// Hears reload claps: a very loud, very short sound far above the room's background level.
/// Same test and thresholds as the web player page (listenForClap in combat/static/phone.html).
final class ClapDetector {
    static let minPeak: Float = 0.5  // 1.0 = the mic's maximum (0.5: a clap right next to the phone)
    static let minCrest: Float = 4  // peak / average level over ~45 ms: how sharp the sound is
    static let overBackground: Float = 8  // peak / the room's background level
    static let gap = 0.4  // s, one clap per this long
    static let echo = 0.3  // s after our own sound ends that it can still reach the mic

    var speaking = false
    var mutedUntil = 0.0
    var onState: ((String) -> Void)?
    private var state = "off" {
        didSet { onState?(state) }
    }
    private let engine = AVAudioEngine()
    private var background: Float = 0.01
    private var lastClap = 0.0

    func start(onClap: @escaping (Float) -> Void) {
        guard !engine.isRunning else { return }
        let session = AVAudioSession.sharedInstance()
        do {
            // Raw audio: voice processing would soften exactly the sharp clap we listen for.
            try session.setCategory(.playAndRecord, mode: .measurement, options: [.defaultToSpeaker, .mixWithOthers])
            try session.setActive(true)
        } catch {
            state = "audio error"
            return
        }
        AVAudioApplication.requestRecordPermission { granted in
            DispatchQueue.main.async {
                if granted { self.listen(onClap) } else { self.state = "no mic permission" }
            }
        }
    }

    func stop() {
        guard engine.isRunning else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        state = "off"
    }

    private func listen(_ onClap: @escaping (Float) -> Void) {
        let input = engine.inputNode
        input.installTap(onBus: 0, bufferSize: 2048, format: input.outputFormat(forBus: 0)) { [weak self] buffer, _ in
            self?.look(at: buffer, onClap)
        }
        do {
            try engine.start()
            state = "listening"
        } catch {
            input.removeTap(onBus: 0)
            state = "mic error"
        }
    }

    private func look(at buffer: AVAudioPCMBuffer, _ onClap: @escaping (Float) -> Void) {
        guard let samples = buffer.floatChannelData?[0], buffer.frameLength > 0 else { return }
        let n = Int(buffer.frameLength)
        var peak: Float = 0
        var sum: Float = 0
        for i in 0..<n {
            let v = samples[i]
            peak = max(peak, abs(v))
            sum += v * v
        }
        let rms = (sum / Float(n)).squareRoot()
        let now = CACurrentMediaTime()
        let ours = speaking || now < mutedUntil
        let clap = !ours && peak >= Self.minPeak && peak >= Self.minCrest * rms
            && peak >= Self.overBackground * background && now - lastClap > Self.gap
        guard clap else {
            if !ours { background = background * 0.98 + rms * 0.02 }  // slowly follow the room's level
            return
        }
        lastClap = now
        DispatchQueue.main.async { onClap(peak) }
    }
}
