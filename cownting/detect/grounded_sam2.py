"""Grounding-DINO + SAM2 backend (open-vocabulary zero-shot).

Slower than YOLO on MPS but flexible (prompt-driven). Also the natural
bootstrap labeler: dump these masks, correct in CVAT, fine-tune YOLO.

Requires the optional deps:
    pip install transformers
    pip install "git+https://github.com/facebookresearch/sam2.git"
plus a SAM2 checkpoint + config (see detect_cfg.sam2_checkpoint / sam2_cfg).
"""
from __future__ import annotations

import cv2
import numpy as np

from ..config import DetectCfg, PostureCfg
from .base import Instance
from .geometry import ground_point_from_bbox, ground_point_from_mask, posture_from_mask


class GroundedSAM2Segmenter:
    def __init__(self, detect_cfg: DetectCfg, posture_cfg: PostureCfg, device: str):
        import torch
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor

        self.torch = torch
        self.device = device
        self.cfg = detect_cfg
        self.posture_cfg = posture_cfg

        self.processor = AutoProcessor.from_pretrained(detect_cfg.dino_model_id)
        self.dino = AutoModelForZeroShotObjectDetection.from_pretrained(detect_cfg.dino_model_id).to(device)

        from sam2.build_sam import build_sam2
        from sam2.sam2_image_predictor import SAM2ImagePredictor

        sam2_model = build_sam2(detect_cfg.sam2_cfg, detect_cfg.sam2_checkpoint, device=device)
        self.predictor = SAM2ImagePredictor(sam2_model)

    def segment(self, image_bgr: np.ndarray) -> list[Instance]:
        from PIL import Image

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)

        inputs = self.processor(images=pil, text=self.cfg.prompt, return_tensors="pt").to(self.device)
        with self.torch.no_grad():
            outputs = self.dino(**inputs)
        results = self.processor.post_process_grounded_object_detection(
            outputs,
            inputs.input_ids,
            box_threshold=self.cfg.box_threshold,
            text_threshold=self.cfg.text_threshold,
            target_sizes=[pil.size[::-1]],  # (h, w)
        )[0]

        boxes = results["boxes"].cpu().numpy()
        scores = results["scores"].cpu().numpy()
        if len(boxes) == 0:
            return []

        self.predictor.set_image(rgb)
        masks, _, _ = self.predictor.predict(box=boxes, multimask_output=False)
        masks = np.asarray(masks)
        if masks.ndim == 4:           # (N, 1, H, W) -> (N, H, W)
            masks = masks[:, 0]
        elif masks.ndim == 2:         # single box -> (H, W)
            masks = masks[None]
        masks = masks.astype(bool)

        out: list[Instance] = []
        for i in range(len(boxes)):
            bbox = tuple(float(v) for v in boxes[i])
            m = masks[i] if i < len(masks) else None
            if m is not None:
                gp = ground_point_from_mask(m) or ground_point_from_bbox(bbox)
                posture = posture_from_mask(m, self.posture_cfg.lying_elongation) if self.posture_cfg.enabled else None
                area = float(m.sum())
            else:
                gp = ground_point_from_bbox(bbox)
                posture = None
                area = abs(bbox[2] - bbox[0]) * abs(bbox[3] - bbox[1])
            out.append(
                Instance(bbox=bbox, score=float(scores[i]), area_px=area, ground_px=gp, posture=posture, mask=m)
            )
        return out
