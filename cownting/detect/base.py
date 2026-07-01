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
    mask: Optional[np.ndarray] = None         # bool HxW, not persisted to the DB


class Segmenter(Protocol):
    def segment(self, image_bgr: np.ndarray) -> list[Instance]:
        ...
