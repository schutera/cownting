"""Detection backends behind one Segmenter interface."""
from __future__ import annotations

from ..config import DetectCfg, PostureCfg, resolve_device
from .base import Instance, Segmenter


def build_segmenter(detect_cfg: DetectCfg, posture_cfg: PostureCfg) -> Segmenter:
    device = resolve_device(detect_cfg.device)
    if detect_cfg.backend == "yolo":
        from .yolo_seg import YoloSegSegmenter

        return YoloSegSegmenter(detect_cfg, posture_cfg, device)
    if detect_cfg.backend == "grounded_sam2":
        from .grounded_sam2 import GroundedSAM2Segmenter

        return GroundedSAM2Segmenter(detect_cfg, posture_cfg, device)
    raise ValueError(f"unknown backend: {detect_cfg.backend}")


__all__ = ["Instance", "Segmenter", "build_segmenter"]
