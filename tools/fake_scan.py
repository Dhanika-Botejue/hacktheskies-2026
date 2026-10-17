"""Generate a synthetic "phone scan" dataset so the pipeline can be tested without an iPhone.

Ray-casts the inside of a textured box room and writes exactly what the iOS app uploads:
  images/*.jpg, transforms.json (nerfstudio, OpenGL camera-to-world), points.ply (x,y,z,rgb)

Usage: python3 tools/fake_scan.py OUT_DIR [--zip]
"""

import json
import math
import shutil
import struct
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOM_MIN = np.array([-3.0, 0.0, -2.0])  # floor at y=0, metres
ROOM_MAX = np.array([3.0, 2.8, 2.0])
W, H = 480, 360
FX = FY = 380.0
N_FRAMES = 48


def texture(p: np.ndarray, face: np.ndarray) -> np.ndarray:
    """Colour for world points p (N,3) lying on box faces (N,) in 0..5."""
    base = np.array(
        [[0.8, 0.3, 0.3], [0.3, 0.8, 0.3], [0.55, 0.45, 0.35], [0.9, 0.9, 0.85], [0.3, 0.3, 0.8], [0.8, 0.8, 0.3]]
    )[face]
    checker = ((np.floor(p[:, 0] * 2) + np.floor(p[:, 1] * 2) + np.floor(p[:, 2] * 2)) % 2)[:, None]
    detail = 0.5 + 0.5 * np.sin(p[:, 0] * 7.3 + p[:, 1] * 5.1) * np.cos(p[:, 2] * 6.7 - p[:, 1] * 3.3)
    return np.clip(base * (0.6 + 0.3 * checker) + 0.15 * detail[:, None], 0, 1)


def cast(origin: np.ndarray, dirs: np.ndarray):
    """Intersect rays from inside the box with its 6 faces. Returns hit points and face ids."""
    t_best = np.full(len(dirs), np.inf)
    face = np.zeros(len(dirs), dtype=int)
    for axis in range(3):
        for side, bound in enumerate((ROOM_MIN[axis], ROOM_MAX[axis])):
            with np.errstate(divide="ignore", invalid="ignore"):
                t = (bound - origin[axis]) / dirs[:, axis]
            better = (t > 1e-6) & (t < t_best)
            t_best[better] = t[better]
            face[better] = axis * 2 + side
    return origin + dirs * t_best[:, None], face


def look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    """OpenGL camera-to-world: camera looks down -Z, Y up (same convention as ARKit)."""
    fwd = target - eye
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, [0, 1, 0])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    m = np.eye(4)
    m[:3, 0], m[:3, 1], m[:3, 2], m[:3, 3] = right, up, -fwd, eye
    return m


def main():
    out = Path(sys.argv[1])
    (out / "images").mkdir(parents=True, exist_ok=True)

    px, py = np.meshgrid(np.arange(W) + 0.5, np.arange(H) + 0.5)
    cam_dirs = np.stack([(px - W / 2) / FX, -(py - H / 2) / FY, -np.ones_like(px)], -1).reshape(-1, 3)

    frames = []
    for i in range(N_FRAMES):
        a = 2 * math.pi * i / N_FRAMES
        eye = np.array([1.6 * math.cos(a), 1.4 + 0.2 * math.sin(3 * a), 1.0 * math.sin(a)])
        target = np.array([3.0 * math.cos(a + 1.2), 1.2 + 0.5 * math.sin(2 * a), 2.0 * math.sin(a + 1.2)])
        c2w = look_at(eye, target)
        dirs = cam_dirs @ c2w[:3, :3].T
        pts, face = cast(eye, dirs)
        img = (texture(pts, face).reshape(H, W, 3) * 255).astype(np.uint8)
        name = f"images/frame_{i:04d}.jpg"
        Image.fromarray(img).save(out / name, quality=92)
        frames.append({"file_path": name, "transform_matrix": c2w.tolist()})

    # Initial point cloud sampled on the walls, like the LiDAR points from the phone.
    rng = np.random.default_rng(0)
    origin = (ROOM_MIN + ROOM_MAX) / 2
    dirs = rng.normal(size=(20000, 3))
    pts, face = cast(origin, dirs / np.linalg.norm(dirs, axis=1, keepdims=True))
    cols = (texture(pts, face) * 255).astype(np.uint8)
    write_points_ply(out / "points.ply", pts, cols)

    transforms = {
        "camera_model": "PINHOLE",
        "fl_x": FX, "fl_y": FY, "cx": W / 2, "cy": H / 2, "w": W, "h": H,
        "ply_file_path": "points.ply",
        "frames": frames,
    }
    (out / "transforms.json").write_text(json.dumps(transforms, indent=1))

    if "--zip" in sys.argv:
        shutil.make_archive(str(out), "zip", out)
        print(f"wrote {out}.zip")
    print(f"wrote {out} ({N_FRAMES} frames)")


def write_points_ply(path: Path, pts: np.ndarray, cols: np.ndarray):
    header = (
        "ply\nformat binary_little_endian 1.0\n"
        f"element vertex {len(pts)}\n"
        "property float x\nproperty float y\nproperty float z\n"
        "property uchar red\nproperty uchar green\nproperty uchar blue\nend_header\n"
    )
    with open(path, "wb") as f:
        f.write(header.encode())
        for p, c in zip(pts.astype(np.float32), cols):
            f.write(struct.pack("<fffBBB", *p, *c))


if __name__ == "__main__":
    main()
