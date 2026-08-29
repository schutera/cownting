"""Shared detection types."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Tuple

import numpy as np


@dataclass
class Instance:
    bbox: Tuple[float, float, float, float]   # x1, y1, x2, y2 (image px)
    score: float
    area_px: float
    ground_px: Tuple[float, float]            # ground-contact point (image px)
    posture: Optional[str] = None             # 'lying' | 'standing' | None
    # bool HxW, in FULL-FRAME px — `mask.shape[:2] == frame.shape[:2]` is a hard
    # postcondition of Segmenter.segment. Reduced to a polygon and PERSISTED as
    # detections.mask_poly (detect.geometry.mask_to_polygon); the array itself is
    # not stored.
    mask: Optional[np.ndarray] = None
    keypoints: Optional[np.ndarray] = None    # (K,3) x,y,conf in image px; pose stage only, not persisted


class Segmenter(Protocol):
    def segment(self, image_bgr: np.ndarray) -> list[Instance]:
        ...
