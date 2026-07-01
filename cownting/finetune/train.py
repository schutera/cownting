"""Fine-tune YOLO11-seg on the corrected masks.

Device-agnostic: `detect.device: auto` resolves cuda > mps > cpu, so this runs
unchanged on the CUDA box and on this Mac. imgsz matches inference (1280) — the
whole point is small/distant cows, so don't train at 640.
"""
from __future__ import annotations

import shutil
from pathlib import Path

from ..config import Config, resolve_device


def train(config: Config) -> Path:
    from ultralytics import YOLO

    ft = config.finetune
    device = resolve_device(config.detect.device)
    data_yaml = Path(ft.dataset_dir) / "data.yaml"
    if not data_yaml.exists():
        raise RuntimeError(f"{data_yaml} not found — run `cownting dataset-build` first")

    print(f"[train] {ft.base_weights} @ imgsz={ft.imgsz} batch={ft.batch} device={device}")
    model = YOLO(ft.base_weights)
    model.train(
        data=str(data_yaml),
        epochs=ft.epochs,
        imgsz=ft.imgsz,
        batch=ft.batch,
        freeze=ft.freeze or None,
        patience=ft.patience,
        device=device,
        project=ft.runs_dir,
        name=ft.run_name,
        exist_ok=True,
    )

    best = Path(ft.runs_dir) / ft.run_name / "weights" / "best.pt"
    out = Path(ft.weights_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(best, out)
    print(f"[train] best weights -> {out}")
    print(f"[train] to deploy: set detect.yolo_weights: {out}  then re-run `segment` + `spotcheck`")
    return out
