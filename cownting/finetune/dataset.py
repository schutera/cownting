"""Pull corrected masks from CVAT and materialize a YOLO11-seg dataset.

Reads the corrections back through FiftyOne, converts each instance mask to a
normalized polygon (YOLO-seg label format), and writes images/ + labels/ +
data.yaml. The train/val split is by contiguous time-block, not random, so
near-duplicate adjacent minutes never straddle the split (which would inflate
val metrics).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import cv2
import numpy as np

from ..config import Config

_MIN_POLY_PTS = 3          # a polygon needs >= 3 vertices
_MIN_MASK_PX = 20          # ignore speck masks


def _mask_to_polygon(mask: np.ndarray, box_px, img_w: int, img_h: int) -> list[float] | None:
    """bbox-cropped bool mask -> normalized [x1,y1,x2,y2,...] over the full frame."""
    bx, by, bw, bh = box_px
    if bw < 1 or bh < 1 or mask is None or mask.sum() < _MIN_MASK_PX:
        return None
    m = cv2.resize(mask.astype(np.uint8), (max(1, round(bw)), max(1, round(bh))), interpolation=cv2.INTER_NEAREST)
    contours, _ = cv2.findContours(m, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    cnt = max(contours, key=cv2.contourArea).squeeze(1)  # (K, 2)
    if cnt.ndim != 2 or len(cnt) < _MIN_POLY_PTS:
        return None
    poly = []
    for x, y in cnt:
        poly.append(float((x + bx) / img_w))
        poly.append(float((y + by) / img_h))
    return poly


def _load_corrected_dataset(config: Config):
    import fiftyone as fo

    name = f"{config.project}_stage1b"
    if not fo.dataset_exists(name):
        raise RuntimeError(
            f"FiftyOne dataset '{name}' not found — run `cownting label-export` first"
        )
    ds = fo.load_dataset(name)
    try:
        ds.load_annotations(config.label.anno_key)
        print(f"[dataset-build] pulled corrections for '{config.label.anno_key}'")
    except Exception as e:  # already loaded, or annotations imported another way
        print(f"[dataset-build] load_annotations skipped ({e}); using labels on the dataset")
    ds.compute_metadata()
    return ds


def build_dataset(config: Config) -> Path:
    ds = _load_corrected_dataset(config)
    cfg = config.finetune
    field = config.label.label_field

    root = Path(cfg.dataset_dir)
    for sub in ("images/train", "images/val", "labels/train", "labels/val"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    samples = sorted(ds, key=lambda s: s.filepath)   # time order (zero-padded frame idx)
    n = len(samples)
    block = max(1, n // 20)
    stride = max(2, round(1.0 / cfg.val_fraction)) if cfg.split_by_time_block else 2

    n_train = n_val = n_inst = 0
    for i, s in enumerate(samples):
        split = "val" if (cfg.split_by_time_block and (i // block) % stride == 0) else "train"
        w = s.metadata.width
        h = s.metadata.height
        stem = f"{Path(s.filepath).parent.name}__{Path(s.filepath).stem}"

        lines = []
        dets = getattr(s[field], "detections", []) if s[field] is not None else []
        for d in dets:
            bx, by, bw, bh = d.bounding_box
            poly = _mask_to_polygon(
                np.asarray(d.mask) if d.mask is not None else None,
                (bx * w, by * h, bw * w, bh * h), w, h,
            )
            if poly is None:
                continue
            lines.append("0 " + " ".join(f"{v:.6f}" for v in poly))
            n_inst += 1

        shutil.copy(s.filepath, root / "images" / split / f"{stem}.jpg")
        (root / "labels" / split / f"{stem}.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
        n_train += split == "train"
        n_val += split == "val"

    data_yaml = root / "data.yaml"
    data_yaml.write_text(
        f"path: {root.resolve()}\n"
        "train: images/train\n"
        "val: images/val\n"
        "names:\n  0: cow\n"
    )
    print(f"[dataset-build] {n_train} train / {n_val} val frames, {n_inst} instances -> {root}")
    print(f"[dataset-build] wrote {data_yaml}")
    return data_yaml
