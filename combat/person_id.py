# -*- coding: utf-8 -*-
"""
person_id.py
============
Tells players apart by what they're wearing, instead of a printed bib.

Before the game every player is scanned at the scan station (/scan, see
static/scan.html): they turn around slowly in front of a phone camera and
SCAN_SAMPLES views of their clothes are kept (Gallery).  In the game the people
in a shooter's frame are found (PersonDetector.detect), the ones near the
finger's line are described the same way and matched against those scans
(PeopleMatcher), and the person the finger points at is the target
(pick_aimed_person).

Descriptor
----------
MediaPipe's pose landmarker runs on each person's crop: its landmarks give the
shirt (shoulders -> hips) and trousers (hips -> mid-shin) rows, and its
segmentation mask keeps only the person's own pixels — a plain crop is mostly
background whenever someone walks, turns or stands next to something.  Each
part becomes a colour histogram: HUE_BINS soft hue bins weighted by how
colourful a pixel is, plus GREY_BINS soft brightness bins (black / grey /
white) for the rest, so dark and grey clothes — the usual case — are described
too.  Similarity is the Bhattacharyya coefficient (1 = identical).

Never trust one frame
---------------------
A loaded gun fires on the first aimed frame (game_state.Gun), so a single
misread would score a hit on the wrong player.  A person only gets an ID when
the same player was a clear match within CONFIRM_S before as well.

Thread-safety
-------------
Create one PersonDetector per thread (like HandDetector).  Gallery is
copy-on-write, so worker threads can read it while a scan replaces an entry.
"""
from __future__ import annotations

import json
import math
import pathlib
import time
from typing import Optional

import cv2
import numpy as np

_HERE = pathlib.Path(__file__).parent
PERSON_MODEL = _HERE / 'efficientdet_lite2.tflite'
PERSON_MODEL_URL = ('https://storage.googleapis.com/mediapipe-models/object_detector/'
                    'efficientdet_lite2/int8/latest/efficientdet_lite2.tflite')
POSE_MODEL = _HERE / 'pose_landmarker_lite.task'
POSE_MODEL_URL = ('https://storage.googleapis.com/mediapipe-models/pose_landmarker/'
                  'pose_landmarker_lite/float16/latest/pose_landmarker_lite.task')
MIN_PERSON_SCORE = 0.35
# Pose runs on the person's box plus this much of its width / height on each side
# (measured on street photos: the most bodies found, the fewest neighbours picked up)
CROP_PAD_X, CROP_PAD_Y = 0.3, 0.1

# Descriptor
HUE_BINS    = 12
GREY_LEVELS = (40.0, 128.0, 215.0)   # soft brightness bins: black, grey, white (0-255)
GREY_BINS   = len(GREY_LEVELS)
MIN_PART_PX = 60             # fewer usable pixels = that part can't be described
UPPER_WEIGHT = 0.6           # shirt vs trousers in similarity()

# Matching
TOP_K      = 3      # a player's score = mean of their TOP_K most similar scan views
MIN_SIM    = 0.80   # the best match must be at least this similar...
MIN_MARGIN = 0.05   # ...and this much more similar than the runner-up
CONFIRM_S  = 0.4    # and the same player matched clearly within this long before

# Scan station
SCAN_SAMPLES    = 24     # views kept per player
SCAN_SAMPLE_S   = 0.3    # at most one view per this long: 24 views = one slow full turn (~8 s)
SCAN_MIN_HEIGHT = 0.35   # the player must fill at least this fraction of the frame height
LOOKALIKE_RATE  = 0.25   # flag two players when this share of one's views can't be told from the other

# The wearer's own arm: a box that touches the frame bottom and starts below this
# fraction of the frame height.  A standing person's head is always above a
# chest camera's midline, however close they are.
OWN_ARM_TOP = 0.45

# Aiming: a ray through a body box hits it; up to PERSON_AIM_SLACK x box width
# beside the box (or within AIM_MIN_ANGLE_DEG, for small far-away people) still counts.
PERSON_AIM_SLACK  = 0.25
AIM_MIN_ANGLE_DEG = 12.0
AIM_MIN_FINGER_PX = 20.0

# Pose landmark indices (MediaPipe pose topology)
_SHOULDERS, _HIPS, _KNEES, _ANKLES = (11, 12), (23, 24), (25, 26), (27, 28)


# ------------------------------------------------------------------
# Detection + description
# ------------------------------------------------------------------

class PersonDetector:
    """
    Finds people (MediaPipe ObjectDetector, EfficientDet-Lite2: reads people
    ~40% smaller than Lite0, ~22 ms a frame on a laptop) and describes their
    clothes (MediaPipe PoseLandmarker lite on each person's crop, ~12 ms each).
    """

    def __init__(self, model_path: str | pathlib.Path = PERSON_MODEL,
                 pose_model_path: str | pathlib.Path = POSE_MODEL,
                 score_threshold: float = MIN_PERSON_SCORE, max_results: int = 8):
        import mediapipe
        from mediapipe.tasks import python as mp_python
        from mediapipe.tasks.python import vision as mp_vision
        for path, url in ((model_path, PERSON_MODEL_URL), (pose_model_path, POSE_MODEL_URL)):
            if not pathlib.Path(path).is_file():
                raise FileNotFoundError(f'{path} is missing: download it from {url}')
        self._mp = mediapipe
        self._detector = mp_vision.ObjectDetector.create_from_options(mp_vision.ObjectDetectorOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(model_path)),
            running_mode=mp_vision.RunningMode.IMAGE,
            category_allowlist=['person'],
            score_threshold=score_threshold,
            max_results=max_results,
        ))
        # Low confidences: far-away people are small and blurry; the crop holds one person anyway
        self._pose = mp_vision.PoseLandmarker.create_from_options(mp_vision.PoseLandmarkerOptions(
            base_options=mp_python.BaseOptions(model_asset_path=str(pose_model_path)),
            running_mode=mp_vision.RunningMode.IMAGE,
            num_poses=1,
            output_segmentation_masks=True,
            min_pose_detection_confidence=0.3,
            min_pose_presence_confidence=0.3,
        ))

    def _image(self, rgb: np.ndarray):
        return self._mp.Image(image_format=self._mp.ImageFormat.SRGB, data=np.ascontiguousarray(rgb))

    def detect(self, rgb: np.ndarray) -> list[dict]:
        """People in an RGB frame: [{'box': (x0, y0, x1, y1) pixels, 'score'}], biggest first."""
        h, w = rgb.shape[:2]
        people = []
        for d in self._detector.detect(self._image(rgb)).detections:
            b = d.bounding_box
            x0, y0 = max(0.0, float(b.origin_x)), max(0.0, float(b.origin_y))
            x1, y1 = min(float(w), float(b.origin_x + b.width)), min(float(h), float(b.origin_y + b.height))
            if x1 - x0 >= 4 and y1 - y0 >= 8:
                people.append({'box': (x0, y0, x1, y1), 'score': round(float(d.categories[0].score), 2)})
        return sorted(people, key=lambda p: -(p['box'][3] - p['box'][1]))

    def describe(self, rgb: np.ndarray, box, mask_out: Optional[np.ndarray] = None) -> Optional[dict]:
        """
        Clothing descriptor of the person in `box` (see clothing_descriptor), or
        None when no body is found in the crop.  mask_out (H x W bool, whole
        frame) marks pixels to ignore: the shooter's own hand in front of them.
        """
        h, w = rgb.shape[:2]
        x0, y0, x1, y1 = box
        bw, bh = x1 - x0, y1 - y0
        X0, Y0 = int(max(0, x0 - CROP_PAD_X * bw)), int(max(0, y0 - CROP_PAD_Y * bh))
        X1, Y1 = int(min(w, x1 + CROP_PAD_X * bw)), int(min(h, y1 + CROP_PAD_Y * bh))
        if X1 - X0 < 8 or Y1 - Y0 < 16:
            return None
        crop = rgb[Y0:Y1, X0:X1]
        result = self._pose.detect(self._image(crop))
        if not result.pose_landmarks or not result.segmentation_masks:
            return None
        lms = result.pose_landmarks[0]
        ch, cw = crop.shape[:2]
        x = lambda ids: float(np.mean([lms[i].x for i in ids])) * cw
        y = lambda ids: float(np.mean([lms[i].y for i in ids])) * ch
        # The body found must be this person, not a neighbour inside the padding
        if not x0 <= X0 + (x(_SHOULDERS) + x(_HIPS)) / 2 <= x1:
            return None
        shoulder, hip, knee, ankle = y(_SHOULDERS), y(_HIPS), y(_KNEES), y(_ANKLES)
        if hip - shoulder < 4:
            return None
        body = result.segmentation_masks[0].numpy_view()
        body = (body[..., 0] if body.ndim == 3 else body) > 0.5
        if mask_out is not None:
            body &= ~mask_out[Y0:Y1, X0:X1]
        rows = np.arange(ch)[:, None]
        upper = body & (rows >= shoulder) & (rows < hip)
        lower = body & (rows >= hip + 0.05 * (knee - hip)) & (rows < knee + 0.5 * (ankle - knee))
        return clothing_descriptor(crop, upper, lower)

    def close(self):
        self._detector.close()
        self._pose.close()


def clothing_descriptor(rgb: np.ndarray, upper: np.ndarray, lower: np.ndarray) -> Optional[dict]:
    """
    {'upper': shirt histogram, 'lower': trousers histogram or None} from the
    pixels each mask keeps, or None if the shirt isn't visible enough.
    """
    hsv = cv2.cvtColor(rgb, cv2.COLOR_RGB2HSV)
    shirt = _part_hist(hsv, upper)
    if shirt is None:
        return None
    return {'upper': shirt, 'lower': _part_hist(hsv, lower)}


def _part_hist(hsv: np.ndarray, keep: np.ndarray) -> Optional[np.ndarray]:
    """Soft hue + grey histogram of the kept pixels of one body part, summing to 1."""
    px = hsv[keep].astype(np.float32)
    if len(px) < MIN_PART_PX:
        return None
    hue, sat, val = px[:, 0] * (HUE_BINS / 180.0), px[:, 1] / 255.0, px[:, 2]
    # How colourful a pixel is (0 = grey): low saturation, or too dark to have a reliable hue
    colour = np.clip((sat - 0.15) / 0.35, 0, 1) * np.clip((val - 30.0) / 50.0, 0, 1)

    # Hue: each pixel split between its two nearest bins (wrapping around red)
    lo = np.floor(hue - 0.5)
    frac = (hue - 0.5) - lo
    lo = lo.astype(int) % HUE_BINS
    hues = (np.bincount(lo, colour * (1 - frac), HUE_BINS) +
            np.bincount((lo + 1) % HUE_BINS, colour * frac, HUE_BINS))
    hues = (np.roll(hues, 1) + 2 * hues + np.roll(hues, -1)) / 4   # tolerate white-balance shifts

    # Grey: brightness split between the two nearest GREY_LEVELS
    levels = np.array(GREY_LEVELS)
    v = np.clip(val, levels[0], levels[-1])
    i = np.clip(np.searchsorted(levels, v) - 1, 0, GREY_BINS - 2)
    f = (v - levels[i]) / (levels[i + 1] - levels[i])
    grey = 1 - colour
    greys = np.bincount(i, grey * (1 - f), GREY_BINS) + np.bincount(i + 1, grey * f, GREY_BINS)

    hist = np.concatenate([hues, greys])
    return hist / max(hist.sum(), 1e-9)


def _bc(a: np.ndarray, b: np.ndarray) -> float:
    """Bhattacharyya coefficient: 1 = identical histograms, 0 = nothing in common."""
    return float(np.sum(np.sqrt(a * b)))


def similarity(a: dict, b: dict) -> float:
    """0..1, how alike two clothing descriptors are.  Trousers count only when both have them."""
    upper = _bc(a['upper'], b['upper'])
    if a.get('lower') is None or b.get('lower') is None:
        return upper
    return UPPER_WEIGHT * upper + (1 - UPPER_WEIGHT) * _bc(a['lower'], b['lower'])


def hands_mask(hands: list[list[tuple[float, float]]], w: int, h: int, grow: float = 1.4) -> Optional[np.ndarray]:
    """Whole-frame bool mask over the given hands (normalised landmarks), each
    hand's outline grown by `grow` around its centre, or None for no hands."""
    if not hands:
        return None
    mask = np.zeros((h, w), np.uint8)
    for lms in hands:
        pts = np.array([(x * w, y * h) for x, y in lms], np.float32)
        centre = pts.mean(axis=0)
        hull = cv2.convexHull(((pts - centre) * grow + centre).astype(np.int32))
        cv2.fillConvexPoly(mask, hull, 1)
    return mask.astype(bool)


# ------------------------------------------------------------------
# Boxes
# ------------------------------------------------------------------

def is_own_arm(box, w: int, h: int) -> bool:
    """The wearer's own arm (or the bottom of their own body), not another player."""
    return box[3] >= 0.98 * h and box[1] >= OWN_ARM_TOP * h


def box_corners(box) -> list[list[float]]:
    x0, y0, x1, y1 = box
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def box_iou(a, b) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ------------------------------------------------------------------
# Gallery (the pre-game scans)
# ------------------------------------------------------------------

def _to_json(desc: dict) -> dict:
    return {k: (None if v is None else [round(float(x), 5) for x in v]) for k, v in desc.items()}


def _from_json(d: dict) -> dict:
    return {k: (None if v is None else np.array(v, np.float32)) for k, v in d.items()}


class Gallery:
    """
    {player_id: [descriptor, ...]} from the scan station, saved to `path` (JSON)
    so a server restart keeps everyone's scan.  Every write replaces the dict,
    so readers in other threads always see a complete one.
    """

    def __init__(self, path: Optional[pathlib.Path] = None):
        self.path = path
        self._views: dict[int, list[dict]] = {}
        self.scanned_at: dict[int, float] = {}
        if path and path.is_file():
            data = json.loads(path.read_text())
            for pid, entry in data.get('players', {}).items():
                self._views[int(pid)] = [_from_json(v) for v in entry['views']]
                self.scanned_at[int(pid)] = entry.get('scanned_at', 0.0)

    def __contains__(self, player_id) -> bool:
        return player_id in self._views

    def players(self) -> list[int]:
        return sorted(self._views)

    def views(self, player_id: int) -> list[dict]:
        return self._views.get(player_id, [])

    def set(self, player_id: int, views: list[dict]) -> None:
        self._views = {**self._views, player_id: list(views)}
        self.scanned_at = {**self.scanned_at, player_id: time.time()}
        self._save()

    def remove(self, player_id: int) -> bool:
        if player_id not in self._views:
            return False
        self._views = {k: v for k, v in self._views.items() if k != player_id}
        self.scanned_at = {k: v for k, v in self.scanned_at.items() if k != player_id}
        self._save()
        return True

    def _save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {'players': {str(pid): {'scanned_at': self.scanned_at.get(pid, 0.0),
                                       'views': [_to_json(v) for v in views]}
                            for pid, views in self._views.items()}}
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(data))
        tmp.replace(self.path)

    @staticmethod
    def _score(desc: dict, views: list[dict]) -> float:
        sims = sorted((similarity(desc, v) for v in views), reverse=True)
        return float(np.mean(sims[:TOP_K])) if sims else 0.0

    def scores(self, desc: dict, exclude=()) -> dict[int, float]:
        """How well `desc` matches each scanned player (except `exclude`): {player_id: 0..1}."""
        views = self._views
        return {pid: self._score(desc, v) for pid, v in views.items() if pid not in exclude}

    def lookalikes(self, player_id: int) -> list[tuple[int, float]]:
        """
        Players this one could be mistaken for: [(other, share of views)], worst first.
        Each scan view is matched against everyone as if it came from a game frame
        (leaving out that view and its neighbours, which are nearly the same picture);
        a view is confusable when another player scores within MIN_MARGIN of the
        right one.  Flagged when that happens for LOOKALIKE_RATE of the views or more.
        """
        views = self._views
        mine = views.get(player_id, [])
        if len(mine) < 2:
            return []
        hits: dict[int, int] = {}
        for i, desc in enumerate(mine):
            own = self._score(desc, [v for j, v in enumerate(mine) if abs(i - j) > 2] or mine)
            for pid, theirs in views.items():
                if pid != player_id and self._score(desc, theirs) >= own - MIN_MARGIN:
                    hits[pid] = hits.get(pid, 0) + 1
        rates = [(pid, round(n / len(mine), 2)) for pid, n in hits.items()]
        return sorted([r for r in rates if r[1] >= LOOKALIKE_RATE], key=lambda r: -r[1])

    def confusable(self) -> list[tuple[int, int, float]]:
        """Every look-alike pair once: [(a, b, share of views)], worst first."""
        pairs: dict[tuple[int, int], float] = {}
        for pid in self.players():
            for other, rate in self.lookalikes(pid):
                key = (min(pid, other), max(pid, other))
                pairs[key] = max(pairs.get(key, 0.0), rate)
        return sorted([(a, b, r) for (a, b), r in pairs.items()], key=lambda p: -p[2])


# ------------------------------------------------------------------
# Matching in the game (one PeopleMatcher per phone)
# ------------------------------------------------------------------

class PeopleMatcher:
    """
    Turns per-frame gallery scores into player IDs for one shooter's camera.
    A person gets an ID only if (1) this frame's best match is clear — at least
    MIN_SIM, and MIN_MARGIN ahead of the runner-up — and (2) the same player was
    a clear match within CONFIRM_S before too.  At most one person per ID per frame.
    Box positions aren't used: a chest camera swings a person across half the
    frame between two frames when its wearer turns.
    """

    def __init__(self, player_id: Optional[int] = None):
        self.player_id = player_id               # the shooter: never a candidate
        self.last_clear: dict[int, float] = {}   # player -> last time a clear match

    def reset(self) -> None:
        self.last_clear = {}

    def identify(self, scored: list[dict[int, float]], now: float) -> list[Optional[int]]:
        """scored: for each person in the frame, {player_id: score}.  Returns each one's ID or None."""
        clear: list[Optional[tuple[int, float]]] = []
        for scores in scored:
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            runner_up = ranked[1][1] if len(ranked) > 1 else 0.0
            ok = bool(ranked) and ranked[0][1] >= MIN_SIM and ranked[0][1] - runner_up >= MIN_MARGIN
            clear.append(ranked[0] if ok else None)

        # Two people can't both be the same player: the better match keeps it
        for i, c in enumerate(clear):
            if c and any(o and o[0] == c[0] and (o[1] > c[1] or (o[1] == c[1] and j < i))
                         for j, o in enumerate(clear) if j != i):
                clear[i] = None

        ids = []
        for c in clear:
            if c is None:
                ids.append(None)
                continue
            pid = c[0]
            ids.append(pid if now - self.last_clear.get(pid, float('-inf')) <= CONFIRM_S else None)
            self.last_clear[pid] = now
        return ids


# ------------------------------------------------------------------
# Aiming
# ------------------------------------------------------------------

def _aim_key(box, knuckle_xy, tip_xy) -> Optional[tuple[int, float]]:
    """
    How squarely the finger points at a body box, lower is better, or None when
    it doesn't point at it.  A ray through the middle half of the body beats one
    that only grazes it or passes close beside; among those through the middle,
    the closest (biggest) person is in front.
    """
    kx, ky = knuckle_xy
    tx, ty = tip_xy
    dx, dy = tx - kx, ty - ky
    length = math.hypot(dx, dy)
    x0, y0, x1, y1 = box
    bw, bh = x1 - x0, y1 - y0
    if length < AIM_MIN_FINGER_PX:
        # Pointing straight at the camera: the person under the fingertip
        miss = math.hypot(max(x0 - tx, 0, tx - x1), max(y0 - ty, 0, ty - y1))
        allowed = PERSON_AIM_SLACK * bw
        if miss > allowed:
            return None
        return (0, -bh) if miss == 0 else (1, miss / allowed)
    ux, uy = dx / length, dy / length
    vx, vy = (x0 + x1) / 2 - tx, (y0 + y1) / 2 - ty
    along = vx * ux + vy * uy
    if along <= 0:
        # Their middle is behind the fingertip: the finger points away from them,
        # even if the hand is in front of their body in the picture
        return None
    beside = abs(vx * uy - vy * ux)                      # the centre's distance from the ray
    half = abs(uy) * bw / 2 + abs(ux) * bh / 2           # the body's half-width across the ray
    allowed = max(PERSON_AIM_SLACK * bw, along * math.tan(math.radians(AIM_MIN_ANGLE_DEG)))
    if beside > half + allowed:
        return None
    return (0, -bh) if beside <= half / 2 else (1, beside / (half + allowed))


def aim_candidates(people: list[dict], knuckle_xy, tip_xy) -> list[dict]:
    """The people (dicts with 'box') the finger points at, best aimed first."""
    keyed = [(k, i) for i, p in enumerate(people) if (k := _aim_key(p['box'], knuckle_xy, tip_xy))]
    return [people[i] for _, i in sorted(keyed)]


def pick_aimed_person(people: list[dict], knuckle_xy, tip_xy) -> Optional[dict]:
    """
    The person the index finger points at, or None.  A 2D ray from the knuckle
    through the fingertip hits a body box when it passes through it, or close
    beside it (PERSON_AIM_SLACK, AIM_MIN_ANGLE_DEG).  People behind the
    fingertip don't count.  When the ray passes through several, the closest
    (biggest) one is in front.  A finger pointing straight at the camera aims
    at the person under the fingertip.
    """
    hit = aim_candidates(people, knuckle_xy, tip_xy)
    return hit[0] if hit else None


# ------------------------------------------------------------------
# Scan station
# ------------------------------------------------------------------

class ScanSession:
    """
    One player's scan: they turn around slowly in front of the camera, and a
    view of their clothes is kept at most every SCAN_SAMPLE_S, until SCAN_SAMPLES.
    Frames the scan can't use say why (HINTS).
    """

    HINTS = {
        'no_person':  'Nobody in view',
        'one_person': 'Only the player being scanned should be in view',
        'closer':     'Step closer',
        'step_back':  'Step back: head to feet must be in view',
        'no_body':    "Can't make out the body: face the camera, arms down",
    }

    def __init__(self, player_id: int):
        self.player_id = player_id
        self.views: list[dict] = []
        self.last_at = float('-inf')
        self.thumbnail: Optional[np.ndarray] = None   # RGB crop of the first view

    @property
    def done(self) -> bool:
        return len(self.views) >= SCAN_SAMPLES

    def wants_view(self, now: float) -> bool:
        return not self.done and now - self.last_at >= SCAN_SAMPLE_S

    def add(self, desc: dict, thumbnail: Optional[np.ndarray], now: float) -> None:
        self.views.append(desc)
        self.last_at = now
        if self.thumbnail is None:
            self.thumbnail = thumbnail

    @staticmethod
    def pick(people: list[dict], w: int, h: int) -> tuple[Optional[dict], Optional[str]]:
        """(the one person to scan, None), or (None, hint key) when the frame won't do."""
        if not people:
            return None, 'no_person'
        big = max(people, key=lambda p: p['box'][3] - p['box'][1])
        bh = big['box'][3] - big['box'][1]
        if sum(1 for p in people if p['box'][3] - p['box'][1] >= 0.5 * bh) > 1:
            return None, 'one_person'
        if big['box'][1] <= 0.01 * h or big['box'][3] >= 0.99 * h:
            return None, 'step_back'
        if bh < SCAN_MIN_HEIGHT * h:
            return None, 'closer'
        return big, None
