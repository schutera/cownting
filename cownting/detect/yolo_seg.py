"""YOLO11-seg backend (Ultralytics). Default; runs on MPS/CUDA/CPU.

Effectively zero-shot for "find cows": pretrained on COCO, no training on your
data. This is also the model you would later fine-tune (Stage 1b).
"""
from __future__ import annotations

import numpy as np

from ..config import DetectCfg, PostureCfg
from .base import Instance
from .geometry import (
    ground_point_from_bbox,
    ground_point_from_mask,
    posture_from_bbox,
    posture_from_mask,
)


class YoloSegSegmenter:
    def __init__(self, detect_cfg: DetectCfg, posture_cfg: PostureCfg, device: str):
        from ultralytics import YOLO

        self.model = YOLO(detect_cfg.yolo_weights)
        self.device = device
        self.conf = detect_cfg.conf
        self.imgsz = detect_cfg.imgsz
        self.posture_cfg = posture_cfg
        # Resolve requested class names to COCO class ids present in this model.
        wanted = set(detect_cfg.class_names)
        self.class_ids = [i for i, n in self.model.names.items() if n in wanted]
        if not self.class_ids:
            raise ValueError(
                f"None of {detect_cfg.class_names} are classes of {detect_cfg.yolo_weights}. "
                f"Available include: cow, sheep, horse, ..."
            )

    def segment(self, image_bgr: np.ndarray) -> list[Instance]:
        res = self.model.predict(
            image_bgr,                 # numpy arrays are treated as BGR by Ultralytics
            classes=self.class_ids,
            conf=self.conf,
            imgsz=self.imgsz,
            device=self.device,
            retina_masks=True,         # full-resolution masks aligned to the frame
            verbose=False,
        )
        r = res[0]
        out: list[Instance] = []
        if r.boxes is None or len(r.boxes) == 0:
            return out

        boxes = r.boxes.xyxy.cpu().numpy()
        scores = r.boxes.conf.cpu().numpy()
        masks = None
        if r.masks is not None:
            masks = r.masks.data.cpu().numpy().astype(bool)  # (N, H, W)

        for i in range(len(boxes)):
            bbox = tuple(float(v) for v in boxes[i])
            m = masks[i] if masks is not None and i < len(masks) else None
            if m is not None:
                gp = ground_point_from_mask(m) or ground_point_from_bbox(bbox)
                posture = posture_from_mask(m, self.posture_cfg.lying_elongation) if self.posture_cfg.enabled else None
                area = float(m.sum())
            else:
                gp = ground_point_from_bbox(bbox)
                posture = posture_from_bbox(bbox, self.posture_cfg.lying_elongation) if self.posture_cfg.enabled else None
                area = abs(bbox[2] - bbox[0]) * abs(bbox[3] - bbox[1])
            out.append(
                Instance(bbox=bbox, score=float(scores[i]), area_px=area, ground_px=gp, posture=posture, mask=m)
            )
        return out
