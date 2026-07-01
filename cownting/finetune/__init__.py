"""Stage 1b — fine-tune / re-label loop.

    label-select   pick a diverse frame subset to hand-correct
    label-export   bootstrap masks (Grounded-SAM2) -> push to CVAT via FiftyOne
    (correct masks in CVAT — the one manual step)
    dataset-build  pull corrections -> YOLO-seg dataset (images/labels/data.yaml)
    train          fine-tune YOLO11-seg on the corrected masks
    eval-detect    val mAP for the fine-tuned weights

Heavy deps (fiftyone, ultralytics, torch) are imported lazily inside each
function so the CLI and config load without them. See docs/SETUP_WINDOWS.md.
"""
from __future__ import annotations

from .select import select_frames, selected_paths
from .dataset import build_dataset
from .export_cvat import export_to_cvat
from .train import train
from .evaluate import evaluate

__all__ = [
    "select_frames",
    "selected_paths",
    "export_to_cvat",
    "build_dataset",
    "train",
    "evaluate",
]
