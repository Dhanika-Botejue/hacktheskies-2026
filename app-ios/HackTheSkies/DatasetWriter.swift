import ARKit
import CoreImage

/// Writes a scan as a nerfstudio dataset that the laptop trains into a Gaussian splat:
///   images/frame_XXXX.jpg   keyframes (sensor's native landscape orientation)
///   transforms.json         per-frame intrinsics + camera-to-world pose (ARKit = OpenGL convention)
///   points.ply              coloured LiDAR points, used as the starting splats
/// Not thread-safe: use from one serial queue.
final class DatasetWriter {
    let dir: URL
    private let imagesDir: URL
    private let ciContext = CIContext()
    private let imageScale: Float = 0.75 // 1920x1440 -> 1440x1080
    private var frames: [[String: Any]] = []
    private var size: (w: Int, h: Int) = (0, 0)
    private var points: [SIMD3<Float>] = []
    private var colors: [SIMD3<UInt8>] = []

    var frameCount: Int { frames.count }
    var pointCount: Int { points.count }

    init() throws {
        dir = FileManager.default.temporaryDirectory.appendingPathComponent("scan-\(Int(Date().timeIntervalSince1970))")
        imagesDir = dir.appendingPathComponent("images")
        try FileManager.default.createDirectory(at: imagesDir, withIntermediateDirectories: true)
    }

    struct Keyframe {
        let image: CVPixelBuffer
        let depth: CVPixelBuffer?
        let confidence: CVPixelBuffer?
        let intrinsics: simd_float3x3
        let pose: simd_float4x4
    }

    func add(_ kf: Keyframe) throws {
        let name = String(format: "images/frame_%04d.jpg", frames.count)
        let s = CGFloat(imageScale)
        let scaled = CIImage(cvPixelBuffer: kf.image)
            .applyingFilter("CILanczosScaleTransform", parameters: [kCIInputScaleKey: s, kCIInputAspectRatioKey: 1.0])
        let w = CVPixelBufferGetWidth(kf.image)
        let h = CVPixelBufferGetHeight(kf.image)
        let outW = Int((Float(w) * imageScale).rounded())
        let outH = Int((Float(h) * imageScale).rounded())
        let cropped = scaled.cropped(to: CGRect(x: 0, y: 0, width: outW, height: outH))
        guard let jpeg = ciContext.jpegRepresentation(
            of: cropped,
            colorSpace: CGColorSpace(name: CGColorSpace.sRGB)!,
            options: [kCGImageDestinationLossyCompressionQuality as CIImageRepresentationOption: 0.85]
        ) else { throw ScanError.encodingFailed }
        try jpeg.write(to: dir.appendingPathComponent(name))
        size = (outW, outH)

        // ARKit intrinsics are column-major: fx = [0][0], fy = [1][1], cx = [2][0], cy = [2][1].
        let k = kf.intrinsics
        frames.append([
            "file_path": name,
            "fl_x": k[0][0] * imageScale,
            "fl_y": k[1][1] * imageScale,
            "cx": k[2][0] * imageScale,
            "cy": k[2][1] * imageScale,
            "w": outW,
            "h": outH,
            "transform_matrix": rows(kf.pose),
        ])

        if let depth = kf.depth, let confidence = kf.confidence {
            addDepthPoints(depth: depth, confidence: confidence, image: kf.image, intrinsics: k, pose: kf.pose)
        }
    }

    /// Unprojects a sparse grid of confident LiDAR depth samples into world space, coloured from the photo.
    private func addDepthPoints(depth: CVPixelBuffer, confidence: CVPixelBuffer, image: CVPixelBuffer,
                                intrinsics k: simd_float3x3, pose: simd_float4x4) {
        CVPixelBufferLockBaseAddress(depth, .readOnly)
        CVPixelBufferLockBaseAddress(confidence, .readOnly)
        CVPixelBufferLockBaseAddress(image, .readOnly)
        defer {
            CVPixelBufferUnlockBaseAddress(depth, .readOnly)
            CVPixelBufferUnlockBaseAddress(confidence, .readOnly)
            CVPixelBufferUnlockBaseAddress(image, .readOnly)
        }
        guard let depthBase = CVPixelBufferGetBaseAddress(depth),
              let confBase = CVPixelBufferGetBaseAddress(confidence),
              let yBase = CVPixelBufferGetBaseAddressOfPlane(image, 0),
              let uvBase = CVPixelBufferGetBaseAddressOfPlane(image, 1)
        else { return }

        let dw = CVPixelBufferGetWidth(depth), dh = CVPixelBufferGetHeight(depth)
        let depthRow = CVPixelBufferGetBytesPerRow(depth)
        let confRow = CVPixelBufferGetBytesPerRow(confidence)
        let iw = CVPixelBufferGetWidth(image), ih = CVPixelBufferGetHeight(image)
        let yRow = CVPixelBufferGetBytesPerRowOfPlane(image, 0)
        let uvRow = CVPixelBufferGetBytesPerRowOfPlane(image, 1)
        let fx = k[0][0], fy = k[1][1], cx = k[2][0], cy = k[2][1]
        let sx = Float(iw) / Float(dw), sy = Float(ih) / Float(dh)

        for v in stride(from: 4, to: dh, by: 8) {
            for u in stride(from: 4, to: dw, by: 8) {
                let conf = confBase.load(fromByteOffset: v * confRow + u, as: UInt8.self)
                let d = depthBase.load(fromByteOffset: v * depthRow + u * 4, as: Float32.self)
                guard conf >= UInt8(ARConfidenceLevel.high.rawValue), d > 0.2, d < 6 else { continue }

                let px = (Float(u) + 0.5) * sx, py = (Float(v) + 0.5) * sy
                // Image-space ray (y down, z forward) -> ARKit camera space (y up, z backward).
                let cam = SIMD4<Float>((px - cx) / fx * d, -(py - cy) / fy * d, -d, 1)
                let world = pose * cam
                points.append(SIMD3(world.x, world.y, world.z))

                let ix = min(iw - 1, Int(px)), iy = min(ih - 1, Int(py))
                let y = Float(yBase.load(fromByteOffset: iy * yRow + ix, as: UInt8.self))
                let uvOffset = (iy / 2) * uvRow + (ix / 2) * 2
                let cb = Float(uvBase.load(fromByteOffset: uvOffset, as: UInt8.self)) - 128
                let cr = Float(uvBase.load(fromByteOffset: uvOffset + 1, as: UInt8.self)) - 128
                func byte(_ x: Float) -> UInt8 { UInt8(max(0, min(255, x))) }
                colors.append(SIMD3(byte(y + 1.402 * cr), byte(y - 0.344136 * cb - 0.714136 * cr), byte(y + 1.772 * cb)))
            }
        }
    }

    /// Writes transforms.json + points.ply. Returns the dataset folder.
    func finish() throws -> URL {
        guard !frames.isEmpty else { throw ScanError.noFrames }
        var transforms: [String: Any] = [
            "camera_model": "PINHOLE",
            "w": size.w,
            "h": size.h,
            "frames": frames,
        ]
        if !points.isEmpty {
            try writePly(to: dir.appendingPathComponent("points.ply"))
            transforms["ply_file_path"] = "points.ply"
        }
        let json = try JSONSerialization.data(withJSONObject: transforms, options: [.prettyPrinted])
        try json.write(to: dir.appendingPathComponent("transforms.json"))
        return dir
    }

    private func writePly(to url: URL) throws {
        var data = Data("""
        ply
        format binary_little_endian 1.0
        element vertex \(points.count)
        property float x
        property float y
        property float z
        property uchar red
        property uchar green
        property uchar blue
        end_header

        """.utf8)
        data.reserveCapacity(data.count + points.count * 15)
        for (p, c) in zip(points, colors) {
            withUnsafeBytes(of: p.x.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: p.y.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            withUnsafeBytes(of: p.z.bitPattern.littleEndian) { data.append(contentsOf: $0) }
            data.append(contentsOf: [c.x, c.y, c.z])
        }
        try data.write(to: url)
    }

    private func rows(_ m: simd_float4x4) -> [[Float]] {
        (0..<4).map { r in [m.columns.0[r], m.columns.1[r], m.columns.2[r], m.columns.3[r]] }
    }
}

enum ScanError: LocalizedError {
    case encodingFailed, noFrames, zipFailed

    var errorDescription: String? {
        switch self {
        case .encodingFailed: "Couldn't encode a camera frame."
        case .noFrames: "No frames were captured. Move around more slowly."
        case .zipFailed: "Couldn't package the scan."
        }
    }
}

/// Zips a folder using the system's built-in "zip for upload" support.
func zipDirectory(_ dir: URL) throws -> URL {
    var result: Result<URL, Error> = .failure(ScanError.zipFailed)
    var coordinationError: NSError?
    NSFileCoordinator().coordinate(readingItemAt: dir, options: [.forUploading], error: &coordinationError) { zipURL in
        let dest = FileManager.default.temporaryDirectory.appendingPathComponent(dir.lastPathComponent + ".zip")
        try? FileManager.default.removeItem(at: dest)
        result = Result { try FileManager.default.copyItem(at: zipURL, to: dest); return dest }
    }
    if let coordinationError { throw coordinationError }
    return try result.get()
}
