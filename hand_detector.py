"""
hand_detector.py
================
MediaPipe HandLandmarker-based finger-gun pose detector.
Uses the MediaPipe Tasks API (mediapipe >= 0.10 / 1.x).

Model file
----------
Requires hand_landmarker.task in the working directory.
Download once with:
    python -c "
    import urllib.request
    urllib.request.urlretrieve(
        'https://storage.googleapis.com/mediapipe-models/hand_landmarker/hand_landmarker/float16/latest/hand_landmarker.task',
        'hand_landmarker.task'
    )
    "

Finger-gun definition  ("thumbs up with the index finger pointing out")
---------------------
  OK  Index finger extended
  OK  Thumb raised away from the fist
  NO  Middle finger curled
  NO  Ring finger curled
  NO  Pinky curled

All checks use MediaPipe's 3D world landmarks (metres, hand-centred), so they
work regardless of which way the hand is pointing — sideways, at the camera,
or up.  The index finger's direction is what aims; see tag_detector.pick_aimed_tag.

Returns a confidence score 0.0-1.0 (fraction of criteria met).
A score >= 0.75 means the hand is "armed"; "ready" (allowed to fire) means
every check in REQUIRED_TO_FIRE passes.

Thread-safety
-------------
Create one HandDetector per thread (HandLandmarker is not thread-safe).
"""

from __future__ import annotations

import pathlib
import numpy as np
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision
from typing import Optional


# ---------------------------------------------------------------------------
# Landmark indices (MediaPipe hand topology - same as before)
# ---------------------------------------------------------------------------
WRIST      = 0
THUMB_IP   = 3
THUMB_TIP  = 4
INDEX_MCP  = 5
INDEX_PIP  = 6
INDEX_TIP  = 8
MIDDLE_MCP = 9
MIDDLE_PIP = 10
MIDDLE_TIP = 12
RING_PIP   = 14
RING_TIP   = 16
PINKY_PIP  = 18
PINKY_TIP  = 20

# Tuning — "reach" = |wrist->tip| / |wrist->PIP| (see _reach).  Tuned on real
# webcam frames: MediaPipe reads loosely tucked fingers at ~1.05-1.25 reach, so
# curl is judged *relative to the index* rather than against a fixed cut-off.
INDEX_MIN_REACH = 1.0  # index must reach at least this far
CURL_GAP        = 0.15 # middle/ring must reach at least this much less than the index
THUMB_OUT_RATIO = 0.7  # thumb raised if |thumb tip->index knuckle| > this * palm length

# Checks that must all pass to fire.  The pinky is shown but not required: it
# reads noisily when tucked and was the lone failing check in most near-misses.
REQUIRED_TO_FIRE = ('index_extended', 'thumb_extended', 'middle_curled', 'ring_curled')

DEFAULT_MODEL = pathlib.Path(__file__).parent / 'hand_landmarker.task'


class HandDetector:
    """
    Wraps MediaPipe HandLandmarker (Tasks API) for finger-gun detection.

    Parameters
    ----------
    model_path : path to hand_landmarker.task
    num_hands  : max hands to detect.  2 by default so a second hand in view
                 (e.g. one holding a phone) can't hide the shooting hand.
    min_hand_detection_confidence
    min_hand_presence_confidence
    min_tracking_confidence
    """

    def __init__(
        self,
        model_path: str | pathlib.Path = DEFAULT_MODEL,
        num_hands: int = 2,
        min_hand_detection_confidence: float = 0.6,
        min_hand_presence_confidence: float = 0.5,
        min_tracking_confidence: float = 0.5,
    ):
        base_opts = mp_python.BaseOptions(model_asset_path=str(model_path))
        opts = mp_vision.HandLandmarkerOptions(
            base_options=base_opts,
            running_mode=mp_vision.RunningMode.IMAGE,  # one frame at a time
            num_hands=num_hands,
            min_hand_detection_confidence=min_hand_detection_confidence,
            min_hand_presence_confidence=min_hand_presence_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )
        self._landmarker = mp_vision.HandLandmarker.create_from_options(opts)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _lm(landmarks, idx) -> np.ndarray:
        lm = landmarks[idx]
        return np.array([lm.x, lm.y, lm.z], dtype=np.float32)

    def _dist(self, lms, a: int, b: int) -> float:
        return float(np.linalg.norm(self._lm(lms, a) - self._lm(lms, b)))

    def _reach(self, lms, tip_idx: int, pip_idx: int) -> float:
        """How far the fingertip reaches past its PIP joint, measured from the wrist.
        ~1.35 when straight, ~1.05 bent 90 deg at the PIP, < 0.9 folded into a fist."""
        return self._dist(lms, WRIST, tip_idx) / max(self._dist(lms, WRIST, pip_idx), 1e-6)

    def _thumb_extended(self, lms) -> bool:
        """True when the thumb tip is held away from the index knuckle (thumbs-up),
        rather than tucked across the curled fingers."""
        palm = max(self._dist(lms, WRIST, MIDDLE_MCP), 1e-6)
        return self._dist(lms, THUMB_TIP, INDEX_MCP) / palm > THUMB_OUT_RATIO

    def _criteria(self, world) -> dict:
        index = self._reach(world, INDEX_TIP, INDEX_PIP)
        return {
            'index_extended': index >= INDEX_MIN_REACH,
            'thumb_extended': self._thumb_extended(world),
            'middle_curled':  self._reach(world, MIDDLE_TIP, MIDDLE_PIP) <= index - CURL_GAP,
            'ring_curled':    self._reach(world, RING_TIP,   RING_PIP)   <= index - CURL_GAP,
            # The pinky reads noisily when tucked, so it only has to be no further out than the index
            'pinky_curled':   self._reach(world, PINKY_TIP,  PINKY_PIP)  <= index,
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process(self, rgb_frame: np.ndarray) -> dict:
        """
        Run finger-gun detection on one RGB frame.

        Parameters
        ----------
        rgb_frame : np.ndarray  shape (H, W, 3), dtype uint8, RGB

        Returns
        -------
        dict — fields for the best (highest-confidence) hand, plus all hands:
            confidence : float       0.0-1.0 (fraction of criteria met)
            detected   : bool        any hand found?
            armed      : bool        confidence >= 0.75
            ready      : bool        every REQUIRED_TO_FIRE check passes
            landmarks  : list|None   [(x, y), ...] normalised, or None
            criteria   : dict        per-criterion booleans (for debug)
            hands      : list        {confidence, armed, landmarks, criteria} per hand
        """
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        result   = self._landmarker.detect(mp_image)

        hands = []
        # image landmarks (normalised, for drawing / aiming) + world landmarks
        # (3D metres, for orientation-free pose checks)
        for lms, world in zip(result.hand_landmarks, result.hand_world_landmarks):
            criteria   = self._criteria(world)
            confidence = sum(criteria.values()) / len(criteria)
            hands.append({
                'confidence': round(confidence, 3),
                'armed':      confidence >= 0.75,
                'ready':      all(criteria[k] for k in REQUIRED_TO_FIRE),
                'landmarks':  [(lm.x, lm.y) for lm in lms],
                'criteria':   criteria,
            })

        if not hands:
            return {
                'confidence': 0.0,
                'detected':   False,
                'armed':      False,
                'ready':      False,
                'landmarks':  None,
                'criteria':   {},
                'hands':      [],
            }

        best = max(hands, key=lambda h: (h['ready'], h['confidence']))
        return {**best, 'detected': True, 'hands': hands}

    def close(self):
        self._landmarker.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
