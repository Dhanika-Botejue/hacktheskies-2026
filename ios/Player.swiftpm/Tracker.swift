import ARKit
import Observation

/// Which way the wearer faces, from the horizontal parts of the back camera's direction and of the
/// phone's top edge. While the camera looks roughly level (strapped to the chest, portrait OR
/// landscape) that's the back camera alone; the top edge takes over as the phone tilts flat, where
/// the camera looks at the floor. (Adding the top edge in always pulled the heading 45° sideways
/// when the phone was worn in landscape.)
func facingDirection(back: (Double, Double), top: (Double, Double)) -> (Double, Double) {
    let level = hypot(back.0, back.1)  // 1: the camera looks straight ahead, 0: straight down
    let topShare = max(0, min(1, (0.7 - level) / 0.7))
    return (back.0 + topShare * top.0, back.1 + topShare * top.1)
}

/// A point on the trail in meters: x right, y down ("up" = direction faced at start).
struct TrailPoint {
    var x: Double
    var y: Double
}

/// Follows the phone with ARKit world tracking and turns its motion into a top-down trail.
@Observable
final class Tracker: NSObject, ARSessionDelegate {
    let session = ARSession()

    var path: [TrailPoint] = []
    var pos = TrailPoint(x: 0, y: 0)
    var heading: Double?  // degrees clockwise from the starting direction
    var distance = 0.0
    var speed = 0.0
    var tracking = false
    var status = "Starting… move the phone slowly"
    var lens = "1x"  // which camera ARKit runs on: "0.5x" (ultra-wide) if this phone allows it

    @ObservationIgnored private var origin: (x: Double, z: Double, yaw: Double)?
    @ObservationIgnored private var lastSample: (t: TimeInterval, p: TrailPoint)?
    @ObservationIgnored private var yaw = 0.0
    /// Bumped whenever tracking restarts with a new world frame, so the server re-aligns.
    @ObservationIgnored private var epoch = 0
    /// Receives every camera pose in raw AR space: (x, z, yaw, tracking, epoch, timestamp).
    @ObservationIgnored var onPose: ((Double, Double, Double, Bool, Int, Double) -> Void)?
    /// Receives every camera frame: the shooting server looks at them for finger guns and tags.
    @ObservationIgnored var onFrame: ((ARFrame) -> Void)?

    override init() {
        super.init()
        session.delegate = self
    }

    func start() {
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        // Autofocus locks onto the nearest thing, your own hand, and blurs the chest tags you aim
        // at. Fixed focus keeps the room sharp: tags are much fussier about blur than hands are.
        config.isAutoFocusEnabled = false
        // A wider view keeps your hand and the tags you aim at in frame together. World tracking
        // normally only offers the 1x camera; use the 0.5x ultra-wide on phones whose ARKit offers it.
        if let wide = ARWorldTrackingConfiguration.supportedVideoFormats.first(where: { $0.captureDeviceType == .builtInUltraWideCamera }) {
            config.videoFormat = wide
            lens = "0.5x"
        } else {
            lens = "1x"
        }
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
        origin = nil
        epoch += 1
    }

    func sessionInterruptionEnded(_ session: ARSession) {
        epoch += 1  // the world frame may not survive an interruption
    }

    /// Restart the trail from where the phone is now, facing "up".
    func reset() {
        origin = nil
    }

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let camera = frame.camera
        switch camera.trackingState {
        case .normal:
            tracking = true
            status = "Tracking"
        case .notAvailable:
            tracking = false
            status = "Tracking not available"
        case .limited(let reason):
            tracking = false
            switch reason {
            case .initializing: status = "Starting… move the phone slowly"
            case .excessiveMotion: status = "Tracking lost: moving too fast"
            case .insufficientFeatures: status = "Tracking lost: point at something with texture"
            case .relocalizing: status = "Relocalizing…"
            @unknown default: status = "Tracking limited"
            }
        }

        // World space: x right, y up, -z forward. Camera axes are in landscape: -z out of the back,
        // -x toward the top edge (in portrait).
        let t = camera.transform
        let (fx, fz) = facingDirection(back: (Double(-t.columns.2.x), Double(-t.columns.2.z)),
                                       top: (Double(-t.columns.0.x), Double(-t.columns.0.z)))
        if hypot(fx, fz) > 0.3 {  // keep the last heading if the phone points straight up or down
            yaw = (atan2(fx, -fz) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
        }
        let x = Double(t.columns.3.x), z = Double(t.columns.3.z)
        onPose?(x, z, yaw, tracking, epoch, frame.timestamp)
        onFrame?(frame)
        guard tracking else { return }

        if origin == nil {
            origin = (x, z, yaw)
            path = [TrailPoint(x: 0, y: 0)]
            distance = 0
            speed = 0
            lastSample = nil
        }
        guard let o = origin else { return }

        // Rotate world space so the starting direction points up on the trail.
        let dx = x - o.x, dz = z - o.z, phi = -o.yaw * .pi / 180
        let p = TrailPoint(x: dx * cos(phi) - dz * sin(phi), y: dx * sin(phi) + dz * cos(phi))
        pos = p
        heading = (yaw - o.yaw + 360).truncatingRemainder(dividingBy: 360)

        if let last = path.last {
            let moved = hypot(p.x - last.x, p.y - last.y)
            if moved > 0.1 {
                path.append(p)
                distance += moved
            }
        }

        let now = frame.timestamp
        if let s = lastSample {
            if now - s.t > 0.3 {
                let v = hypot(p.x - s.p.x, p.y - s.p.y) / (now - s.t)
                speed = speed * 0.6 + v * 0.4
                lastSample = (now, p)
            }
        } else {
            lastSample = (now, p)
        }
    }
}
