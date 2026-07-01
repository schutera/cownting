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
