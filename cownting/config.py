"""Typed configuration loaded from YAML (pydantic v2)."""
from __future__ import annotations

from pathlib import Path
from typing import List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


class CameraCfg(BaseModel):
    id: str
    video: str                      # path to a prerecorded video file
    start: Optional[str] = None     # ISO8601 timestamp of frame 0; None -> file mtime


class IngestCfg(BaseModel):
    target_fps: float = 1.0
    time_bin_seconds: float = 2.0
    save_frames: bool = True
    # Time-lapse support. When set, each *sampled* frame advances real capture
    # time by this many seconds (a Brinno grabs 1 frame/minute -> 60.0), so the
    # per-frame timestamp is start + frame_idx * frame_interval_seconds. When
    # None (default), timestamps use real-time playback: start + frame_idx / video_fps.
    frame_interval_seconds: Optional[float] = None


class DetectCfg(BaseModel):
    backend: Literal["yolo", "grounded_sam2"] = "yolo"
    device: str = "auto"
    conf: float = 0.25
    imgsz: int = 1280            # inference resolution; critical for small/distant cows
    # yolo
    yolo_weights: str = "yolo11x-seg.pt"
    class_names: List[str] = Field(default_factory=lambda: ["cow"])
    # grounded_sam2
    dino_model_id: str = "IDEA-Research/grounding-dino-tiny"
    sam2_cfg: str = "configs/sam2.1/sam2.1_hiera_s.yaml"
    sam2_checkpoint: str = "checkpoints/sam2.1_hiera_small.pt"
    prompt: str = "cow."
    box_threshold: float = 0.3
    text_threshold: float = 0.25


class PostureCfg(BaseModel):
    enabled: bool = True
    lying_elongation: float = 1.9


class LabelCfg(BaseModel):
    """Stage 1b: bootstrap-label selection + CVAT annotation round-trip."""
    workspace: str = "data/labeling"         # selections + export staging live here
    n_select: int = 150                      # frames to hand-correct
    seed: int = 0                            # deterministic stratified sample
    # Grounded-SAM2 is the bootstrap labeler (see docs/SETUP_WINDOWS.md for install).
    bootstrap_backend: Literal["grounded_sam2", "yolo"] = "grounded_sam2"
    # FiftyOne <-> CVAT round-trip
    cvat_url: str = "http://localhost:8080"
    anno_key: str = "cownting_stage1b"       # FiftyOne annotation-run key (push + pull)
    label_field: str = "cows"                # instance field annotators edit in CVAT


class FinetuneCfg(BaseModel):
    """Stage 1b: YOLO11-seg fine-tune on the corrected masks."""
    dataset_dir: str = "data/finetune/dataset"    # generated YOLO-seg dataset root
    runs_dir: str = "runs/finetune"               # ultralytics output root
    run_name: str = "cownting"
    base_weights: str = "yolo11m-seg.pt"          # start point (already on disk)
    weights_out: str = "data/finetune/cownting-seg.pt"  # promoted best.pt
    epochs: int = 100
    imgsz: int = 1280                             # match inference; critical for small cows
    batch: int = 4                                # small; safe on 12 GB VRAM / MPS at 1280
    freeze: int = 0                               # backbone layers to freeze (0 = full FT)
    patience: int = 20                            # early-stop patience
    val_fraction: float = 0.2
    split_by_time_block: bool = True              # avoid near-duplicate minutes leaking train->val


class CalibCfg(BaseModel):
    """3-stage warp calibration site defaults (per-camera values live in calibration.json)."""
    h_center: Optional[float] = None     # panel-center / torque-tube height (m); optional size cue


class ShadeCfg(BaseModel):
    enabled: bool = False
    margin_px: float = 0.0  # px a ground point must sit inside a footprint edge to count as
                            # sheltering; within it = boundary/uncertain (configurable)


class FlagsCfg(BaseModel):
    within_camera_tracking: bool = False
    global_reid: bool = False
    pose_enabled: bool = False


class PathsCfg(BaseModel):
    artifacts_dir: str = "data/artifacts"
    db_path: str = "data/cownting.duckdb"
    calibration: str = "data/calibration.json"
    fence: str = "data/fence.json"               # site-wide cow-enclosure polygon (ortho px)
    tiepoints: str = "data/tiepoints.json"       # cross-camera shared ground points (joint calib)
    panels: str = "data/panels.json"             # solar-panel ground footprints (ortho + per-camera px)
    orthophoto: Optional[str] = None


class Config(BaseModel):
    project: str = "cownting"
    cameras: List[CameraCfg]
    ingest: IngestCfg = Field(default_factory=IngestCfg)
    detect: DetectCfg = Field(default_factory=DetectCfg)
    posture: PostureCfg = Field(default_factory=PostureCfg)
    label: LabelCfg = Field(default_factory=LabelCfg)
    finetune: FinetuneCfg = Field(default_factory=FinetuneCfg)
    calib: CalibCfg = Field(default_factory=CalibCfg)
    shade: ShadeCfg = Field(default_factory=ShadeCfg)
    flags: FlagsCfg = Field(default_factory=FlagsCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)


def resolve_device(device: str) -> str:
    """Resolve 'auto' to the best available backend: cuda > mps > cpu."""
    if device != "auto":
        return device
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"
