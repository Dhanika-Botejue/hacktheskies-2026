import Foundation

/// Detects steps from accelerometer magnitude. Same algorithm as web/js/pdr.js.
final class StepDetector {
    var k = 0.48  // Weinberg stride constant
    var high = 1.2  // m/s² above gravity to arm a step
    var minInterval = 0.28  // s between steps
    private(set) var count = 0

    private var gravity = 9.81
    private var smooth = 0.0
    private var armed = false
    private var peak = 0.0
    private var trough = 0.0
    private var lastStep = 0.0
    private var lastT = 0.0
    private var headingSin = 0.0
    private var headingCos = 0.0
    private var heading = 0.0
    private let onStep: (_ heading: Double, _ length: Double) -> Void

    init(onStep: @escaping (_ heading: Double, _ length: Double) -> Void) {
        self.onStep = onStep
    }

    /// Feed the current heading (degrees) as often as it updates.
    func addHeading(_ h: Double) {
        heading = h
        headingSin += sin(h * .pi / 180)
        headingCos += cos(h * .pi / 180)
    }

    /// Feed total acceleration including gravity, in m/s², with a timestamp in seconds.
    func addMotion(ax: Double, ay: Double, az: Double, t: Double) {
        let dt = lastT > 0 ? min(t - lastT, 0.1) : 0.016
        lastT = t
        let mag = (ax * ax + ay * ay + az * az).squareRoot()
        gravity += (mag - gravity) * min(1, dt / 2.0)  // ~2 s baseline
        smooth += ((mag - gravity) - smooth) * min(1, dt / 0.06)  // ~60 ms low-pass
        let s = smooth

        trough = min(trough, s)
        if !armed && s > high {
            armed = true
            peak = s
        }
        if armed {
            peak = max(peak, s)
            if s < 0 {
                armed = false
                if t - lastStep > minInterval { fire(t) }
            }
        }
    }

    private func fire(_ t: Double) {
        let swing = max(peak - trough, 0.5)
        let length = min(1.8, max(0.3, k * pow(swing, 0.25)))
        let h = (headingSin != 0 || headingCos != 0)
            ? (atan2(headingSin, headingCos) * 180 / .pi + 360).truncatingRemainder(dividingBy: 360)
            : heading
        headingSin = 0
        headingCos = 0
        trough = 0
        lastStep = t
        count += 1
        onStep(h, length)
    }
}
