"""Per-instance geometry: ground-contact point and coarse posture.

These are deliberately simple and occlusion-tolerant. Posture from a single
oblique view is a coarse proxy (documented as such) and is meant to be replaced
by the optional pose stage later.
"""
from __future__ import annotations

from typing import Optional, Tuple

import cv2
import numpy as np


def ground_point_from_mask(mask: np.ndarray) -> Optional[Tuple[float, float]]:
    """Robust ground-contact point: median x over the bottom ~5% of mask rows.

    Far better than bbox-bottom under partial occlusion (legs behind a panel).
    """
    ys, xs = np.where(mask)
    if ys.size == 0:
        return None
    y_max = int(ys.max())
    y_min = int(ys.min())
    band = y_max - max(1, int(0.05 * (y_max - y_min)))
    sel = ys >= band
    return float(np.median(xs[sel])), float(y_max)


def ground_point_from_bbox(bbox: Tuple[float, float, float, float]) -> Tuple[float, float]:
    x1, _, x2, y2 = bbox
    return (x1 + x2) / 2.0, float(y2)


# --------------------------------------------------------------- the stored outline
# The mask reduced to something a database can hold and an annotator can drag
# (M4 §2.2). It shares its middle three lines with finetune/dataset.py's
# `_mask_to_polygon` — RETR_EXTERNAL, largest contour by area — but NOT its
# contract, and the two must not be merged: that one takes a BBOX-LOCAL FiftyOne
# mask and resizes it to the box, and returns a FLAT NORMALISED list for the YOLO
# text format. Ours is already frame-aligned (resizing it would shrink it by the
# frame/box ratio), returns [[x, y], …] PIXEL pairs to match mask_edits.polygon,
# simplifies, and reports the part count.
_MIN_MASK_PX = 20        # a speck is not an animal (mirrors finetune/dataset.py)
_MIN_POLY_PTS = 3        # below this it is not a shape (mirrors labels_db.MASK_MIN_POINTS)
# == labels_db.MASK_MAX_POINTS. Restated rather than imported: cownting.detect must
# not depend on the label store. It matters because a served polygon can be echoed
# straight back into /api/label/mask-fix, which rejects anything above the cap.
_MAX_POLY_PTS = 400
# ~300 raw contour points per cow -> ~30. Measured, not guessed: at eps=0 the same
# corpus costs 26x the storage and DuckDB's string compression stops paying for
# itself, so this is what makes the column ~40 MB per 200k detections instead of
# ~1 GB.
SIMPLIFY_EPS_PX = 2.0


def mask_to_polygon(
    mask: np.ndarray,
    bbox: Optional[Tuple[float, float, float, float]] = None,
    *,
    expect_shape: Optional[Tuple[int, int]] = None,
    eps_px: float = SIMPLIFY_EPS_PX,
    max_points: int = _MAX_POLY_PTS,
) -> Optional[Tuple[list, int]]:
    """Frame-aligned bool mask -> `(largest external contour in FULL-FRAME px, n_parts)`.

    `bbox` is a PERFORMANCE argument and never a semantic one — the coordinates
    come back in full-frame space either way. It matters more than it looks:
    findContours over a 4K frame costs ~12 ms per instance (half of it the
    bool->uint8 cast), so twenty cows would add ~250 ms to a frame, the same order
    as the inference itself. Cropped to the instance's own box it is ~0.03 ms.
    Ultralytics' `process_mask_native` already zeroes the mask outside the box, so
    the crop cannot lose foreground, and it is padded by a pixel so the contour
    never has to run along the array edge.

    Returns None for a mask that is absent, degenerate or smaller than
    `_MIN_MASK_PX`. The caller then stores NULL, which every consumer already
    reads as "no stored outline".
    """
    if mask is None or getattr(mask, "ndim", 0) != 2:
        return None
    # THE COORDINATE-SPACE GUARD, and it is load-bearing. Full-frame alignment is
    # not something this repo asserts anywhere — it is a consequence of
    # `retina_masks=True` routing Ultralytics through process_mask_native; the
    # default path returns proto-resolution masks about a quarter of the frame.
    # Today a proto-resolution mask crashes loudly in render_overlay, which
    # boolean-indexes a frame-shaped array with it. A backfill renders no overlay,
    # so the same misconfiguration would SILENTLY write every polygon wrong by the
    # stride factor — outlines landing a quarter of the way up the frame, on the
    # wrong animal, with nothing to show anything had gone wrong.
    if expect_shape is not None and tuple(mask.shape[:2]) != tuple(expect_shape):
        raise ValueError(
            f"mask is {mask.shape[:2]} but the frame is {tuple(expect_shape)} — "
            "the segmenter is not returning full-resolution masks "
            "(check retina_masks=True); refusing to store a mis-scaled outline"
        )
    h, w = mask.shape
    ox = oy = 0
    sub = mask
    if bbox is not None:
        x1, y1, x2, y2 = (float(v) for v in bbox)
        ox = max(0, int(np.floor(min(x1, x2))) - 1)
        oy = max(0, int(np.floor(min(y1, y2))) - 1)
        ex = min(w, int(np.ceil(max(x1, x2))) + 1)
        ey = min(h, int(np.ceil(max(y1, y2))) + 1)
        if ex <= ox or ey <= oy:
            return None
        sub = mask[oy:ey, ox:ex]
    # A slice is not contiguous, and findContours needs a contiguous CV_8UC1.
    m = np.ascontiguousarray(sub, dtype=np.uint8)
    if int(m.sum()) < _MIN_MASK_PX:
        return None
    cnts, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    # RETR_EXTERNAL retrieves only OUTERMOST contours, so a mask with a hole is one
    # part while a cow split by an occluding panel is two — which is exactly the
    # "fragmented outline" signal a reviewer needs. Free: same call, no second pass.
    parts = len(cnts)
    cnt = max(cnts, key=cv2.contourArea)

    # Escalate rather than reject. A ragged mask that simplifies to more than the
    # cap is unstorable downstream, and refusing it would throw away a real animal
    # over a rendering detail; doubling eps converges in a couple of rounds.
    eps = max(float(eps_px), 0.0)
    approx = cv2.approxPolyDP(cnt, eps, True) if eps > 0 else cnt
    # max_points <= 0 means "no cap" — the training-label caller wants the raw
    # contour, where fidelity is worth the bytes because it is written once and
    # read by a trainer, not dragged by hand on every queue fetch.
    if max_points > 0:
        for _ in range(8):
            if len(approx) <= max_points:
                break
            eps = eps * 2 if eps > 0 else 1.0
            approx = cv2.approxPolyDP(cnt, eps, True)

    pts = np.asarray(approx).reshape(-1, 2)
    # The floor is checked AFTER simplification, not before: approxPolyDP can
    # collapse a 4-point sliver to one or two points, and a 1-point "polygon"
    # renders as nothing and is rejected by the submit route on echo-back. The
    # export helper's pre-simplification check would not catch it.
    if len(pts) < _MIN_POLY_PTS:
        return None
    # cv2 contour points are already (x, y) — image convention, not numpy (row,
    # col) — so only the crop origin has to be added back. Reach for np.argwhere,
    # skimage.find_contours or rasterio.shapes instead and they return (row, col),
    # which on a near-square animal still looks like a cow and is silently
    # transposed. findContours, and nothing else.
    #
    # The +0.5 converts a pixel INDEX to a pixel CENTRE. bbox_* are continuous
    # edge coordinates — pixel i spans [i, i+1) — so raw indices put the outline
    # half a pixel up-left of the mask and read as 1.5 px inside the ring on the
    # right and bottom. It is the same index-versus-edge slip this module already
    # carries between ground_point_from_mask (a row index) and
    # ground_point_from_bbox (an edge), and it is visible at the zoom the outline
    # editor works at.
    poly = [[float(x) + ox + 0.5, float(y) + oy + 0.5] for x, y in pts]
    return poly, int(parts)


def _elongation_from_mask(mask: np.ndarray) -> Optional[float]:
    cnts, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not cnts:
        return None
    c = max(cnts, key=cv2.contourArea)
    if cv2.contourArea(c) < 1:
        return None
    (_, _), (w, h), _ = cv2.minAreaRect(c)
    if min(w, h) < 1e-6:
        return None
    return max(w, h) / min(w, h)


def posture_from_mask(mask: np.ndarray, lying_elongation: float) -> Optional[str]:
    e = _elongation_from_mask(mask)
    if e is None:
        return None
    return "lying" if e >= lying_elongation else "standing"


def posture_from_bbox(bbox: Tuple[float, float, float, float], lying_elongation: float) -> str:
    x1, y1, x2, y2 = bbox
    w, h = abs(x2 - x1), abs(y2 - y1)
    if min(w, h) < 1e-6:
        return "standing"
    return "lying" if (max(w, h) / min(w, h)) >= lying_elongation else "standing"


# AP-10K 17-keypoint schema (the ViTPose animal expert's output order).
AP10K_KPT = {
    "l_eye": 0, "r_eye": 1, "nose": 2, "neck": 3, "tail": 4,
    "l_shoulder": 5, "l_elbow": 6, "l_f_paw": 7,
    "r_shoulder": 8, "r_elbow": 9, "r_f_paw": 10,
    "l_hip": 11, "l_knee": 12, "l_b_paw": 13,
    "r_hip": 14, "r_knee": 15, "r_b_paw": 16,
}
AP10K_HEAD = (AP10K_KPT["nose"], AP10K_KPT["l_eye"], AP10K_KPT["r_eye"])
AP10K_HOOVES = (AP10K_KPT["l_f_paw"], AP10K_KPT["r_f_paw"],
                AP10K_KPT["l_b_paw"], AP10K_KPT["r_b_paw"])
# The RIGID dorsal spine: shoulders, hips, tail-root. Deliberately excludes the
# neck/nose — those drop when a cow grazes (head down), which would sink the
# reference line and misread a standing grazer as lying. The withers-to-tail
# spine stays level regardless of head position.
AP10K_TOPLINE = (AP10K_KPT["tail"], AP10K_KPT["l_shoulder"],
                 AP10K_KPT["r_shoulder"], AP10K_KPT["l_hip"], AP10K_KPT["r_hip"])
# Skeleton edges for the overlay (index pairs).
AP10K_SKELETON = (
    (2, 3), (3, 4),                                   # nose-neck-tail (topline)
    (3, 5), (5, 6), (6, 7), (3, 8), (8, 9), (9, 10),  # front legs
    (4, 11), (11, 12), (12, 13), (4, 14), (14, 15), (15, 16),  # hind legs
)


def posture_from_pose(
    keypoints: np.ndarray,
    bbox: Tuple[float, float, float, float],
    min_kpt_conf: float = 0.35,
    min_confident_kpts: int = 10,
    min_legs_visible: int = 3,
    min_bbox_h_px: float = 200.0,
    max_oob_frac: float = 0.35,
    graze_head_drop: float = 0.15,
    stand_lie_ratio: float = 0.0,
) -> str:
    """Classify a cow's posture from AP-10K keypoints: one of
    'standing' | 'lying' | 'grazing' | 'unknown'.

    All signals are vertical drops from the rigid dorsal spine, normalized by
    bbox height (viewpoint-robust, calibration-free):

    - **unknown**: the pose is garbage — too few confident joints, or the spine /
      legs we classify from are missing. We say so rather than guess.
    - **grazing**: the head is dropped toward the feet,
      (head_y - spine_y) / bbox_h >= graze_head_drop. Checked before standing/lying
      because a grazing cow is upright (legs extended) but head-down.
    - **standing** vs **lying**: leg drop (hoof_y - spine_y) / bbox_h, split at
      stand_lie_ratio. Standing extends the legs well below the spine; lying tucks
      them up toward it.
    """
    if keypoints is None or len(keypoints) < 17:
        return "unknown"
    x1, y1, x2, y2 = bbox
    bbox_h = abs(y2 - y1)
    bbox_w = abs(x2 - x1)
    if bbox_h < max(1e-6, min_bbox_h_px):     # too small / distant -> pose unreliable
        return "unknown"

    conf = keypoints[:, 2]
    confident = conf >= min_kpt_conf
    if int(confident.sum()) < min_confident_kpts:
        return "unknown"

    # Confident joints landing outside the animal's own (padded) box mean the pose
    # latched onto background / a neighbour -> garbage. Catches splayed skeletons
    # that ViTPose still reports at moderate confidence.
    mx, my = 0.25 * bbox_w, 0.25 * bbox_h
    kx, ky = keypoints[:, 0], keypoints[:, 1]
    inside = (kx >= x1 - mx) & (kx <= x2 + mx) & (ky >= y1 - my) & (ky <= y2 + my)
    n_conf = int(confident.sum())
    if n_conf and (1.0 - inside[confident].mean()) > max_oob_frac:
        return "unknown"

    spine = [i for i in AP10K_TOPLINE if confident[i]]
    legs = [i for i in AP10K_HOOVES if confident[i]]
    if not spine or len(legs) < min_legs_visible:
        return "unknown"

    spine_y = float(np.median(keypoints[spine, 1]))
    hoof_y = float(np.median(keypoints[legs, 1]))

    # Anatomical plausibility: a real cow's hooves sit at or below its back line
    # (image y grows downward). Hooves resolved ABOVE the spine mean the skeleton
    # is scrambled (legs latched onto the head/back) -> unknown, not a wrong call.
    if hoof_y < spine_y - 0.05 * bbox_h:
        return "unknown"

    head = [i for i in AP10K_HEAD if conf[i] >= min_kpt_conf]
    if head:
        head_y = float(np.median(keypoints[head, 1]))
        if (head_y - spine_y) / bbox_h >= graze_head_drop:
            return "grazing"

    return "standing" if (hoof_y - spine_y) / bbox_h >= stand_lie_ratio else "lying"
