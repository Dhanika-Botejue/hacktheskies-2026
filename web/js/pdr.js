// Pedestrian dead reckoning on the phone: heading from the orientation sensor
// and step detection from the accelerometer. The server turns steps into
// positions with a wall-aware particle filter.

const DEG = Math.PI / 180;

// Direction the phone's rear camera points, in degrees clockwise from the
// orientation frame's "north" (which may be arbitrary; calibration fixes it).
// With the phone strapped to the chest, that is the direction the player faces.
// When the phone lies flat the camera points at the floor, so fall back to
// where the top edge points.
export function headingFromOrientation(alpha, beta, gamma) {
  const ca = Math.cos(alpha * DEG), sa = Math.sin(alpha * DEG);
  const cb = Math.cos(beta * DEG), sb = Math.sin(beta * DEG);
  const cg = Math.cos(gamma * DEG), sg = Math.sin(gamma * DEG);

  // Device -z axis (out of the back) in the earth frame (x east, y north), from R = Rz(a)·Rx(b)·Ry(g).
  const camE = -(ca * sg + sa * sb * cg);
  const camN = ca * sb * cg - sa * sg;
  // Device +y axis (top edge) in the earth frame.
  const topE = -sa * cb;
  const topN = ca * cb;

  const camH = Math.hypot(camE, camN);
  const [e, n] = camH > 0.5 ? [camE, camN] : [topE, topN];
  return ((Math.atan2(e, n) / DEG) + 360) % 360;
}

export class StepDetector {
  // onStep({ heading, len }) fires once per detected step.
  constructor(onStep) {
    this.onStep = onStep;
    this.k = 0.48; // Weinberg stride constant; tuned per player via the UI
    this.high = 1.2; // m/s^2 above gravity to arm a step
    this.minInterval = 280; // ms between steps
    this.gravity = 9.81;
    this.smooth = 0;
    this.armed = false;
    this.peak = 0;
    this.trough = 0;
    this.lastStep = 0;
    this.lastT = 0;
    this.headingSin = 0;
    this.headingCos = 0;
    this.heading = 0;
    this.count = 0;
  }

  // Feed the current heading as often as orientation events arrive.
  addHeading(h) {
    this.heading = h;
    this.headingSin += Math.sin(h * DEG);
    this.headingCos += Math.cos(h * DEG);
  }

  addMotion(ax, ay, az, t) {
    const dt = this.lastT ? Math.min((t - this.lastT) / 1000, 0.1) : 0.016;
    this.lastT = t;
    const mag = Math.hypot(ax, ay, az);
    this.gravity += (mag - this.gravity) * Math.min(1, dt / 2.0); // ~2 s baseline
    const raw = mag - this.gravity;
    this.smooth += (raw - this.smooth) * Math.min(1, dt / 0.06); // ~60 ms low-pass
    const s = this.smooth;

    this.trough = Math.min(this.trough, s);
    if (!this.armed && s > this.high) { this.armed = true; this.peak = s; }
    if (this.armed) {
      this.peak = Math.max(this.peak, s);
      if (s < 0) {
        this.armed = false;
        if (t - this.lastStep > this.minInterval) this.fire(t);
      }
    }
  }

  fire(t) {
    const swing = Math.max(this.peak - this.trough, 0.5);
    const len = Math.min(1.8, Math.max(0.3, this.k * Math.pow(swing, 0.25)));
    // The average since the last step; after standing still that's mostly where the player faced
    // before they turned to walk off, so the first step takes the current heading.
    const heading = (this.headingSin || this.headingCos) && t - this.lastStep < 2000
      ? ((Math.atan2(this.headingSin, this.headingCos) / DEG) + 360) % 360
      : this.heading;
    this.headingSin = this.headingCos = 0;
    this.trough = 0;
    this.lastStep = t;
    this.count++;
    this.onStep({ heading, len });
  }
}

// iOS needs an explicit permission prompt from a user gesture.
export async function requestSensorPermission() {
  for (const E of [window.DeviceMotionEvent, window.DeviceOrientationEvent]) {
    if (E && typeof E.requestPermission === 'function') {
      const r = await E.requestPermission();
      if (r !== 'granted') throw new Error('Motion sensor permission was denied.');
    }
  }
}

// Which way the back camera faces in AR space, in degrees clockwise seen from
// above (AR space: x right, y up, -z forward at start). q is the viewer pose's
// orientation quaternion. If the phone is near flat, use where its top edge points.
export function yawFromQuaternion(q) {
  const { x, y, z, w } = q;
  let fx = -2 * (x * z + w * y), fz = -(1 - 2 * (x * x + y * y));
  if (Math.hypot(fx, fz) < 0.3) { fx = 2 * (x * y - w * z); fz = 2 * (y * z + w * x); }
  return (Math.atan2(fx, -fz) / DEG + 360) % 360;
}
