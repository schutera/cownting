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
