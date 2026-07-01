"""Render an instance-segmentation overlay image for the dashboard seg view."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .base import Instance

_PALETTE = [
    (66, 135, 245), (245, 197, 66), (66, 245, 152), (245, 66, 167),
    (155, 66, 245), (245, 111, 66), (66, 245, 236), (160, 245, 66),
]


def render_overlay(image_bgr: np.ndarray, instances: list[Instance], out_path: str) -> str:
    canvas = image_bgr.copy()
    for i, inst in enumerate(instances):
        color = _PALETTE[i % len(_PALETTE)]
        if inst.mask is not None:
            colored = np.zeros_like(canvas)
            colored[inst.mask] = color
            canvas = cv2.addWeighted(canvas, 1.0, colored, 0.45, 0)
            cnts, _ = cv2.findContours(inst.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(canvas, cnts, -1, color, 2)
        # masks only: bounding boxes, score/posture labels intentionally omitted
        gx, gy = int(inst.ground_px[0]), int(inst.ground_px[1])
        cv2.circle(canvas, (gx, gy), 4, (0, 0, 255), -1)  # ground-contact point
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(out_path, canvas)
    return out_path
