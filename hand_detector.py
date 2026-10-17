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

Finger-gun definition
---------------------
  OK  Index finger fully extended (tip above PIP joint in image coords)
  OK  Thumb extended (tip further from wrist than IP joint)
  NO  Middle finger curled
  NO  Ring finger curled
  NO  Pinky curled

Returns a confidence score 0.0-1.0 (fraction of criteria met).
A score >= 0.75 means the hand is "armed"; >= 0.85 means "fire".

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
INDEX_PIP  = 6
INDEX_TIP  = 8
MIDDLE_PIP = 10
MIDDLE_TIP = 12
RING_PIP   = 14
RING_TIP   = 16
PINKY_PIP  = 18
PINKY_TIP  = 20

DEFAULT_MODEL = pathlib.Path(__file__).parent / 'hand_landmarker.task'


class HandDetector:
    """
    Wraps MediaPipe HandLandmarker (Tasks API) for single-hand finger-gun
    detection.

    Parameters
    ----------
    model_path : path to hand_landmarker.task
    num_hands  : max hands to detect (1 is enough, faster)
    min_hand_detection_confidence
    min_hand_presence_confidence
    min_tracking_confidence
    """

    def __init__(
        self,
        model_path: str | pathlib.Path = DEFAULT_MODEL,
        num_hands: int = 1,
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

    def _tip_extended(self, lms, tip_idx: int, pip_idx: int) -> bool:
        """True when fingertip is above (smaller y) the PIP joint."""
        return lms[tip_idx].y < lms[pip_idx].y

    def _thumb_extended(self, lms) -> bool:
        """True when thumb tip is further from wrist than IP joint."""
        wrist = self._lm(lms, WRIST)
        tip   = self._lm(lms, THUMB_TIP)
        ip    = self._lm(lms, THUMB_IP)
        return float(np.linalg.norm(tip - wrist)) > float(np.linalg.norm(ip - wrist))

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
        dict
            confidence : float       0.0-1.0 (fraction of criteria met)
            detected   : bool        any hand found?
            armed      : bool        confidence >= 0.75
            landmarks  : list|None   [(x, y), ...] normalised, or None
            criteria   : dict        per-criterion booleans (for debug)
        """
        mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_frame)
        result   = self._landmarker.detect(mp_image)

        if not result.hand_landmarks:
            return {
                'confidence': 0.0,
                'detected':   False,
                'armed':      False,
                'landmarks':  None,
                'criteria':   {},
            }

        lms = result.hand_landmarks[0]  # first detected hand

        criteria = {
            'index_extended': self._tip_extended(lms, INDEX_TIP,  INDEX_PIP),
            'thumb_extended': self._thumb_extended(lms),
            'middle_curled':  not self._tip_extended(lms, MIDDLE_TIP, MIDDLE_PIP),
            'ring_curled':    not self._tip_extended(lms, RING_TIP,   RING_PIP),
            'pinky_curled':   not self._tip_extended(lms, PINKY_TIP,  PINKY_PIP),
        }

        confidence = sum(criteria.values()) / len(criteria)
        landmarks  = [(lm.x, lm.y) for lm in lms]

        return {
            'confidence': round(confidence, 3),
            'detected':   True,
            'armed':      confidence >= 0.75,
            'landmarks':  landmarks,
            'criteria':   criteria,
        }

    def close(self):
        self._landmarker.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
