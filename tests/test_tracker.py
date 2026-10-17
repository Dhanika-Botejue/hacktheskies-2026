import math

import numpy as np
import pytest

from server.fusion import PlayerFusion
from server.geometry import segments_hit_walls
from server.tracker import PlayerTracker
from sim.scenario import DEMO_MAP, DEMO_PATH, simulate_session, simulate_walk, start_pose

WALLS = np.array(DEMO_MAP["walls"], dtype=float)
NO_WALLS = np.zeros((0, 4))
LONG_PATH = DEMO_PATH + DEMO_PATH[::-1][1:]


def test_segment_crossing():
    walls = np.array([[0, 0, 0, 10]], dtype=float)
    p = np.array([[-1, 5], [-1, 5], [1, 5], [-1, 11]], dtype=float)
    q = np.array([[1, 5], [-0.5, 5], [2, 5], [1, 11]], dtype=float)
    assert segments_hit_walls(p, q, walls).tolist() == [True, False, False, False]


def test_no_walls_never_hits():
    p = np.zeros((3, 2))
    assert not segments_hit_walls(p, p + 1, NO_WALLS).any()


def test_calibration_aligns_phone_heading_to_map():
    t = PlayerTracker(rng=np.random.default_rng(0))
    t.calibrate(5, 5, map_heading_deg=90, headings={"gyro": 30})
    # Phone at 30° means facing map 90° (east): a step should move +x.
    for _ in range(10):
        t.step(30, 1.0, NO_WALLS, "gyro")
    x, y, _ = t.estimate()
    assert x == pytest.approx(15, abs=1.0)
    assert y == pytest.approx(5, abs=1.0)


def test_camera_displacement_is_rotated_into_map():
    t = PlayerTracker(rng=np.random.default_rng(0))
    # Camera yaw 0 (AR forward = -z) is map heading 90 (east).
    t.calibrate(5, 5, map_heading_deg=90, headings={"ar": 0})
    for _ in range(10):
        t.move_ar(0, -0.5, NO_WALLS)  # walk forward in AR space
    x, y, _ = t.estimate()
    assert x == pytest.approx(10, abs=0.6)
    assert y == pytest.approx(5, abs=0.6)


# ---------- step counting alone ----------

def run_steps(walls, seed):
    t = PlayerTracker(rng=np.random.default_rng(seed))
    x, y, mh, ph = start_pose()
    t.calibrate(x, y, mh, {"gyro": ph})
    errors = []
    for tx, ty, h, length in simulate_walk(seed=seed):
        t.step(h, length, walls, "gyro")
        x, y, _ = t.estimate()
        errors.append(math.hypot(x - tx, y - ty))
    return errors[-1], t


@pytest.mark.parametrize("seed", range(5))
def test_walls_reduce_step_drift(seed):
    free_final, _ = run_steps(NO_WALLS, seed)
    wall_final, tracker = run_steps(WALLS, seed)
    assert wall_final < free_final
    assert wall_final < 1.5
    assert tracker.lost_steps == 0


# ---------- camera-primary fusion ----------

def run_session(walls=WALLS, seed=0, **kw):
    f = PlayerFusion(PlayerTracker(rng=np.random.default_rng(seed)))
    errors, sources = [], set()
    for t, kind, d in simulate_session(seed=seed, **kw):
        if kind == "heading":
            f.on_heading(d["gyro"], d["compass"], t)
        elif kind == "pose":
            f.on_pose(d["x"], d["z"], d["yaw"], d["tracking"], d["epoch"], t, walls)
        elif kind == "step":
            f.on_step(d["heading"], d["len"], d["src"], t, walls)
        elif kind == "calibrate":
            f.calibrate(d["x"], d["y"], d["mapHeading"], t)
        elif kind == "truth":
            x, y, _ = f.tracker.estimate()
            errors.append(math.hypot(x - d["x"], y - d["y"]))
            sources.add(f.source(t))
    return errors, sources, f


@pytest.mark.parametrize("seed", range(4))
def test_camera_tracking(seed):
    errors, sources, _ = run_session(seed=seed, path=LONG_PATH)
    assert np.mean(errors) < 0.8
    assert errors[-1] < 1.5
    assert sources == {"camera"}


@pytest.mark.parametrize("seed", range(4))
def test_steps_bridge_camera_tracking_loss(seed):
    errors, sources, _ = run_session(seed=seed, loss=(8, 13))
    assert "steps (camera lost)" in sources
    assert max(errors) < 2.5
    assert errors[-1] < 1.5


@pytest.mark.parametrize("seed", range(4))
def test_blocked_camera_is_overruled_by_steps(seed):
    errors, sources, _ = run_session(seed=seed, blocked=(8, 13))
    assert "steps (camera disagrees)" in sources
    assert max(errors) < 3.0
    assert errors[-1] < 1.5


@pytest.mark.parametrize("seed", range(4))
@pytest.mark.parametrize("gyro, expected", [(True, "gyro"), (False, "compass")])
def test_camera_restart_is_realigned(seed, gyro, expected):
    errors, _, f = run_session(seed=seed, reset_at=12, gyro=gyro)
    assert f.realigns == [expected]
    assert errors[-1] < 1.5


def test_camera_started_after_placement_is_aligned_from_gyro():
    f = PlayerFusion(PlayerTracker(rng=np.random.default_rng(0)))
    f.on_heading(gyro=200, compass=None, now=0)
    f.calibrate(5, 5, map_heading=90, now=0)  # placed before any camera pose arrived
    assert not f.tracker.can_use("ar")
    f.on_pose(0, 0, yaw=40, tracking=True, epoch=0, now=1, walls=NO_WALLS)
    assert f.realigns == ["gyro"]
    ahead = (math.sin(math.radians(40)), -math.cos(math.radians(40)))  # camera yaw 40° in AR space
    for i in range(1, 11):  # walk 5 m straight ahead
        f.on_pose(0.5 * i * ahead[0], 0.5 * i * ahead[1], yaw=40, tracking=True, epoch=0,
                  now=1 + i * 0.1, walls=NO_WALLS)
    x, y, _ = f.tracker.estimate()
    assert x == pytest.approx(10, abs=0.7)  # straight ahead is still map east
    assert y == pytest.approx(5, abs=0.7)
