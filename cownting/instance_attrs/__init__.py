"""M5 — the instance attribute model (docs/roadmap/M5_instance_attrs.md).

One small classifier over the instance CROP, trained on what M3's annotators
recorded, answering three questions the pipeline cannot answer today:

    is this a cow?   -> gate away the segmenter's false positives
    sun exposure     -> fills `in_shade`, which is 0-of-46,486 populated
    behaviour        -> beyond `posture`, which mask elongation can only ever
                        resolve into standing/lying

The heads are DERIVED, not hardcoded: every single-select taxonomy group becomes
a head whose classes are that group's active, non-escape options. A poweruser
adding a class therefore changes the next model's shape without a code edit, and
`is_escape` ("the body is visible but the answer will not resolve") stays out of
the label set — annotator uncertainty is the one thing a model must not be
taught to reproduce.

Heavy deps (torch, torchvision) are imported lazily inside the functions that
need them, matching `finetune/`: the CLI, the API and the config must load on a
box where the model is never run.

Module layout mirrors the pipeline order:
    dataset.py  label store -> crops + manifest      (`cownting attrs build-dataset`)
    model.py    the multi-head net and its masked loss
    train.py    the training loop and the val report (`cownting attrs train`)
    infer.py    score `detections` and write the verdict (`cownting attrs score`)
"""
from __future__ import annotations

from .dataset import build_dataset, block_of, split_blocks, load_manifest

__all__ = ["build_dataset", "block_of", "split_blocks", "load_manifest"]
