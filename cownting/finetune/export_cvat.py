"""Bootstrap masks with Grounded-SAM2, then push to CVAT for correction.

FiftyOne is the bridge: we build a dataset from the selected frames, attach the
open-vocab model's masks as pre-annotations, and `annotate(...)` uploads them to
a CVAT task. A human then adds the distant/shade cows the model missed (the
recall fix), deletes condensation false-positives, and tightens masks. Pull the
result back with `cownting dataset-build`.

Needs the `label` extra + a running CVAT server (see docs/SETUP_WINDOWS.md).
"""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from ..config import Config
from ..detect import build_segmenter
from .select import selected_paths


def _fo_dataset_name(config: Config) -> str:
    return f"{config.project}_stage1b"


def _to_fo_detection(fo, inst, w: int, h: int):
    """Instance -> FiftyOne Detection with a bbox-cropped instance mask."""
    x1, y1, x2, y2 = inst.bbox
    x1i, y1i = max(0, int(round(x1))), max(0, int(round(y1)))
    x2i, y2i = min(w, int(round(x2))), min(h, int(round(y2)))
    if x2i <= x1i or y2i <= y1i:
        return None
    rel_box = [x1i / w, y1i / h, (x2i - x1i) / w, (y2i - y1i) / h]
    mask = None
    if inst.mask is not None:
        mask = np.ascontiguousarray(inst.mask[y1i:y2i, x1i:x2i].astype(bool))
    return fo.Detection(label="cow", bounding_box=rel_box, mask=mask)


def export_to_cvat(config: Config, launch: bool = True) -> str:
    """Build the FiftyOne dataset, seed masks, and push a CVAT annotation task."""
    import fiftyone as fo

    cfg = config.label
    paths = selected_paths(config)

    # Grounded-SAM2 (or the configured bootstrap backend) seeds the masks.
    detect_cfg = config.detect.model_copy(update={"backend": cfg.bootstrap_backend})
    segmenter = build_segmenter(detect_cfg, config.posture)

    name = _fo_dataset_name(config)
    if fo.dataset_exists(name):
        fo.delete_dataset(name)
    ds = fo.Dataset(name=name, persistent=True)

    samples = []
    for p in paths:
        abspath = str(Path(p).resolve())
        img = cv2.imread(abspath)
        if img is None:
            print(f"[label-export] unreadable, skipping: {p}")
            continue
        h, w = img.shape[:2]
        instances = segmenter.segment(img)
        dets = [d for d in (_to_fo_detection(fo, i, w, h) for i in instances) if d is not None]
        s = fo.Sample(filepath=abspath)
        s[cfg.label_field] = fo.Detections(detections=dets)
        samples.append(s)
    ds.add_samples(samples)
    n_masks = sum(len(s[cfg.label_field].detections) for s in ds)
    print(f"[label-export] {len(samples)} frames, {n_masks} bootstrap masks -> CVAT")

    ds.annotate(
        cfg.anno_key,
        backend="cvat",
        url=cfg.cvat_url,
        label_field=cfg.label_field,
        label_type="instances",   # editable instance masks
        classes=["cow"],
        launch_editor=launch,
    )
    print(f"[label-export] pushed task '{cfg.anno_key}' to {cfg.cvat_url}")
    print("[label-export] correct the masks in CVAT, then run `cownting dataset-build`")
    return name
