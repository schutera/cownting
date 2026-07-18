"""Typed configuration loaded from YAML (pydantic v2)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field


class CameraCfg(BaseModel):
    id: str
    video: str                      # path to a prerecorded video file
    start: Optional[str] = None     # ISO8601 timestamp of frame 0; None -> file mtime


class DatasetCfg(BaseModel):
    """Identifies the data-package (a day's multi-camera shoot) an ingest produces.

    All optional: leaving it blank derives the id/day/label from the earliest
    camera `start`. Override `id` for a same-day re-shoot that must not replace
    the first one; a matching `id` on re-ingest replaces (idempotent)."""
    id: Optional[str] = None        # surrogate key; default = the capture day slug 'YYYY-MM-DD'
    day: Optional[str] = None       # ISO date 'YYYY-MM-DD'; default = earliest cam.start's date
    label: Optional[str] = None     # human label; default = a friendly 'Mon DD, YYYY'


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


class PoseCfg(BaseModel):
    """Stage-1c pose: real keypoints supersede the mask-elongation posture proxy.

    Zero-shot ViTPose++ (AP-10K animal expert, Apache-2.0) run on YOLO-seg
    instances. Gated by `flags.pose_enabled`; when off, nothing here is touched
    and posture stays the elongation proxy. See docs/roadmap/M1_pose_posture.md."""
    backend: Literal["vitpose"] = "vitpose"
    checkpoint: str = "usyd-community/vitpose-plus-base"  # HF ViTPose++ MoE
    dataset_index: int = 3            # MoE expert head: 3 = AP-10K (17 animal kpts)
    device: str = "auto"
    use_mask: bool = True            # zero non-instance pixels in each crop before pose
    crop_pad: float = 0.15           # bbox padding fraction before cropping
    # --- quality gate -> 'unknown' when the pose is garbage ---
    # ViTPose is confidently wrong on tiny/oblique cows, so confidence alone can't
    # flag bad poses; these geometric gates do. Tune per-camera.
    # Deliberately STRICT: only large, clean, anatomically-plausible poses get a
    # call; everything else is 'unknown'. High precision on what we do label, and
    # a visibly high unknown rate that flags the need for a fine-tuned pose model.
    min_kpt_conf: float = 0.35       # a keypoint counts only above this confidence
    min_confident_kpts: int = 10     # need this many confident joints overall, else 'unknown'
    min_legs_visible: int = 3        # need this many confident hooves (of 4), else 'unknown'
    min_bbox_h_px: float = 200.0     # cows smaller than this -> pose unreliable -> 'unknown'
    max_oob_frac: float = 0.35       # >this share of confident joints outside the (padded) box -> 'unknown'
    # --- class boundaries (normalized by bbox height) ---
    # Calibrated 2026-07-17 on 20 hand-verified gated cows (see the roadmap note).
    # grazing: head dropped toward the feet, (head_y - spine_y)/bbox_h >= this.
    # Clean grazers cluster at +0.18..+0.30; head-up cows are <= 0. 0.15 splits them.
    graze_head_drop: float = 0.15
    # standing vs lying: leg drop (hoof_y - spine_y)/bbox_h >= this -> standing.
    # NOTE: leg_ratio is viewpoint-dominated (frontal/rear standing cows read as
    # low as 0.03, broadside up to 0.52), and the validation footage has NO lying
    # cows, so the lying boundary is uncalibrated. Set to 0.0 so upright cows read
    # 'standing' rather than false-'lying'; reliable lying detection is DEFERRED
    # until lying-labelled data + a fine-tune exist (a real lying cow currently
    # reads 'standing' or 'unknown' — the known gap fine-tuning closes).
    stand_lie_ratio: float = 0.0


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


class FlagsCfg(BaseModel):
    within_camera_tracking: bool = False
    global_reid: bool = False
    pose_enabled: bool = False


class AuthCfg(BaseModel):
    """Login gate for the served dashboard. Only `enabled` lives here — the
    session secret and the bootstrap admin credentials are read from the
    environment (COWNTING_SECRET, COWNTING_ADMIN_USER/PASSWORD) so no secret is
    ever committed to the YAML. Disable only for tests / trusted-LAN demos."""
    enabled: bool = True
    session_max_age: int = 60 * 60 * 12   # cookie lifetime in seconds (12h)
    https_only: bool = False              # set True when served behind TLS


class PathsCfg(BaseModel):
    artifacts_dir: str = "data/artifacts"
    db_path: str = "data/cownting.duckdb"
    archive_db_path: str = "data/cownting_archive.duckdb"  # deleted days move here (not destroyed)
    count_areas: str = "data/count_areas.json"   # named counting regions (camera + ortho polygons)
    panel_areas: str = "data/panel_areas.json"   # shelter regions: a cow inside one = under a panel
    orthophoto: Optional[str] = None


class Config(BaseModel):
    project: str = "cownting"
    cameras: List[CameraCfg]
    dataset: DatasetCfg = Field(default_factory=DatasetCfg)
    ingest: IngestCfg = Field(default_factory=IngestCfg)
    detect: DetectCfg = Field(default_factory=DetectCfg)
    posture: PostureCfg = Field(default_factory=PostureCfg)
    pose: PoseCfg = Field(default_factory=PoseCfg)
    label: LabelCfg = Field(default_factory=LabelCfg)
    finetune: FinetuneCfg = Field(default_factory=FinetuneCfg)
    flags: FlagsCfg = Field(default_factory=FlagsCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        with open(path) as f:
            data = yaml.safe_load(f)
        return cls(**data)


def resolve_dataset(config: "Config") -> Tuple[str, Optional[date], str]:
    """Resolve (dataset_id, day, label) for an ingest run from the config.

    Precedence: explicit `dataset.*` overrides, else derive from the earliest
    camera `start`. `dataset_id` defaults to the ISO day slug so re-ingesting the
    same day replaces rather than duplicates."""
    ds = config.dataset
    day: Optional[date] = None
    if ds.day:
        day = date.fromisoformat(ds.day)
    else:
        starts = [c.start for c in config.cameras if c.start]
        if starts:
            day = min(datetime.fromisoformat(s) for s in starts).date()
    if ds.id:
        dataset_id = ds.id
    elif day is not None:
        dataset_id = day.isoformat()
    else:
        dataset_id = "dataset"
    label = ds.label or (day.strftime("%b %d, %Y") if day else dataset_id)
    return dataset_id, day, label


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
