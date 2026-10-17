"""Combines a phone's sensor streams into its player's tracker.

Camera tracking (ARCore/ARKit) is the primary source of movement. The step
counter takes over when the camera has lost tracking, or when the two disagree
(e.g. steps say "walking" but the camera reports standing still because someone
is blocking it). When the camera restarts tracking with a new orientation, the
gyro (or else the compass) re-aligns it. The compass also corrects heading drift
while steps are driving movement; with the camera healthy its heading is better
than an indoor compass, so the compass is left out then.

Kept free of networking so tests can drive it with simulated sensor data and time.
"""

import math
from collections import deque

import numpy as np

from .tracker import PlayerTracker

CAMERA_FRESH_S = 0.7  # camera counts as driving movement if it moved us this recently
POSE_FRESH_S = 1.0  # an AR pose this recent means the phone is running camera tracking
FROZEN_STEPS = 3  # this many steps in a row with the camera barely moving = camera is blocked
MATCH_RATIO = 0.3  # camera must move this fraction of a step's length to "agree" with it
DRIFT_DIST = 2.5  # m the camera may move with no steps before it's considered drifting
MAX_SPEED = 8.0  # m/s; faster camera jumps are glitches, not movement
COMPASS_PERIOD_S = 1.0
COMPASS_MIN_MOVE = 0.5  # m moved between compass corrections (standing next to a steel pillar shouldn't drag the heading)
NORTH_SAMPLES = 50  # compass readings averaged after calibration before the compass corrects anything


class PlayerFusion:
    def __init__(self, tracker: PlayerTracker | None = None):
        self.tracker = tracker or PlayerTracker()
        self.gyro: float | None = None  # latest phone heading readings, degrees
        self.compass: float | None = None
        self.last_pose: dict | None = None
        self.camera_used_at = -math.inf
        self.last_step_at = -math.inf
        self.camera_suspect = False
        self.steps = 0
        self.realigns: list[str] = []
        self._recent_steps: deque = deque(maxlen=8)  # dicts: heading, length, src, verdict, applied
        self._ar_at_step: tuple[float, float] | None = None  # camera position at the last step
        self._compass_at = -math.inf
        self._compass_pos: tuple[float, float] | None = None
        self._north_samples: list[float] | None = None

    # ---------- inputs ----------

    def on_heading(self, gyro: float | None, compass: float | None, now: float) -> None:
        if gyro is not None:
            self.gyro = gyro
        self.compass = compass
        if compass is None or not self.tracker.calibrated:
            return
        if self._north_samples is not None:
            self._learn_north(compass, now)
            return
        # The camera's heading beats an indoor compass, so only correct drift while steps drive movement.
        if self.camera_driving(now) or now - self._compass_at < COMPASS_PERIOD_S:
            return
        x, y, _ = self.tracker.estimate()
        if self._compass_pos and math.dist((x, y), self._compass_pos) < COMPASS_MIN_MOVE:
            return
        src, facing = self._facing_source(now)
        if facing is not None:
            self.tracker.compass_update(compass, facing, src)
            self._compass_at, self._compass_pos = now, (x, y)

    def on_pose(self, x: float, z: float, yaw: float, tracking: bool, epoch: int, now: float,
                walls: np.ndarray) -> None:
        prev = self.last_pose
        self.last_pose = {"x": x, "z": z, "yaw": yaw, "tracking": tracking, "epoch": epoch, "t": now}
        if self.tracker.calibrated and not self.tracker.can_use("ar"):
            # Placed before camera tracking started: align the camera from the gyro/compass.
            self.realigns.append(self.tracker.realign_ar(yaw, self.gyro, self.compass))
            return
        if prev is None:
            return
        if prev["epoch"] != epoch:
            # The phone restarted camera tracking; its orientation frame changed.
            self.realigns.append(self.tracker.realign_ar(yaw, self.gyro, self.compass))
            self._ar_at_step = None
            return

        dX, dZ = x - prev["x"], z - prev["z"]
        d = math.hypot(dX, dZ)
        if d > max(1.0, MAX_SPEED * (now - prev["t"])):
            self._ar_at_step = None
            return  # tracking glitch: skip the jump, keep following from the new pose
        if not (tracking and prev["tracking"]):
            self.camera_used_at = -math.inf  # hand over to steps right away
            return

        counter_alive = now - self.last_step_at < 20  # only trust "no steps" if the counter has been working
        if counter_alive and now - self.last_step_at > 1.5 and self._ar_moved_since_step() > DRIFT_DIST:
            self.camera_suspect = True  # moving with no steps: the camera is drifting
        if self.camera_suspect:
            return
        if self.tracker.move_ar(dX, dZ, walls):
            self.camera_used_at = now
        if self.gyro is not None:
            self.tracker.sync_gyro(yaw, self.gyro)

    def on_step(self, heading: float, length: float, src: str, now: float, walls: np.ndarray) -> None:
        self.steps += 1
        self.last_step_at = now
        # Did the camera move along with this step? Unknown if it wasn't tracking.
        if self._ar_at_step is None or not (self.last_pose and self.last_pose["tracking"]):
            verdict = "unknown"
        else:
            verdict = "agree" if self._ar_moved_since_step() > MATCH_RATIO * length else "disagree"
        pose = self.last_pose
        self._ar_at_step = (pose["x"], pose["z"]) if pose and pose["tracking"] else None
        step = {"heading": heading, "length": length, "src": src, "verdict": verdict, "applied": False}
        self._recent_steps.append(step)
        recent = list(self._recent_steps)

        if self.camera_suspect:
            if len(recent) >= 2 and all(r["verdict"] == "agree" for r in recent[-2:]):
                self.camera_suspect = False  # camera agrees with the steps again
        elif self.camera_driving(now) and len(recent) >= FROZEN_STEPS \
                and all(r["verdict"] == "disagree" for r in recent[-FROZEN_STEPS:]):
            # Walking, but the camera barely moved: it's blocked or frozen. Overrule it and
            # replay the steps it missed (this one is applied below).
            self.camera_suspect = True
            self.camera_used_at = -math.inf
            missed = []
            for r in reversed(recent[:-1]):
                if r["verdict"] != "disagree" or r["applied"]:
                    break
                missed.insert(0, r)
            for r in missed:
                self._apply_step(r, walls)

        if not self.camera_driving(now):
            self._apply_step(step, walls)

    def _ar_moved_since_step(self) -> float:
        """Net camera displacement since the last step (jitter doesn't add up like path length)."""
        pose = self.last_pose
        if self._ar_at_step is None or not pose or not pose["tracking"]:
            return 0.0
        return math.dist(self._ar_at_step, (pose["x"], pose["z"]))

    def _apply_step(self, step: dict, walls: np.ndarray) -> None:
        step["applied"] = True
        # Heading from the camera's orientation if it's aligned, else the gyro.
        heading, src = step["heading"], step["src"]
        if src == "ar" and not self.tracker.can_use("ar"):
            src, heading = "gyro", self.gyro
        if heading is not None:
            self.tracker.step(heading, step["length"], walls, src)

    def calibrate(self, x: float, y: float, map_heading: float, now: float) -> None:
        pose = self.last_pose if self.last_pose and now - self.last_pose["t"] < POSE_FRESH_S else None
        self.tracker.calibrate(x, y, map_heading,
                               {"ar": pose and pose["yaw"], "gyro": self.gyro}, self.compass)
        self._compass_pos = None
        self._north_samples = []

    def _learn_north(self, compass: float, now: float) -> None:
        """A single compass reading is often 10°+ off indoors, so average where north is
        over the first readings after calibration, then let the compass correct drift."""
        src, h = self._facing_source(now)
        facing = None if h is None else self.tracker.map_heading(h, src)
        if facing is None:
            return
        self._north_samples.append(math.radians(facing - compass))
        if len(self._north_samples) >= NORTH_SAMPLES:
            a = np.array(self._north_samples)
            self.tracker.north_offset = float(np.arctan2(np.sin(a).mean(), np.cos(a).mean()))
            self._north_samples = None

    # ---------- state ----------

    def camera_driving(self, now: float) -> bool:
        return now - self.camera_used_at < CAMERA_FRESH_S

    def source(self, now: float) -> str:
        if self.camera_driving(now):
            return "camera"
        has_pose = self.last_pose is not None and now - self.last_pose["t"] < POSE_FRESH_S
        if has_pose and self.camera_suspect:
            return "steps (camera disagrees)"
        if has_pose:
            return "steps (camera lost)" if now - self.last_step_at < 3 else "camera lost"
        return "steps" if now - self.last_step_at < 3 else "idle"

    def facing(self, now: float) -> float | None:
        src, h = self._facing_source(now)
        return None if h is None else self.tracker.map_heading(h, src)

    def _facing_source(self, now: float) -> tuple[str, float | None]:
        pose = self.last_pose
        if pose and now - pose["t"] < POSE_FRESH_S and self.tracker.can_use("ar"):
            return "ar", pose["yaw"]
        return "gyro", self.gyro
