"""Stage-1c pose estimation: zero-shot ViTPose++ (AP-10K) over YOLO-seg instances.

Top-down pose reuses the segmenter's boxes+masks: each instance is cropped, its
background zeroed with the seg mask (so a loose crop can't latch onto a neighbour
under occlusion), and all crops for a frame are batched through the ViTPose++ MoE
using its AP-10K animal expert head. Keypoints are written back onto each
`Instance` in full-image pixels, and a pose-derived posture supersedes the
elongation proxy when it is confident (else the proxy value is kept).

Gated by `flags.pose_enabled`; the heavy imports below are lazy so a pose-off run
never loads torch/transformers here beyond what detection already needs.
See docs/roadmap/M1_pose_posture.md.
"""
from __future__ import annotations

from typing import List, Protocol

import numpy as np

from ..config import PoseCfg, resolve_device
from .base import Instance
from .geometry import posture_from_pose


class PoseEstimator(Protocol):
    def estimate(self, image_bgr: np.ndarray, instances: List[Instance]) -> None:
        """Populate `inst.keypoints` (and refine `inst.posture`) in place."""
        ...


def build_pose_estimator(pose_cfg: PoseCfg) -> "PoseEstimator":
    device = resolve_device(pose_cfg.device)
    if pose_cfg.backend == "vitpose":
        return ViTPoseEstimator(pose_cfg, device)
    raise ValueError(f"unknown pose backend: {pose_cfg.backend}")


class ViTPoseEstimator:
    def __init__(self, cfg: PoseCfg, device: str):
        import torch
        from transformers import AutoProcessor, VitPoseForPoseEstimation

        self.cfg = cfg
        self.device = device
        self._torch = torch
        self.processor = AutoProcessor.from_pretrained(cfg.checkpoint)
        self.model = VitPoseForPoseEstimation.from_pretrained(cfg.checkpoint).to(device)
        self.model.eval()

    def _crop(self, image_bgr: np.ndarray, inst: Instance):
        """Padded bbox crop, background zeroed by the seg mask. Returns (rgb, x0, y0)."""
        H, W = image_bgr.shape[:2]
        x1, y1, x2, y2 = inst.bbox
        pw, ph = (x2 - x1) * self.cfg.crop_pad, (y2 - y1) * self.cfg.crop_pad
        x0, y0 = max(0, int(x1 - pw)), max(0, int(y1 - ph))
        x3, y3 = min(W, int(x2 + pw)), min(H, int(y2 + ph))
        if x3 - x0 < 2 or y3 - y0 < 2:
            return None, x0, y0
        crop = image_bgr[y0:y3, x0:x3].copy()
        if self.cfg.use_mask and inst.mask is not None:
            m = inst.mask[y0:y3, x0:x3]
            crop[~m] = 0
        rgb = crop[:, :, ::-1]  # BGR -> RGB
        return rgb, x0, y0

    def estimate(self, image_bgr: np.ndarray, instances: List[Instance]) -> None:
        from PIL import Image

        crops, origins, targets = [], [], []
        for inst in instances:
            rgb, x0, y0 = self._crop(image_bgr, inst)
            if rgb is None:
                continue
            crops.append(Image.fromarray(np.ascontiguousarray(rgb)))
            origins.append((x0, y0))
            targets.append(inst)
        if not crops:
            return

        # One box per image spanning the whole (already-cropped) image.
        boxes = [[[0.0, 0.0, float(im.width), float(im.height)]] for im in crops]
        inputs = self.processor(crops, boxes=boxes, return_tensors="pt").to(self.device)
        ds_index = self._torch.tensor([self.cfg.dataset_index] * len(crops)).to(self.device)
        with self._torch.no_grad():
            outputs = self.model(**inputs, dataset_index=ds_index)
        results = self.processor.post_process_pose_estimation(outputs, boxes=boxes)

        for inst, (x0, y0), res in zip(targets, origins, results):
            pose = res[0]
            kxy = pose["keypoints"].cpu().numpy()
            kconf = pose["scores"].cpu().numpy()
            kpts = np.concatenate([kxy, kconf[:, None]], axis=1)  # (K,3)
            kpts[:, 0] += x0
            kpts[:, 1] += y0
            inst.keypoints = kpts
            # Pose is the source of truth when enabled: this returns one of
            # standing/lying/grazing/unknown and overwrites the elongation proxy
            # (garbage poses become 'unknown' rather than a wrong guess).
            inst.posture = posture_from_pose(
                kpts, inst.bbox,
                min_kpt_conf=self.cfg.min_kpt_conf,
                min_confident_kpts=self.cfg.min_confident_kpts,
                min_legs_visible=self.cfg.min_legs_visible,
                min_bbox_h_px=self.cfg.min_bbox_h_px,
                max_oob_frac=self.cfg.max_oob_frac,
                graze_head_drop=self.cfg.graze_head_drop,
                stand_lie_ratio=self.cfg.stand_lie_ratio,
            )


__all__ = ["PoseEstimator", "ViTPoseEstimator", "build_pose_estimator"]
