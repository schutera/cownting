"""Fine-tune the multi-head crop classifier and report what it actually learned.

Reports MACRO-F1 per head, not accuracy. Accuracy is actively misleading on
every head here: the gate is ~88% cow, so "always cow" scores 88% while doing
precisely nothing, and the behaviour head's rarest class can be ignored entirely
at a cost of a few percent. Macro-F1 makes the rare classes count as much as the
common ones, which is the only reading under which this model is worth
deploying — and the same reason it is the early-stopping criterion.

Every val number in the report is over a block-disjoint split (see dataset.py),
so it is a claim about a camera-day the model has not seen rather than about
crops adjacent to ones it memorised.

The core loop is `fit`, which takes explicit train/val item lists. `train` is
the one-split entry point that saves a checkpoint; `cv.py` calls `fit` once per
fold and never saves anything. Both go through the same code, so a recipe that
wins under CV is exactly the recipe `train` then ships.
"""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any

from ..config import Config, InstanceAttrsCfg, resolve_device
from . import model as M
from .dataset import load_manifest

# The "no label for this head" sentinel. Matches the mask in `M.masked_loss`;
# torch's own ignore_index default is -100, but the mask here is explicit
# because a sample is legitimately unlabeled on most heads and that is the
# common case rather than an error path.
NO_LABEL = -1

# The crop renderer's neutral fill (labeling._FILL), so a rotation's exposed
# corners look like the out-of-frame padding the model already sees rather than
# a new black wedge it has never seen.
_FILL = (96, 96, 96)


def _transforms(cfg: InstanceAttrsCfg, *, train: bool):
    """Augmentation that cannot destroy the label it is training on.

    NO photometric jitter. The obvious recipe for a small image dataset is
    ColorJitter on brightness/contrast/saturation, and it is precisely wrong
    here: `shaded` versus `direct_sun` IS a brightness and contrast judgement
    ("most of the body sits in panel shade" vs "casting its own sharp shadow"),
    so jittering brightness relabels the sample without telling the loss. The
    same argument rules out grayscale.

    NO vertical flip either — `lying` versus `standing` is an orientation
    judgement, and an upside-down cow is not a pose this corpus contains.

    What remains is geometry and resolution, both of which preserve every label:
      - horizontal flip: the same cow, same light, same thing, facing the other way
      - a mild scale/translate crop: robustness to the detector's box being
        looser or tighter than the one the annotator saw
      - a small rotation (optional): the cameras are not level either
      - random erasing (optional): partial occlusion, which the corpus has
      - gaussian blur (optional): resolution degradation — the corpus spans
        25–400px crops all resized to one size, so a sharp 300px cow and a
        blurry 40px one are the same class, and blur teaches that on purpose
    """
    from torchvision import transforms as T

    norm = T.Normalize(M.IMAGENET_MEAN, M.IMAGENET_STD)
    if not train:
        return T.Compose([T.Resize((cfg.img_size, cfg.img_size)), T.ToTensor(), norm])
    ops: list = [T.RandomResizedCrop(cfg.img_size, scale=(0.75, 1.0), ratio=(0.9, 1.1)),
                 T.RandomHorizontalFlip()]
    if cfg.aug_rotate_deg > 0:
        ops.append(T.RandomRotation(cfg.aug_rotate_deg, fill=_FILL))
    if cfg.aug_blur_p > 0:
        ops.append(T.RandomApply(
            [T.GaussianBlur(kernel_size=5, sigma=(0.2, 1.6))], p=cfg.aug_blur_p))
    ops += [T.ToTensor(), norm]
    if cfg.aug_erase_p > 0:
        # After normalisation, on the tensor; 'random' fills with noise, which
        # is what a genuinely occluding object looks like to a classifier.
        ops.append(T.RandomErasing(p=cfg.aug_erase_p, scale=(0.02, 0.15), value="random"))
    return T.Compose(ops)


def _dataset_class():
    from PIL import Image
    from torch.utils.data import Dataset

    class CropDataset(Dataset):
        """Crops plus a per-head target vector, with NO_LABEL where unlabeled."""

        def __init__(self, root: Path, items: list[dict], heads: dict[str, list[str]],
                     tf):
            self.root = Path(root)
            self.items = items
            self.heads = heads
            self.index = {h: {c: i for i, c in enumerate(cs)} for h, cs in heads.items()}
            self.tf = tf

        def __len__(self) -> int:
            return len(self.items)

        def __getitem__(self, i: int):
            it = self.items[i]
            with Image.open(self.root / it["crop"]) as im:
                x = self.tf(im.convert("RGB"))
            y = {}
            for h, idx in self.index.items():
                c = it["labels"].get(h)
                y[h] = idx.get(c, NO_LABEL) if c is not None else NO_LABEL
            return x, y

    return CropDataset


def _collate(batch):
    import torch

    xs = torch.stack([b[0] for b in batch])
    heads = batch[0][1].keys()
    ys = {h: torch.tensor([b[1][h] for b in batch], dtype=torch.long) for h in heads}
    return xs, ys


def _forward(net, xs, *, tta_hflip: bool):
    """Logits per head, optionally averaged over the horizontal mirror.

    Averaging logits rather than probabilities is deliberate: both views vote
    with their confidence, and a view the model is unsure about does not get
    the same say as one it is sure about.
    """
    import torch

    out = net(xs)
    if not tta_hflip:
        return out
    flipped = net(torch.flip(xs, dims=[3]))
    return {h: (out[h] + flipped[h]) / 2 for h in out}


def confusion(net, loader, heads: dict[str, list[str]], device, *,
              tta_hflip: bool = False) -> dict[str, list[list[int]]]:
    """Per-head confusion matrices over a loader. The raw material for every
    metric here, and what `cv.py` pools across folds so a class can be judged
    on ALL its examples rather than on one fold's handful."""
    import torch

    net.eval()
    conf = {h: [[0] * len(cs) for _ in cs] for h, cs in heads.items()}
    with torch.no_grad():
        for xs, ys in loader:
            out = _forward(net, xs.to(device), tta_hflip=tta_hflip)
            for h, logit in out.items():
                pred = logit.argmax(1).cpu()
                for p, g in zip(pred.tolist(), ys[h].tolist()):
                    if g >= 0:
                        conf[h][g][p] += 1
    return conf


def report_from_confusion(conf: dict[str, list[list[int]]],
                          heads: dict[str, list[str]]) -> dict[str, Any]:
    """Accuracy, macro-F1 and per-class P/R/F1 from confusion matrices.

    The matrix itself is carried in full: with classes this rare, "macro-F1 0.61"
    says nothing about whether the model has simply stopped predicting the
    rarest class, and the matrix says it at a glance.
    """
    report: dict[str, Any] = {}
    for h, cs in heads.items():
        m = conf[h]
        total = sum(sum(r) for r in m)
        correct = sum(m[i][i] for i in range(len(cs)))
        f1s = []
        per_class = {}
        for i, c in enumerate(cs):
            tp = m[i][i]
            fp = sum(m[r][i] for r in range(len(cs))) - tp
            fn = sum(m[i]) - tp
            prec = tp / (tp + fp) if tp + fp else 0.0
            rec = tp / (tp + fn) if tp + fn else 0.0
            f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
            # A class with no examples is not a zero to average in — it is
            # unmeasured, and averaging it as 0 would silently drag macro-F1
            # down by a constant and make runs incomparable.
            if sum(m[i]) > 0:
                f1s.append(f1)
            per_class[c] = {"support": sum(m[i]), "precision": round(prec, 4),
                            "recall": round(rec, 4), "f1": round(f1, 4)}
        report[h] = {
            "support": total,
            "accuracy": round(correct / total, 4) if total else 0.0,
            "macro_f1": round(sum(f1s) / len(f1s), 4) if f1s else 0.0,
            "per_class": per_class,
            "confusion": m,
            "classes": list(cs),
        }
    return report


def _evaluate(net, loader, heads, device, *, tta_hflip: bool = False) -> dict[str, Any]:
    return report_from_confusion(confusion(net, loader, heads, device, tta_hflip=tta_hflip),
                                 heads)


def mean_macro_f1(report: dict[str, Any]) -> float:
    return sum(r["macro_f1"] for r in report.values()) / max(1, len(report))


def _class_counts(items: list[dict], heads: dict[str, list[str]]) -> dict[str, list[int]]:
    counts = {h: [0] * len(cs) for h, cs in heads.items()}
    for it in items:
        for h, cs in heads.items():
            c = it["labels"].get(h)
            if c in cs:
                counts[h][cs.index(c)] += 1
    return counts


def fit(cfg: InstanceAttrsCfg, root: Path, heads: dict[str, list[str]],
        tr_items: list[dict], va_items: list[dict], *, device: str,
        pretrained: bool = True, log=None) -> dict[str, Any]:
    """Train on `tr_items`, early-stop on `va_items`, return the best model + report.

    The single training loop behind both `train` and `cv.cross_validate`. Returns
    {"model", "report", "history", "best", "epochs", "counts"}; the model is on
    CPU with the best epoch's (EMA, if enabled) weights loaded.
    """
    import torch
    from torch.utils.data import DataLoader

    torch.manual_seed(cfg.seed)
    CropDataset = _dataset_class()
    tr = CropDataset(root, tr_items, heads, _transforms(cfg, train=True))
    va = CropDataset(root, va_items, heads, _transforms(cfg, train=False))
    tl = DataLoader(tr, batch_size=cfg.batch_size, shuffle=True,
                    num_workers=cfg.num_workers, collate_fn=_collate)
    vl = DataLoader(va, batch_size=cfg.batch_size, shuffle=False,
                    num_workers=cfg.num_workers, collate_fn=_collate)

    net = M.build_model(heads, backbone=cfg.backbone, pretrained=pretrained).to(device)

    # Weights from the TRAIN split only. Deriving them from the whole manifest
    # would leak the val class balance into the training objective — a small
    # leak, but a free one to avoid.
    counts = _class_counts(tr_items, heads)
    weights = {h: M.class_weights(counts[h], power=cfg.weight_power, device=device)
               for h in heads}

    # Discriminative learning rates: the heads are random and may want to move
    # fast; the trunk is pretrained and mostly wants to stay near where it is.
    head_params = list(net.classifiers.parameters())
    head_ids = {id(p) for p in head_params}
    trunk_params = [p for p in net.parameters() if id(p) not in head_ids]
    opt = torch.optim.AdamW(
        [{"params": trunk_params, "lr": cfg.lr},
         {"params": head_params, "lr": cfg.lr * cfg.head_lr_mult}],
        weight_decay=cfg.weight_decay)

    # Linear warmup into cosine decay, per epoch. Warmup matters more with a
    # higher head LR: the heads' first few steps on random init would otherwise
    # send large gradients back into the pretrained trunk.
    total = max(1, cfg.epochs)
    warm = max(0, min(cfg.warmup_epochs, total - 1))

    def lr_at(epoch: int) -> float:          # epoch is 0-based
        if epoch < warm:
            return (epoch + 1) / (warm + 1)
        t = (epoch - warm) / max(1, total - warm)
        return 0.5 * (1 + math.cos(math.pi * t))

    sched = torch.optim.lr_scheduler.LambdaLR(opt, lr_at)

    # EMA of the weights, evaluated in place of the raw ones. `use_buffers` so
    # BatchNorm running stats are averaged too, not just parameters — otherwise
    # the averaged weights are evaluated against un-averaged statistics.
    ema = None
    if cfg.ema_decay > 0:
        from torch.optim.swa_utils import AveragedModel, get_ema_multi_avg_fn
        ema = AveragedModel(net, multi_avg_fn=get_ema_multi_avg_fn(cfg.ema_decay),
                            use_buffers=True)

    def eval_model():
        return ema.module if ema is not None else net

    best = -1.0
    best_state = None
    best_report: dict[str, Any] = {}
    since = 0
    history = []
    for epoch in range(1, cfg.epochs + 1):
        net.train()
        run = 0.0
        for xs, ys in tl:
            xs = xs.to(device)
            ys = {h: t.to(device) for h, t in ys.items()}
            opt.zero_grad(set_to_none=True)
            loss, _parts = M.masked_loss(net(xs), ys, weights,
                                         label_smoothing=cfg.label_smoothing)
            loss.backward()
            opt.step()
            if ema is not None:
                ema.update_parameters(net)
            run += float(loss.detach())
        sched.step()
        report = _evaluate(eval_model(), vl, heads, device, tta_hflip=cfg.tta_hflip)
        mf1 = mean_macro_f1(report)
        row = {"epoch": epoch, "train_loss": round(run / max(1, len(tl)), 4),
               "mean_macro_f1": round(mf1, 4),
               **{f"f1_{h}": r["macro_f1"] for h, r in report.items()}}
        history.append(row)
        if log is not None:
            log(row)
        if mf1 > best:
            best, since = mf1, 0
            best_report = report
            # Kept on CPU so the best epoch survives regardless of what the
            # device does later, and so the saved tensors are device-agnostic.
            best_state = {k: v.detach().cpu().clone()
                          for k, v in eval_model().state_dict().items()}
        else:
            since += 1
            if since >= cfg.patience:
                break

    final = M.build_model(heads, backbone=cfg.backbone, pretrained=False)
    if best_state is not None:
        final.load_state_dict(best_state)
    final.eval()
    return {"model": final, "report": best_report, "history": history,
            "best": round(best, 4), "epochs": len(history), "counts": counts}


def train(config: Config, *, dataset_dir: str | Path | None = None,
          out: str | Path | None = None, pretrained: bool = True) -> dict[str, Any]:
    """Train on the manifest's split, save the best checkpoint. Returns metrics."""
    cfg = config.instance_attrs
    root = Path(dataset_dir) if dataset_dir is not None else Path(cfg.dataset_dir)
    man = load_manifest(root)
    heads = {h: list(cs) for h, cs in man["heads"].items()}
    device = resolve_device(cfg.device)
    tr_items = [i for i in man["items"] if i["split"] == "train"]
    va_items = [i for i in man["items"] if i["split"] == "val"]

    t0 = time.time()
    r = fit(cfg, root, heads, tr_items, va_items, device=device, pretrained=pretrained)
    metrics = {
        "mean_macro_f1": r["best"],
        "epochs_run": r["epochs"],
        "seconds": round(time.time() - t0, 1),
        "device": device,
        "backbone": cfg.backbone,
        "recipe": {k: getattr(cfg, k) for k in (
            "weight_power", "label_smoothing", "ema_decay", "warmup_epochs",
            "head_lr_mult", "aug_rotate_deg", "aug_erase_p", "aug_blur_p",
            "tta_hflip", "lr", "batch_size", "img_size")},
        "train_size": len(tr_items),
        "val_size": len(va_items),
        "train_class_counts": {h: dict(zip(heads[h], r["counts"][h])) for h in heads},
        "heads": r["report"],
        "history": r["history"],
    }
    dest = Path(out) if out is not None else Path(cfg.weights)
    rev = M.save_checkpoint(dest, model=r["model"], heads=heads, img_size=cfg.img_size,
                            metrics=metrics, manifest_config=man.get("config", {}))
    metrics["rev"] = rev
    metrics["weights"] = str(dest)
    (dest.parent / f"{dest.stem}.metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def format_report(metrics: dict[str, Any]) -> str:
    """The human-readable summary the CLI prints and a reviewer reads."""
    out = [
        f"rev {metrics.get('rev', '?')} · {metrics.get('backbone', '?')} · "
        f"{metrics['train_size']} train / {metrics['val_size']} val · "
        f"{metrics['epochs_run']} epochs on {metrics['device']} in {metrics['seconds']}s",
        f"mean macro-F1 {metrics['mean_macro_f1']:.3f}",
    ]
    for head, r in metrics["heads"].items():
        out.append(f"\n  {head}:  acc {r['accuracy']:.3f}   macro-F1 {r['macro_f1']:.3f}"
                   f"   (n={r['support']})")
        for c, s in r["per_class"].items():
            short = c.split(".")[-1]
            flag = "  <- no val examples" if s["support"] == 0 else ""
            out.append(f"      {short:<14} n={s['support']:<4} "
                       f"P={s['precision']:.2f} R={s['recall']:.2f} F1={s['f1']:.2f}{flag}")
    return "\n".join(out)
