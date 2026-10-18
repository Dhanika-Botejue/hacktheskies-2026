"""Particle filter for a player's position on the map, constrained by walls.

Motion comes from two sources:
  * camera (ARCore/ARKit) displacements, the primary source: move_ar()
  * detected steps with a heading, the fallback: step()
and the compass nudges the heading estimate: compass_update().

Each heading source ("ar", "gyro") has an arbitrary zero on the phone. A
calibration ("I'm here, facing there") gives each source's offset to map
heading, and the compass's offset to map heading (north_offset).

Each particle carries its own heading bias and step-length scale, so the filter
learns slow heading drift and stride error. Particles whose move crosses a wall
are killed, so walls prune wrong hypotheses.

Map heading convention: degrees clockwise from map "up" (-y), so moving L at
heading h is (sin h * L, -cos h * L).
"""

from dataclasses import dataclass

import numpy as np

from .geometry import segments_hit_walls


def wrap(a):
    return (a + np.pi) % (2 * np.pi) - np.pi


@dataclass
class TrackerConfig:
    n_particles: int = 600
    init_pos_std: float = 0.4  # m
    init_bias_std: float = np.radians(6)
    init_scale_std: float = 0.12
    # Steps
    step_heading_std: float = np.radians(5)
    step_len_std: float = 0.06  # m
    bias_walk_std: float = np.radians(0.6)  # per step
    scale_walk_std: float = 0.004
    scale_min: float = 0.6
    scale_max: float = 1.6
    # Camera
    ar_pos_std: float = 0.003  # m per update (phones send 30 a second)
    ar_rel_std: float = 0.03  # fraction of distance moved
    ar_bias_walk_std: float = np.radians(0.07)  # per update
    # Compass
    compass_std: float = np.radians(25)
    compass_floor: float = 0.5  # keeps a wild reading from wiping out particles
    # Re-aligning the camera's heading after its tracking restarts
    realign_gyro_std: float = np.radians(2)
    realign_compass_std: float = np.radians(15)
    realign_blind_std: float = np.radians(45)
    # When every particle hits a wall, retry with this extra noise before giving up
    # and ignoring walls for that move.
    recovery_heading_stds: tuple = (np.radians(15), np.radians(35))
    recovery_pos_stds: tuple = (0.15, 0.4)


class PlayerTracker:
    def __init__(self, config: TrackerConfig | None = None, rng: np.random.Generator | None = None):
        self.cfg = config or TrackerConfig()
        self.rng = rng or np.random.default_rng()
        self.calibrated = False
        self.offsets: dict[str, float] = {}  # rad, map_heading = source_heading + offset (+ bias)
        self.north_offset: float | None = None  # rad, map_heading = compass + north_offset
        self.lost_steps = 0  # moves where walls had to be ignored
        n = self.cfg.n_particles
        self.x = np.zeros(n)
        self.y = np.zeros(n)
        self.bias = np.zeros(n)
        self.scale = np.ones(n)
        self.w = np.full(n, 1.0 / n)

    def calibrate(self, x: float, y: float, map_heading_deg: float,
                  headings: dict[str, float | None], compass_deg: float | None = None) -> None:
        """Place the player at (x, y) facing map_heading. `headings` holds what each
        heading source reads right now, e.g. {"ar": 12.0, "gyro": 230.5}."""
        c, n, r = self.cfg, self.cfg.n_particles, self.rng
        mh = np.radians(map_heading_deg)
        self.offsets = {src: mh - np.radians(h) for src, h in headings.items() if h is not None}
        self.north_offset = None if compass_deg is None else mh - np.radians(compass_deg)
        self.x = x + r.normal(0, c.init_pos_std, n)
        self.y = y + r.normal(0, c.init_pos_std, n)
        self.bias = r.normal(0, c.init_bias_std, n)
        self.scale = np.clip(r.normal(1.0, c.init_scale_std, n), c.scale_min, c.scale_max)
        self.w = np.full(n, 1.0 / n)
        self.calibrated = True
        self.lost_steps = 0

    def can_use(self, src: str) -> bool:
        return self.calibrated and src in self.offsets

    # ---------- motion ----------

    def step(self, heading_deg: float, length: float, walls: np.ndarray, src: str = "gyro") -> bool:
        if not self.can_use(src):
            return False
        c, n, r = self.cfg, self.cfg.n_particles, self.rng
        base = np.radians(heading_deg) + self.offsets[src]

        def propose(extra):
            h = base + self.bias + r.normal(0, np.hypot(c.step_heading_std, extra), n)
            L = np.maximum(length * self.scale + r.normal(0, c.step_len_std, n), 0)
            return self.x + np.sin(h) * L, self.y - np.cos(h) * L

        self._move(propose, (0.0, *c.recovery_heading_stds), walls)
        self.bias = self.bias + r.normal(0, c.bias_walk_std, n)
        self.scale = np.clip(self.scale + r.normal(0, c.scale_walk_std, n), c.scale_min, c.scale_max)
        return True

    def move_ar(self, dX: float, dZ: float, walls: np.ndarray) -> bool:
        """Apply a camera displacement measured in AR space (x right, z toward the
        starting viewer, so -z is the AR "forward")."""
        if not self.can_use("ar"):
            return False
        c, n, r = self.cfg, self.cfg.n_particles, self.rng
        d = np.hypot(dX, dZ)

        def propose(extra):
            rot = self.offsets["ar"] + self.bias
            cos, sin = np.cos(rot), np.sin(rot)
            std = np.hypot(c.ar_pos_std + c.ar_rel_std * d, extra)
            nx = self.x + dX * cos - dZ * sin + r.normal(0, std, n)
            ny = self.y + dX * sin + dZ * cos + r.normal(0, std, n)
            return nx, ny

        self._move(propose, (0.0, *c.recovery_pos_stds), walls)
        self.bias = self.bias + r.normal(0, c.ar_bias_walk_std, n)
        return True

    def _move(self, propose, extras, walls: np.ndarray) -> None:
        start = np.column_stack([self.x, self.y])
        for extra in extras:
            nx, ny = propose(extra)
            alive = ~segments_hit_walls(start, np.column_stack([nx, ny]), walls)
            if alive.any():
                break
        if alive.any():
            w = self.w * alive
        else:
            # Every hypothesis walked into a wall: the map or the alignment is off.
            # Keep moving rather than freezing the player in place.
            self.lost_steps += 1
            w = self.w.copy()
        self.x, self.y = nx, ny
        self._set_weights(w)

    # ---------- heading corrections ----------

    def compass_update(self, compass_deg: float, facing_deg: float, src: str) -> None:
        """Reweight particles by how well their heading bias agrees with the compass.
        facing_deg is what heading source `src` reads at the same moment."""
        if self.north_offset is None or not self.can_use(src):
            return
        c = self.cfg
        expected = np.radians(facing_deg) + self.offsets[src] + self.bias
        measured = np.radians(compass_deg) + self.north_offset
        d = wrap(expected - measured)
        self._set_weights(self.w * (c.compass_floor + np.exp(-0.5 * (d / c.compass_std) ** 2)))

    def sync_gyro(self, ar_yaw_deg: float, gyro_deg: float) -> None:
        """While the camera is healthy, learn how the gyro heading maps to the camera's,
        so the gyro can carry the heading across a camera tracking restart."""
        if not self.can_use("ar"):
            return
        target = np.radians(ar_yaw_deg) + self.offsets["ar"] - np.radians(gyro_deg)
        if "gyro" not in self.offsets:
            self.offsets["gyro"] = target
        else:
            self.offsets["gyro"] += 0.05 * wrap(target - self.offsets["gyro"])

    def realign_ar(self, ar_yaw_deg: float, gyro_deg: float | None, compass_deg: float | None) -> str:
        """The camera restarted tracking with a new, unknown orientation. Re-derive its
        heading offset from the gyro if possible, else the compass, else guess widely."""
        if not self.calibrated:
            return "none"
        c, n, r = self.cfg, self.cfg.n_particles, self.rng
        if gyro_deg is not None and "gyro" in self.offsets:
            self.offsets["ar"] = np.radians(gyro_deg) + self.offsets["gyro"] - np.radians(ar_yaw_deg)
            self.bias = self.bias + r.normal(0, c.realign_gyro_std, n)
            return "gyro"
        if compass_deg is not None and self.north_offset is not None:
            self.offsets["ar"] = np.radians(compass_deg) + self.north_offset - np.radians(ar_yaw_deg)
            self.bias = r.normal(0, c.realign_compass_std, n)
            return "compass"
        if "ar" in self.offsets:
            self.bias = r.normal(0, c.realign_blind_std, n)
            return "blind"
        return "none"

    # ---------- bookkeeping ----------

    def _set_weights(self, w: np.ndarray) -> None:
        total = w.sum()
        self.w = w / total if total > 0 else np.full(self.cfg.n_particles, 1.0 / self.cfg.n_particles)
        if 1.0 / np.sum(self.w**2) < self.cfg.n_particles / 2:
            self._resample()

    def _resample(self) -> None:
        n = self.cfg.n_particles
        positions = (self.rng.random() + np.arange(n)) / n
        idx = np.minimum(np.searchsorted(np.cumsum(self.w), positions), n - 1)
        self.x, self.y = self.x[idx], self.y[idx]
        self.bias, self.scale = self.bias[idx], self.scale[idx]
        self.w = np.full(n, 1.0 / n)

    def estimate(self) -> tuple[float, float, float]:
        """Weighted mean position and its spread (std, m)."""
        mx = float(np.sum(self.w * self.x))
        my = float(np.sum(self.w * self.y))
        var = np.sum(self.w * ((self.x - mx) ** 2 + (self.y - my) ** 2))
        return mx, my, float(np.sqrt(var))

    def mean_bias(self) -> float:
        return float(np.arctan2(np.sum(self.w * np.sin(self.bias)), np.sum(self.w * np.cos(self.bias))))

    def map_heading(self, heading_deg: float, src: str) -> float | None:
        """Best estimate of the direction the player faces, in map degrees."""
        if not self.can_use(src):
            return None
        return float(np.degrees(np.radians(heading_deg) + self.offsets[src] + self.mean_bias()) % 360)

    def sample_particles(self, k: int) -> list[list[float]]:
        idx = self.rng.choice(self.cfg.n_particles, size=min(k, self.cfg.n_particles), replace=False)
        return [[round(float(self.x[i]), 2), round(float(self.y[i]), 2)] for i in idx]
