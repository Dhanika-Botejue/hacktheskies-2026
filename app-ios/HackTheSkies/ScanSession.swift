import ARKit
import Combine

/// Runs ARKit while the user walks around and picks keyframes for the Gaussian-splat dataset.
/// ARKit supplies the camera pose (metres, gravity-aligned) for every frame; the photos plus
/// those poses are what the laptop trains on.
final class ScanSession: NSObject, ObservableObject, ARSessionDelegate {
    @Published private(set) var keyframes = 0
    @Published private(set) var points = 0
    @Published private(set) var hint: String?

    let session = ARSession()
    let hasLiDAR = ARWorldTrackingConfiguration.supportsFrameSemantics(.sceneDepth)

    // Take a new keyframe after moving this far or turning this much since the last one.
    private let minTranslation: Float = 0.2
    private let minRotation: Float = 12 * .pi / 180
    // Skip frames while the phone moves fast (motion blur).
    private let maxAngularSpeed: Float = 1.0 // rad/s
    private let maxLinearSpeed: Float = 1.0 // m/s

    private let queue = DispatchQueue(label: "scan.writer", qos: .userInitiated)
    private var writer: DatasetWriter?
    private var busy = false
    private var lastKey: simd_float4x4?
    private var prev: (pose: simd_float4x4, time: TimeInterval)?
    private var angularSpeed: Float = 0
    private var linearSpeed: Float = 0

    func start() throws {
        writer = try DatasetWriter()
        let config = ARWorldTrackingConfiguration()
        config.worldAlignment = .gravity
        if hasLiDAR { config.frameSemantics.insert(.sceneDepth) }
        session.delegate = self
        session.run(config, options: [.resetTracking, .removeExistingAnchors])
    }

    /// Stops capturing and writes the dataset once all pending keyframes are saved.
    func finish() async throws -> URL {
        session.pause()
        guard let writer else { throw ScanError.noFrames }
        return try await withCheckedThrowingContinuation { cont in
            queue.async { cont.resume(with: Result { try writer.finish() }) }
        }
    }

    func cancel() {
        session.pause()
        if let dir = writer?.dir { try? FileManager.default.removeItem(at: dir) }
    }

    // MARK: ARSessionDelegate (called on the main queue)

    func session(_ session: ARSession, didUpdate frame: ARFrame) {
        let pose = frame.camera.transform
        updateSpeeds(pose: pose, time: frame.timestamp)

        switch frame.camera.trackingState {
        case .notAvailable:
            hint = "Tracking unavailable"
            return
        case .limited(let reason):
            hint = switch reason {
            case .initializing: "Move the phone slowly to start"
            case .excessiveMotion: "Slow down"
            case .insufficientFeatures: "Point at more detailed areas"
            case .relocalizing: "Go back to where you were"
            @unknown default: "Tracking limited"
            }
            return
        case .normal:
            break
        }
        if angularSpeed > maxAngularSpeed || linearSpeed > maxLinearSpeed {
            hint = "Slow down"
            return
        }
        hint = nil

        if let last = lastKey,
           simd_distance(position(last), position(pose)) < minTranslation,
           rotationAngle(last, pose) < minRotation {
            return
        }
        guard !busy, let writer else { return }

        busy = true
        lastKey = pose
        let kf = DatasetWriter.Keyframe(
            image: frame.capturedImage,
            depth: frame.sceneDepth?.depthMap,
            confidence: frame.sceneDepth?.confidenceMap,
            intrinsics: frame.camera.intrinsics,
            pose: pose
        )
        queue.async { [weak self] in
            do { try writer.add(kf) } catch { print("keyframe failed: \(error)") }
            let (count, pts) = (writer.frameCount, writer.pointCount)
            DispatchQueue.main.async {
                self?.busy = false
                self?.keyframes = count
                self?.points = pts
            }
        }
    }

    private func updateSpeeds(pose: simd_float4x4, time: TimeInterval) {
        defer { prev = (pose, time) }
        guard let prev, time > prev.time else { return }
        let dt = Float(time - prev.time)
        // Smooth a little so one jittery frame doesn't trigger "slow down".
        angularSpeed = 0.7 * angularSpeed + 0.3 * rotationAngle(prev.pose, pose) / dt
        linearSpeed = 0.7 * linearSpeed + 0.3 * simd_distance(position(prev.pose), position(pose)) / dt
    }
}

private func position(_ m: simd_float4x4) -> SIMD3<Float> {
    SIMD3(m.columns.3.x, m.columns.3.y, m.columns.3.z)
}

private func rotationAngle(_ a: simd_float4x4, _ b: simd_float4x4) -> Float {
    let qa = simd_quatf(a), qb = simd_quatf(b)
    let angle = (qa.inverse * qb).angle
    return min(angle, 2 * .pi - angle)
}
