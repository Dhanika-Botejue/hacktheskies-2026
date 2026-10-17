# -*- coding: utf-8 -*-
"""
tag_detector.py
===============
AprilTag detector using pupil-apriltags — a Python wrapper around the C
apriltag3 library.

Each player wears a printed bib (front and back) with their player-ID tag.
The shooter's rear camera detects the target's bib tag, giving us the target
player ID without any network communication.

Tag family
----------
TAG_FAMILY = tag25h9: a 7x7-cell tag (vs 8x8 for tag36h11), so each cell is
bigger at the same print size.  Measured on webcam frames with blur, it reads
~25% smaller (= farther) tags than tag36h11, which matters indoors where
lower light means more motion blur.  It has 35 IDs (0-34) — enough for the
shoot-only game.  gen_tags.py prints tags in this same family.

Tag ID mapping
--------------
  IDs 1–99   : player bibs  (tag ID == player ID; tag25h9 only goes up to 34)
  IDs 100–199: base markers (tag ID - 100 == team index, 0=red, 1=blue)
  IDs 200+   : flag markers (tag ID - 200 == flag index)
  Bases/flags need a family with more IDs (e.g. tag36h11) — unused in shoot-only.

Thread-safety
-------------
Create one TagDetector per thread (the underlying C library has per-handle
state). Use _get_thread_local_detector() in server.py.
"""

from __future__ import annotations

import math
from typing import Optional
import numpy as np
import pupil_apriltags as apriltag


TAG_FAMILY = 'tag25h9'

# Minimum decision margin (0–∞).  Values < 20 are usually false positives.
_MIN_MARGIN = 20.0


class TagDetector:
    """
    Thin wrapper around pupil_apriltags.Detector tuned for phone cameras.

    Parameters
    ----------
    quad_decimate : float
        Down-scale factor before quad detection.  2.0 gives a good
        speed/accuracy trade-off for 720p frames.
    nthreads : int
        Internal C-library threads (independent of Python's thread pool).
    """

    def __init__(self, quad_decimate: float = 2.0, nthreads: int = 2):
        self._detector = apriltag.Detector(
            families          = TAG_FAMILY,
            nthreads          = nthreads,
            quad_decimate     = quad_decimate,
            quad_sigma        = 0.0,
            refine_edges      = 1,
            decode_sharpening = 0.25,
            debug             = 0,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def detect(self, gray: np.ndarray) -> list[dict]:
        """
        Detect all AprilTags in a grayscale frame.

        Parameters
        ----------
        gray : np.ndarray  shape (H, W), dtype uint8, single channel

        Returns
        -------
        list of dicts, each:
            id      : int          tag ID
            center  : (float, float)   pixel coords of tag centre
            corners : [[x,y] x 4]  clock-wise corner pixels
            margin  : float        detection confidence (higher = better)
            role    : str          'player' | 'base' | 'flag' | 'unknown'
        """
        raw = self._detector.detect(gray)
        results = []
        for d in raw:
            margin = float(d.decision_margin)
            if margin < _MIN_MARGIN:
                continue
            tag_id = int(d.tag_id)
            results.append({
                'id':      tag_id,
                'center':  (float(d.center[0]), float(d.center[1])),
                'corners': d.corners.tolist(),
                'margin':  round(margin, 1),
                'role':    _tag_role(tag_id),
            })
        return results

    def best_centered_tag(
        self,
        gray: np.ndarray,
        frame_w: int,
        frame_h: int,
        role_filter: Optional[str] = None,
    ) -> Optional[dict]:
        """
        Return the tag closest to the image centre, optionally filtered by role.

        This is the tag the player is most likely aiming at.

        Parameters
        ----------
        gray         : grayscale frame
        frame_w/h    : frame dimensions (used to compute centre)
        role_filter  : if given, only consider tags with this role
                       ('player', 'base', 'flag')

        Returns
        -------
        dict (same structure as detect()) or None
        """
        tags = self.detect(gray)
        if role_filter:
            tags = [t for t in tags if t['role'] == role_filter]
        if not tags:
            return None

        cx, cy = frame_w / 2.0, frame_h / 2.0
        return min(
            tags,
            key=lambda t: (t['center'][0] - cx) ** 2 + (t['center'][1] - cy) ** 2,
        )

    def player_id_from_tag(self, tag: dict) -> Optional[int]:
        """Extract player ID from a 'player' role tag, else None."""
        if tag and tag['role'] == 'player':
            return tag['id']  # ID == player ID by convention
        return None

    # Allow use as a context manager
    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass  # pupil_apriltags has no explicit close()


# ------------------------------------------------------------------
# Finger aiming
# ------------------------------------------------------------------

# Below this length (px) the finger points (almost) straight at/away from the
# camera and its 2D direction is noise — fall back to "tag under the fingertip".
AIM_MIN_FINGER_PX = 20.0
# Hit if the finger ray passes within this many tag-radii of the tag centre...
AIM_TOLERANCE = 1.3
# ...or within this angle of it, seen from the fingertip.  Far-away tags are tiny,
# but the finger's direction is only accurate to a few degrees.
AIM_MIN_ANGLE_DEG = 5.0


def pick_aimed_tag(
    tags: list[dict],
    knuckle_xy: tuple[float, float],
    tip_xy: tuple[float, float],
) -> Optional[dict]:
    """
    Return the tag the index finger points at, or None.

    Casts a 2D ray from the index knuckle through the fingertip (pixel coords)
    and picks the tag whose centre lies closest to that ray, relative to how
    much miss is allowed for it (see AIM_TOLERANCE / AIM_MIN_ANGLE_DEG).
    Tags behind the fingertip are ignored.
    """
    if not tags:
        return None

    kx, ky = knuckle_xy
    tx, ty = tip_xy
    dx, dy = tx - kx, ty - ky
    length = math.hypot(dx, dy)
    angle_tan = math.tan(math.radians(AIM_MIN_ANGLE_DEG))

    best, best_score = None, 1.0
    for t in tags:
        cx, cy = t['center']
        radius  = max(math.hypot(cx - x, cy - y) for x, y in t['corners'])
        allowed = AIM_TOLERANCE * max(radius, 1.0)
        vx, vy  = cx - tx, cy - ty

        if length < AIM_MIN_FINGER_PX:
            miss = math.hypot(vx, vy)
        else:
            ux, uy = dx / length, dy / length
            along  = vx * ux + vy * uy
            if along < -radius:               # tag is behind the fingertip
                continue
            miss    = abs(vx * uy - vy * ux)  # perpendicular distance to the ray
            allowed = max(allowed, along * angle_tan)

        score = miss / allowed
        if score <= best_score:
            best, best_score = t, score
    return best


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

def _tag_role(tag_id: int) -> str:
    if 1 <= tag_id <= 99:
        return 'player'
    if 100 <= tag_id <= 199:
        return 'base'
    if tag_id >= 200:
        return 'flag'
    return 'unknown'
