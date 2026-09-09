"""The multi-head crop classifier and its masked loss.

One ImageNet-pretrained backbone, one linear head per task, and a loss that
IGNORES heads a sample has no label for. That masking is the whole architecture
argument: the corpus is sparse across tasks by construction — a false positive
carries no class answers (the overlap in the store is exactly zero), and an
instance answered before the outline step existed carries no cow verdict. Three
separate single-task models would each see a fraction of 628 samples and share
nothing; one shared trunk sees every crop and lets "what cows look like" be
learned once.

Why MobileNetV3-Small. The serving box is CPU-only with 2 torch threads and
already spends ~4 s/frame on segmentation; a backbone heavier than this would
cost more to run than the thing it filters. It is also ~2.5M parameters against
628 training samples, and that ratio is already uncomfortable — the fine-tuning
recipe below (frozen early blocks, heavy augmentation, early stopping on macro-F1)
is chosen to survive it rather than to squeeze out accuracy.

torch and torchvision are imported lazily inside the functions, matching
`finetune/`: config, CLI and API must load on a box that never runs the model.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Sequence

# Bumped when the stored tensor layout or the preprocessing changes in a way
# that makes an older checkpoint wrong rather than merely worse. Written into
# every checkpoint and refused on load, because a silently mis-loaded model does
# not crash — it produces confident nonsense over 46,486 detections.
CHECKPOINT_VERSION = 1

# ImageNet statistics. Fixed by the pretrained weights, not a tuning knob: the
# backbone's first conv expects inputs in this distribution, and the inference
# path re-derives them from the checkpoint so training and serving cannot drift.
IMAGENET_MEAN = (0.485, 0.456, 0.406)
IMAGENET_STD = (0.229, 0.224, 0.225)


def build_model(heads: Mapping[str, Sequence[str]], *, pretrained: bool = True):
    """A shared MobileNetV3-Small trunk with one linear head per task.

    `heads` is head -> ordered class names, straight from the dataset manifest,
    so the checkpoint's output layout is defined by the taxonomy that produced
    the labels rather than by a constant in this file.

    `pretrained=False` exists for tests and for an offline box: it builds the
    identical architecture with random weights, so shape/serialisation can be
    exercised without a weights download.
    """
    import torch.nn as nn
    from torchvision.models import mobilenet_v3_small, MobileNet_V3_Small_Weights

    weights = MobileNet_V3_Small_Weights.IMAGENET1K_V1 if pretrained else None
    net = mobilenet_v3_small(weights=weights)
    # Replace the 1000-way ImageNet classifier with our own heads, but keep the
    # penultimate 1024-d layer: it is part of the pretrained feature extractor,
    # and dropping it throws away representation we cannot re-learn from 628
    # samples.
    in_features = net.classifier[3].in_features
    net.classifier = nn.Sequential(*list(net.classifier[:3]))

    # Defined here rather than at module scope because nn.Module cannot be
    # subclassed until torch is imported, and this module must stay importable
    # on a box that never runs the model (the config, the CLI and the API all
    # import it). Same lazy-import discipline as `finetune/`.
    class MultiHead(nn.Module):
        def __init__(self, trunk, feat_dim: int, heads: dict[str, list[str]]):
            super().__init__()
            self.trunk = trunk
            self.heads = heads
            # ModuleDict keys cannot contain '.'. Head names are GROUP keys
            # ('sun_exposure'), which are safe, while class keys are dotted —
            # asserted rather than trusted, because a head silently dropped by
            # ModuleDict trains nothing and reports nothing.
            assert all("." not in h for h in heads), heads
            self.classifiers = nn.ModuleDict(
                {h: nn.Linear(feat_dim, len(cs)) for h, cs in heads.items()})

        def forward(self, x):
            feat = self.trunk(x)
            return {h: c(feat) for h, c in self.classifiers.items()}

    return MultiHead(net, in_features, {h: list(cs) for h, cs in heads.items()})


def masked_loss(logits: Mapping[str, Any], targets: Mapping[str, Any],
                weights: Mapping[str, Any] | None = None):
    """Cross-entropy per head, skipping samples whose target for that head is -1.

    -1 is the "no label" sentinel and it is why this function exists. The
    alternative — dropping a sample that lacks any head — would discard the 76
    false positives from the class heads' batches *and* the 89 instances that
    have a class answer but no outline verdict from the gate's, which is a
    quarter of the corpus thrown away to avoid a two-line mask.

    A head with no labeled sample in a batch contributes exactly zero, not NaN:
    `cross_entropy` over an empty selection returns nan, so the count is checked
    first. With a corpus this small and heads this sparse, an all-unlabeled head
    in some batch is routine rather than exotic.
    """
    import torch
    import torch.nn.functional as F

    total = None
    parts: dict[str, float] = {}
    for head, logit in logits.items():
        t = targets[head]
        keep = t >= 0
        n = int(keep.sum())
        if n == 0:
            continue
        w = None if weights is None else weights.get(head)
        loss = F.cross_entropy(logit[keep], t[keep], weight=w)
        parts[head] = float(loss.detach())
        total = loss if total is None else total + loss
    if total is None:
        # Every head unlabeled across the whole batch. Return a real zero tensor
        # tied to the graph so `.backward()` is still legal.
        any_logit = next(iter(logits.values()))
        total = (any_logit * 0.0).sum()
    return total, parts


def class_weights(counts: Sequence[int], *, device=None):
    """Inverse-frequency weights, normalised to mean 1.

    The gate is 443 cow against 57 not-a-cow. Unweighted, predicting "cow"
    always scores 89% and learns nothing — and the minority class is the entire
    point of the head. Normalising to mean 1 keeps the loss magnitude comparable
    across heads so one head's imbalance does not dominate the shared trunk's
    gradient.
    """
    import torch

    c = torch.tensor([max(1, int(n)) for n in counts], dtype=torch.float32)
    w = c.sum() / (len(c) * c)
    w = w / w.mean()
    return w.to(device) if device is not None else w


def save_checkpoint(path: str | Path, *, model, heads: Mapping[str, Sequence[str]],
                    img_size: int, metrics: Mapping[str, Any],
                    manifest_config: Mapping[str, Any]) -> str:
    """Write weights plus everything needed to serve them, and return the rev.

    The checkpoint is self-describing on purpose. Inference must not be handed
    the head vocabulary out of band — a checkpoint trained before a poweruser
    added a class would then be read with the new class list and every
    prediction above the inserted index would shift by one, silently. The crop
    geometry travels too, so scoring cannot cut different pixels than training
    did.
    """
    import hashlib
    import io

    import torch

    heads_out = {h: list(cs) for h, cs in heads.items()}
    blob = {
        "version": CHECKPOINT_VERSION,
        "state_dict": model.state_dict(),
        "heads": heads_out,
        "img_size": int(img_size),
        "norm": {"mean": list(IMAGENET_MEAN), "std": list(IMAGENET_STD)},
        # The crop geometry this model's training pixels were cut with. Scoring
        # asserts against it.
        "crop": {k: manifest_config.get(k) for k in
                 ("crop_pad", "crop_max_width", "mask_timestamp_banner")},
        "metrics": dict(metrics),
    }
    buf = io.BytesIO()
    torch.save(blob, buf)
    data = buf.getvalue()
    # The rev is the hash of the bytes actually written, so `attr_model_rev` on a
    # scored row identifies the exact weights that produced it — including two
    # runs of the same config that landed on different minima.
    rev = hashlib.sha256(data).hexdigest()[:12]
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    (p.parent / f"{p.stem}.rev").write_text(rev, encoding="utf-8")
    return rev


def load_checkpoint(path: str | Path, *, device: str = "cpu") -> dict[str, Any]:
    """Rebuild a servable model from a checkpoint. Refuses a version it cannot read."""
    import hashlib

    import torch

    p = Path(path)
    data = p.read_bytes()
    blob = torch.load(p, map_location=device, weights_only=False)
    got = int(blob.get("version", 0))
    if got != CHECKPOINT_VERSION:
        raise ValueError(
            f"{p}: checkpoint version {got}, this build reads {CHECKPOINT_VERSION}. "
            "Re-train rather than loading it — the layout changed."
        )
    model = build_model(blob["heads"], pretrained=False)
    model.load_state_dict(blob["state_dict"])
    model.eval()
    blob["model"] = model
    blob["rev"] = hashlib.sha256(data).hexdigest()[:12]
    return blob
