import ARKit
import Observation

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

    @ObservationIgnored private var origin: (x: Double, z: Double, yaw: Double)?
    @ObservationIgnored private var lastSample: (t: TimeInterval, p: TrailPoint)?
    @ObservationIgnored private var yaw = 0.0
    /// Bumped whenever tracking restarts with a new world frame, so the server re-aligns.
    @ObservationIgnored private var epoch = 0
    /// Receives every camera pose in raw AR space: (x, z, yaw, tracking, epoch, timestamp).
    @ObservationIgnored var onPose: ((Double, Double, Double, Bool, Int, Double) -> Void)?

    override init() {
        super.init()
        session.delegate = self
    }

    func start() {
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
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
        // -x toward the top edge. Facing = where the top of the phone points, tilted or flat: the
        // sum of the top edge (points forward when flat) and the back camera (forward when upright).
        let t = camera.transform
        let fx = Double(-t.columns.0.x - t.columns.2.x), fz = Double(-t.columns.0.z - t.columns.2.z)
        if hypot(fx, fz) > 0.3 {  // keep the last heading if the phone points straight up or down
            yaw = (atan2(fx, -fz) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
        }
        let x = Double(t.columns.3.x), z = Double(t.columns.3.z)
        onPose?(x, z, yaw, tracking, epoch, frame.timestamp)
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
