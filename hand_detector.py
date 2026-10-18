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

Finger-gun definition  (see ready_to_fire)
---------------------
  Required:  index finger extended, and middle OR ring finger curled
  Shown on the phone but not required: thumb raised, pinky curled
  (they read noisily when the hand is edge-on to the camera)

All checks use MediaPipe's 3D world landmarks (metres, hand-centred), so they
work regardless of which way the hand is pointing — sideways, at the camera,
or up.  The index finger's direction is what aims; see tag_detector.pick_aimed_tag.

Returns a confidence score 0.0-1.0 (fraction of criteria met).
A score >= 0.75 means the hand is "armed"; "ready" (allowed to fire) is
decided by ready_to_fire().

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
CURL_GAP        = 0.08 # middle/ring must reach at least this much less than the index
THUMB_OUT_RATIO = 0.7  # thumb raised if |thumb tip->index knuckle| > this * palm length


# One-handed play, clap to reload.
# Only the wearer's own hands count: from a chest camera they are big in the
# frame (palm ~15% of the frame at arm's length), while other players' hands
# across the room are tiny.  Without this, an opponent's hand could become the
# shooting hand, or an opponent clapping could reload your gun.
OWN_HAND_MIN_PALM = 0.06   # palm size (see _palm) / shorter frame side
# Clap detection (see ClapTracker).  Distances are between palm centres, in
# palm-lengths.  Real claps rarely show as two touching hands in one frame:
# pressed palms get merged into ONE hand, and a fast clap blurs both away.
CLAP_MAX_PALMS    = 2.8    # two hands this close = clap (touching hands measured 2.3-2.7 in play)
CLAP_NEAR_PALMS   = 4.5    # two hands this close = approaching...
CLAP_WINDOW_S     = 0.8    # ...then merging into one hand / vanishing within this = clap
# Your hands = the biggest (closest) hand, plus a second one only if it's at
# least this fraction of its size: your two hands are about equally far from
# the camera, while a noticeably smaller hand is further off (someone else's).
PAIR_SIZE_RATIO   = 0.6
_PALM = (0, 5, 9, 13, 17)   # wrist + the four finger knuckles


def _palm(hand: dict, w: int, h: int) -> tuple[np.ndarray, float]:
    """
    (palm centre in pixels, palm size in pixels).

    Size = the larger of the palm's length (wrist -> middle knuckle) and its
    width (index -> pinky knuckle, scaled to match a length).  The length alone
    collapses when the fingers point away from the camera — exactly how hands
    look from a chest camera when pointing forward or clapping in front of you —
    which made your own hands look tiny ("someone else's") and far apart.
    """
    lms = hand['landmarks']
    pts = np.array([(lms[i][0] * w, lms[i][1] * h) for i in _PALM])
    length = float(np.linalg.norm(pts[0] - pts[2]))
    width  = float(np.linalg.norm(pts[1] - pts[4])) * 1.25
    return pts.mean(axis=0), max(length, width)


def own_hands(hands: list[dict], w: int, h: int) -> list[dict]:
    """The wearer's hands, closest first: at most two, see PAIR_SIZE_RATIO."""
    min_palm = OWN_HAND_MIN_PALM * min(w, h)
    mine = sorted((hd for hd in hands if _palm(hd, w, h)[1] >= min_palm),
                  key=lambda hd: -_palm(hd, w, h)[1])
    if not mine:
        return []
    closest = _palm(mine[0], w, h)[1]
    return [hd for hd in mine if _palm(hd, w, h)[1] >= PAIR_SIZE_RATIO * closest][:2]


def shooting_hand(hands: list[dict], w: int, h: int) -> Optional[dict]:
    """One-handed only: your hand, when exactly one of your hands is in view.
    With both of yours in view there is no shooting hand — the second hand
    only comes in to clap (reload)."""
    mine = own_hands(hands, w, h)
    return mine[0] if len(mine) == 1 else None


def own_palms(hands: list[dict], w: int, h: int) -> list[tuple[tuple[float, float], float]]:
    """(palm centre, palm length) in pixels for each of the wearer's hands."""
    return [(tuple(map(float, c)), s) for c, s in (_palm(hd, w, h) for hd in own_hands(hands, w, h))]


class ClapTracker:
    """
    Detects a clap across frames (one tracker per player).  A clap is:
      - two hands within CLAP_MAX_PALMS of each other, or
      - two hands within CLAP_NEAR_PALMS (approaching), and within CLAP_WINDOW_S
        they merge into one hand near where they were meeting, or both vanish
        (motion blur at the moment of the clap).
    One hand simply leaving the frame doesn't count: the remaining hand is
    still where it was, away from the meeting point.
    """

    def __init__(self):
        self.near_at: Optional[float] = None
        self.near_mid = (0.0, 0.0)
        self.near_palm = 1.0
        self.last_gap: Optional[float] = None   # for logging

    def update(self, palms: list[tuple[tuple[float, float], float]], now: float) -> bool:
        # palms = own_palms(): only your (at most two, closest) hands
        if len(palms) >= 2:
            (ca, sa), (cb, sb) = palms
            size = max((sa + sb) / 2, 1e-6)
            gap  = float(np.hypot(ca[0] - cb[0], ca[1] - cb[1])) / size
            self.last_gap = round(gap, 2)
            if gap <= CLAP_MAX_PALMS:
                self.near_at = None
                return True
            if gap <= CLAP_NEAR_PALMS:
                self.near_at, self.near_palm = now, size
                self.near_mid = ((ca[0] + cb[0]) / 2, (ca[1] + cb[1]) / 2)
            else:
                self.near_at = None
            return False

        self.last_gap = None
        if self.near_at is None or now - self.near_at > CLAP_WINDOW_S:
            return False
        if not palms:                     # both hands blurred out as they met
            self.near_at = None
            return True
        (c, _), = palms                   # merged into one detection at the meeting point?
        if np.hypot(c[0] - self.near_mid[0], c[1] - self.near_mid[1]) <= 1.2 * self.near_palm:
            self.near_at = None
            return True
        return False


def ready_to_fire(criteria: dict) -> bool:
    """
    Lenient on purpose: index out plus middle OR ring tucked.  In real play logs
    most aimed-but-rejected frames failed thumb and/or one curl check (hand seen
    edge-on or foreshortened), so the thumb and pinky are shown but not required.
    An open hand still fails: its middle and ring reach as far as the index.
    """
    return criteria['index_extended'] and (criteria['middle_curled'] or criteria['ring_curled'])

DEFAULT_MODEL = pathlib.Path(__file__).parent / 'hand_landmarker.task'


class HandDetector:
    """
    Wraps MediaPipe HandLandmarker (Tasks API) for finger-gun detection.

    Parameters
    ----------
    model_path : path to hand_landmarker.task
    num_hands  : max hands to detect.  3 by default: your two hands (shooting +
                 clapping) plus one spare, so another player's hand in view
                 can't take a slot and hide one of yours.
    min_hand_detection_confidence
    min_hand_presence_confidence
    min_tracking_confidence
    """

    def __init__(
        self,
        model_path: str | pathlib.Path = DEFAULT_MODEL,
        num_hands: int = 3,
        # Lowish: a clapping hand moves fast and blurs.  False hands are filtered
        # out later anyway (too small to be the wearer's, see own_hands).
        min_hand_detection_confidence: float = 0.45,
        min_hand_presence_confidence: float = 0.4,
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
            ready      : bool        ready_to_fire(criteria)
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
                'ready':      ready_to_fire(criteria),
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
