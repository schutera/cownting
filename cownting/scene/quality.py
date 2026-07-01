"""Rule-based frame-quality gate (no training required).

Lens condensation / fog has a strong no-label signature: low high-frequency
detail (Laplacian variance), washed-out colour (low saturation), low contrast.
Night frames are simply dark. Everything else is 'ok' — including EMPTY frames,
which are valid occupancy=0 measurements and must be kept.

Thresholds are scene/resolution dependent; tune per camera (see the printed
distribution from `cownting quality`). A learned classifier is only warranted
if these metrics prove inadequate on borderline haze.
"""
from __future__ import annotations

from typing import Sequence, Tuple

import cv2
import numpy as np

from ..config import QualityCfg


def _roi(image_bgr: np.ndarray, roi: Sequence[float]) -> np.ndarray:
    h, w = image_bgr.shape[:2]
    x1, y1, x2, y2 = roi
    return image_bgr[int(y1 * h): int(y2 * h), int(x1 * w): int(x2 * w)]


def quality_metrics(image_bgr: np.ndarray, roi: Sequence[float]) -> dict:
    crop = _roi(image_bgr, roi)
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    return {
        "lap_var": float(cv2.Laplacian(gray, cv2.CV_64F).var()),
        "saturation": float(hsv[..., 1].mean()),
        "brightness": float(hsv[..., 2].mean()),
        "contrast": float(gray.std()),
    }


def classify(metrics: dict, cfg: QualityCfg) -> str:
    # Only two exclusion tiers, both meaning "a 0-cow reading can't be trusted":
    #   dark  -> night / no signal
    #   blind -> lens so occluded (near-opaque condensation) cows would be invisible
    # Everything else is 'ok', INCLUDING mild haze where cows are still detectable.
    # A frame that yields a detection is kept regardless (handled in the analytics layer).
    if metrics["brightness"] < cfg.dark_brightness:
        return "dark"
    if metrics["lap_var"] < cfg.blind_lap_var:
        return "blind"
    return "ok"


def assess_quality(image_bgr: np.ndarray, cfg: QualityCfg) -> Tuple[str, dict]:
    m = quality_metrics(image_bgr, cfg.roi)
    return classify(m, cfg), m
