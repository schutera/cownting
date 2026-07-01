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


class QualityCfg(BaseModel):
    enabled: bool = True
    skip_blind_in_segment: bool = False      # default: still RUN detection on hazy frames
    dark_brightness: float = 40.0            # mean V below -> 'dark' (night)
    blind_lap_var: float = 10.0              # Laplacian variance below -> lens too occluded to trust a 0
    roi: List[float] = Field(default_factory=lambda: [0.15, 0.35, 0.9, 0.7])  # x1,y1,x2,y2 fractions


class LabelCfg(BaseModel):
    """Stage 1b: bootstrap-label selection + CVAT annotation round-trip."""
    workspace: str = "data/labeling"         # selections + export staging live here
    n_select: int = 150                      # frames to hand-correct
    exclude_blind: bool = True               # drop frames the quality gate tagged blind/dark
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


class ShadeCfg(BaseModel):
    enabled: bool = False


class FlagsCfg(BaseModel):
    within_camera_tracking: bool = False
    global_reid: bool = False
    pose_enabled: bool = False


class PathsCfg(BaseModel):
    artifacts_dir: str = "data/artifacts"
    db_path: str = "data/cownting.duckdb"
    calibration: str = "data/calibration.json"
    orthophoto: Optional[str] = None


class Config(BaseModel):
    project: str = "cownting"
    cameras: List[CameraCfg]
    ingest: IngestCfg = Field(default_factory=IngestCfg)
    detect: DetectCfg = Field(default_factory=DetectCfg)
    posture: PostureCfg = Field(default_factory=PostureCfg)
    quality: QualityCfg = Field(default_factory=QualityCfg)
    label: LabelCfg = Field(default_factory=LabelCfg)
    finetune: FinetuneCfg = Field(default_factory=FinetuneCfg)
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
