"""Validation metrics for the fine-tuned weights.

Reports mask + box mAP on the held-out val split. This is the model-quality
gate; the *operational* gate is the count MAE/bias from `cownting spotcheck`
after re-segmenting with the new weights (see docs/SETUP_WINDOWS.md).
"""
from __future__ import annotations

from pathlib import Path

from ..config import Config, resolve_device


def evaluate(config: Config, weights: str | None = None) -> dict:
    from ultralytics import YOLO

    ft = config.finetune
    device = resolve_device(config.detect.device)
    data_yaml = Path(ft.dataset_dir) / "data.yaml"
    if not data_yaml.exists():
        raise RuntimeError(f"{data_yaml} not found — run `cownting dataset-build` first")

    w = weights or ft.weights_out
    if not Path(w).exists():
        raise RuntimeError(f"weights {w} not found — run `cownting train` first (or pass --weights)")

    print(f"[eval-detect] {w} on {data_yaml} (device={device})")
    metrics = YOLO(w).val(data=str(data_yaml), imgsz=ft.imgsz, device=device, verbose=False)

    seg = getattr(metrics, "seg", None)
    box = getattr(metrics, "box", None)
    out = {
        "mask_mAP50":    round(float(seg.map50), 4) if seg else None,
        "mask_mAP50-95": round(float(seg.map), 4) if seg else None,
        "box_mAP50":     round(float(box.map50), 4) if box else None,
        "box_mAP50-95":  round(float(box.map), 4) if box else None,
    }
    for k, v in out.items():
        print(f"[eval-detect] {k:>14}: {v}")
    print("[eval-detect] next: point detect.yolo_weights at the new weights, "
          "re-run `segment`, then `spotcheck manual.csv` for count MAE vs zero-shot")
    return out
