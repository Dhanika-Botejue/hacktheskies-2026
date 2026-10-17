"""Vectorized 2D geometry helpers. All coordinates are in meters, map frame (x right, y down)."""

import numpy as np


def segments_hit_walls(p: np.ndarray, q: np.ndarray, walls: np.ndarray) -> np.ndarray:
    """For N movement segments p[i]->q[i], return a bool array (N,) that is True
    where the segment crosses any wall.

    p, q: (N, 2) arrays. walls: (M, 4) array of [x1, y1, x2, y2].
    """
    n = p.shape[0]
    if walls.size == 0 or n == 0:
        return np.zeros(n, dtype=bool)

    a = walls[:, 0:2]  # (M, 2)
    e = walls[:, 2:4] - a  # (M, 2)
    d = q - p  # (N, 2)

    # Solve p + t*d = a + u*e for every (particle, wall) pair.
    ap_x = a[None, :, 0] - p[:, None, 0]  # (N, M)
    ap_y = a[None, :, 1] - p[:, None, 1]
    denom = d[:, None, 0] * e[None, :, 1] - d[:, None, 1] * e[None, :, 0]
    t_num = ap_x * e[None, :, 1] - ap_y * e[None, :, 0]
    u_num = ap_x * d[:, None, 1] - ap_y * d[:, None, 0]

    parallel = np.abs(denom) < 1e-12
    safe = np.where(parallel, 1.0, denom)
    t = t_num / safe
    u = u_num / safe
    hit = ~parallel & (t >= 0) & (t <= 1) & (u >= 0) & (u <= 1)
    return hit.any(axis=1)
