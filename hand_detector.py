"""
hand_detector.py
================
MediaPipe-based finger-gun pose detector.

Finger-gun definition
---------------------
  ✓ Index finger fully extended (tip above PIP joint in image coords)
  ✓ Thumb extended (tip further from wrist than IP joint)
  ✗ Middle finger curled
  ✗ Ring finger curled
  ✗ Pinky curled

Returns a confidence score 0.0–1.0 (fraction of criteria met).
A score ≥ 0.75 means the hand is "armed"; ≥ 0.85 means "fire".

Thread-safety
-------------
Create one HandDetector per thread (MediaPipe is not thread-safe).
Use _get_thread_local_detector() in server.py.
"""

import numpy as np
import mediapipe as mp
from typing import Optional


# ---------------------------------------------------------------------------
# Landmark indices (from MediaPipe hand topology)
# ---------------------------------------------------------------------------
WRIST       = 0
THUMB_CMC   = 1
THUMB_MCP   = 2
THUMB_IP    = 3
THUMB_TIP   = 4
INDEX_MCP   = 5
INDEX_PIP   = 6
INDEX_DIP   = 7
INDEX_TIP   = 8
MIDDLE_MCP  = 9
MIDDLE_PIP  = 10
MIDDLE_DIP  = 11
MIDDLE_TIP  = 12
RING_MCP    = 13
RING_PIP    = 14
RING_DIP    = 15
RING_TIP    = 16
PINKY_MCP   = 17
PINKY_PIP   = 18
PINKY_DIP   = 19
PINKY_TIP   = 20


class HandDetector:
    """
    Wraps MediaPipe Hands for single-hand finger-gun detection.

    Parameters
    ----------
    model_complexity : 0 or 1
        0 = fastest (recommended for real-time), 1 = more accurate.
    min_detection_confidence : float
    min_tracking_confidence  : float
    """

    def __init__(
        self,
        model_complexity: int = 0,
        min_detection_confidence: float = 0.6,
        min_tracking_confidence: float = 0.5,
    ):
        _mp = mp.solutions.hands
        self._hands = _mp.Hands(
            static_image_mode=False,
            max_num_hands=1,
            model_complexity=model_complexity,
            min_detection_confidence=min_detection_confidence,
            min_tracking_confidence=min_tracking_confidence,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _lm(landmarks, idx) -> np.ndarray:
        """Return (x, y, z) array for landmark at index."""
        lm = landmarks.landmark[idx]
        return np.array([lm.x, lm.y, lm.z], dtype=np.float32)

    def _tip_extended(self, lms, tip_idx: int, pip_idx: int) -> bool:
        """
        True when the fingertip is 'above' the PIP joint.
        In image coordinates y=0 is at the top, so 'above' means smaller y.
        """
        return lms.landmark[tip_idx].y < lms.landmark[pip_idx].y

    def _thumb_extended(self, lms) -> bool:
        """
        True when the thumb tip is further from the wrist than the IP joint.
        Works regardless of hand orientation.
        """
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
        rgb_frame : np.ndarray  shape (H, W, 3), dtype uint8, RGB colour order

        Returns
        -------
        dict
            confidence : float       0.0–1.0  (fraction of criteria met)
            detected   : bool        whether any hand was found at all
            armed      : bool        confidence >= 0.75
            landmarks  : list|None   [(x, y), ...] normalised coords, or None
            criteria   : dict        per-criterion boolean flags (for debugging)
        """
        result = self._hands.process(rgb_frame)

        if not result.multi_hand_landmarks:
            return {
                'confidence': 0.0,
                'detected':   False,
                'armed':      False,
                'landmarks':  None,
                'criteria':   {},
            }

        lms = result.multi_hand_landmarks[0]

        # ── Evaluate each finger-gun criterion ──────────────────────────
        criteria = {
            'index_extended': self._tip_extended(lms, INDEX_TIP,  INDEX_PIP),
            'thumb_extended': self._thumb_extended(lms),
            'middle_curled':  not self._tip_extended(lms, MIDDLE_TIP, MIDDLE_PIP),
            'ring_curled':    not self._tip_extended(lms, RING_TIP,   RING_PIP),
            'pinky_curled':   not self._tip_extended(lms, PINKY_TIP,  PINKY_PIP),
        }

        confidence = sum(criteria.values()) / len(criteria)
        landmarks  = [(lm.x, lm.y) for lm in lms.landmark]

        return {
            'confidence': round(confidence, 3),
            'detected':   True,
            'armed':      confidence >= 0.75,
            'landmarks':  landmarks,
            'criteria':   criteria,
        }

    def close(self):
        """Release MediaPipe resources."""
        self._hands.close()

    # Allow use as a context manager
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()
