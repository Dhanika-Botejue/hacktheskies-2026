"""A demo floor and a simulated walk with realistic sensor errors.

Used by the tests and by simulate.py to exercise the tracker without walking around.
"""

import math

import numpy as np

# 30 x 20 m floor: three rooms on top, a corridor through the middle, two rooms below.
DEMO_MAP = {
    "width": 30.0,
    "height": 20.0,
    "image": None,
    "walls": [
        [0, 0, 30, 0], [30, 0, 30, 20], [30, 20, 0, 20], [0, 20, 0, 0],
        [0, 8, 4, 8], [5.2, 8, 14, 8], [15.2, 8, 24, 8], [25.2, 8, 30, 8],
        [0, 12, 6, 12], [7.2, 12, 22, 12], [23.2, 12, 30, 12],
        [10, 0, 10, 8], [20, 0, 20, 8], [15, 12, 15, 20],
    ],
}

# From the top-left room, out its door, along the corridor, into the bottom-right room.
DEMO_PATH = [(3, 4), (4.6, 6.5), (4.6, 10), (22.6, 10), (22.6, 14), (26, 17)]


def heading_between(a, b) -> float:
    """Map heading in degrees (clockwise from up) from a to b."""
    return math.degrees(math.atan2(b[0] - a[0], -(b[1] - a[1]))) % 360


def simulate_walk(path=DEMO_PATH, seed=0, true_step=0.7, len_error=0.88,
                  heading_error_deg=8.0, drift_deg_per_step=0.15, heading_noise_deg=4.0,
                  phone_zero_deg=137.0):
    """Yield (truth_x, truth_y, phone_heading_deg, reported_len) per step.

    The phone's heading has an arbitrary zero (phone_zero_deg), a calibration
    error, slow gyro drift and per-step noise. Reported step length is off by
    len_error (a stride the step model gets wrong).
    """
    rng = np.random.default_rng(seed)
    drift = 0.0
    for a, b in zip(path, path[1:]):
        seg = math.dist(a, b)
        n = max(1, round(seg / true_step))
        h = heading_between(a, b)
        for i in range(1, n + 1):
            drift += drift_deg_per_step
            x = a[0] + (b[0] - a[0]) * i / n
            y = a[1] + (b[1] - a[1]) * i / n
            phone_h = (h - phone_zero_deg + heading_error_deg + drift + rng.normal(0, heading_noise_deg)) % 360
            reported = seg / n * len_error + rng.normal(0, 0.04)
            yield x, y, phone_h, reported


def start_pose(path=DEMO_PATH, phone_zero_deg=137.0):
    """(x, y, map_heading, phone_heading) for calibrating at the path start."""
    h = heading_between(path[0], path[1])
    return path[0][0], path[0][1], h, (h - phone_zero_deg) % 360


def _rotate_cw(x, y, a):
    """Rotate (x, y) clockwise by a radians in a y-down frame (adds a to headings)."""
    return x * math.cos(a) - y * math.sin(a), x * math.sin(a) + y * math.cos(a)


def simulate_session(path=DEMO_PATH, seed=0, speed=1.27, step_period=0.55, pose_hz=15,
                     calib_error_deg=4.0, ar_scale=1.02, ar_drift_deg_per_min=1.0,
                     loss=None, blocked=None, reset_at=None, compass=True, compass_bias_deg=8.0,
                     gyro_drift_deg_per_min=3.0, gyro=True):
    """Simulate a phone running camera tracking along `path`.

    Yields (t, kind, data) in time order, kind in {"heading", "pose", "step",
    "calibrate", "truth"}, matching what a phone sends to the server.

    Errors modelled: the player's calibration tap is off by calib_error_deg, the
    camera's distance scale and yaw drift slowly, camera tracking is lost during
    `loss` = (start_s, end_s) (position frozen), the camera's position freezes
    while it still claims to be tracking during `blocked` (someone standing in
    front of it), the camera restarts tracking
    with a new frame at `reset_at` seconds, the compass has a fixed bias plus
    wandering disturbance, and the gyro drifts.
    """
    rng = np.random.default_rng(seed)
    gyro_ok = gyro
    seg_lens = [math.dist(a, b) for a, b in zip(path, path[1:])]
    total_t = sum(seg_lens) / speed

    def truth_at(t):
        d = min(t * speed, sum(seg_lens))
        for (a, b), L in zip(zip(path, path[1:]), seg_lens):
            if d <= L:
                f = d / L if L else 0
                return a[0] + (b[0] - a[0]) * f, a[1] + (b[1] - a[1]) * f, heading_between(a, b)
            d -= L
        return path[-1][0], path[-1][1], heading_between(path[-2], path[-1])

    ar_rot = math.radians(rng.uniform(0, 360))  # map = rotate_cw(ar, ar_rot) at the start
    ar_origin = (rng.uniform(-5, 5), rng.uniform(-5, 5))
    gyro_zero = rng.uniform(0, 360)
    north = rng.uniform(0, 360)  # map heading of magnetic north
    compass_wander = 0.0
    epoch = 0
    frozen = None

    def ar_pose(t):
        nonlocal frozen
        x, y, h = truth_at(t)
        rot = ar_rot + math.radians(ar_drift_deg_per_min * t / 60)
        ax, az = _rotate_cw(x, y, -rot)
        ax, az = ax * ar_scale + ar_origin[0], az * ar_scale + ar_origin[1]
        tracking = not (loss and loss[0] <= t < loss[1])
        if not tracking or (blocked and blocked[0] <= t < blocked[1]):
            if frozen is None:
                frozen = (ax, az)
            ax, az = frozen
        else:
            frozen = None
        yaw = (h - math.degrees(rot) + rng.normal(0, 1.0)) % 360
        return ax + rng.normal(0, 0.01), az + rng.normal(0, 0.01), yaw, tracking

    def readings(t):
        nonlocal compass_wander
        _, _, h = truth_at(t)
        gyro = (h - gyro_zero + gyro_drift_deg_per_min * t / 60 + rng.normal(0, 1.5)) % 360
        compass_wander = 0.95 * compass_wander + rng.normal(0, 3)
        c = (h - north + compass_bias_deg + compass_wander + rng.normal(0, 5)) % 360 if compass else None
        return {"gyro": gyro if gyro_ok else None, "compass": c}

    # Settle, then the player taps where they are and the direction they face.
    x0, y0, h0 = truth_at(0)
    yield 0.0, "heading", readings(0)
    ax, az, yaw, tr = ar_pose(0)
    yield 0.0, "pose", {"x": ax, "z": az, "yaw": yaw, "tracking": tr, "epoch": epoch}
    yield 0.0, "calibrate", {"x": x0, "y": y0, "mapHeading": (h0 + calib_error_deg) % 360}

    dt = 1 / pose_hz
    next_step = step_period
    next_heading = 0.2
    t = 0.0
    while t < total_t:
        t += dt
        if reset_at is not None and t >= reset_at:
            reset_at = None
            epoch += 1
            ar_rot = math.radians(rng.uniform(0, 360))
            ar_origin = (rng.uniform(-5, 5), rng.uniform(-5, 5))
        ax, az, yaw, tr = ar_pose(t)
        yield t, "pose", {"x": ax, "z": az, "yaw": yaw, "tracking": tr, "epoch": epoch}
        if t >= next_heading:
            next_heading += 0.2
            yield t, "heading", readings(t)
        if t >= next_step:
            next_step += step_period
            reported = speed * step_period * 0.9 + rng.normal(0, 0.04)
            yield t, "step", {"heading": yaw, "len": reported, "src": "ar"}
        x, y, _ = truth_at(t)
        yield t, "truth", {"x": x, "y": y}
