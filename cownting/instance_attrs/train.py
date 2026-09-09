"""Fine-tune the multi-head crop classifier and report what it actually learned.

Reports MACRO-F1 per head, not accuracy. Accuracy is actively misleading on
every head here: the gate is 89% cow, so "always cow" scores 89% while doing
precisely nothing, and the behaviour head's `head_probing` (9 train / 3 val) can
be ignored entirely at a cost of under 2%. Macro-F1 makes the rare classes count
as much as the common ones, which is the only reading under which this model is
worth deploying — and the same reason it is the early-stopping criterion.

Every val number in the report is over a block-disjoint split (see dataset.py),
so it is a claim about a camera-day the model has not seen rather than about
crops adjacent to ones it memorised.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from ..config import Config, resolve_device
from . import model as M
from .dataset import load_manifest

# The "no label for this head" sentinel. Matches the mask in `M.masked_loss`;
# torch's own ignore_index default is -100, but the mask here is explicit
# because a sample is legitimately unlabeled on most heads and that is the
# common case rather than an error path.
NO_LABEL = -1


def _transforms(img_size: int, *, train: bool):
    """Augmentation that cannot destroy the label it is training on.

    NO photometric jitter. The obvious recipe for a small image dataset is
    ColorJitter on brightness/contrast/saturation, and it is precisely wrong
    here: `shaded` versus `direct_sun` IS a brightness and contrast judgement
    ("most of the body sits in panel shade" vs "casting its own sharp shadow"),
    so jittering brightness relabels the sample without telling the loss. The
    same argument rules out grayscale and heavy blur.

    NO vertical flip either — `lying` versus `standing` is an orientation
    judgement, and an upside-down cow is not a pose this corpus contains.

    What remains is geometry that preserves both labels: horizontal flip (a cow
    facing the other way is the same cow, in the same light, doing the same
    thing) and a mild scale/translate crop, which also buys robustness to the
    detector's box being looser or tighter than the one the annotator saw.
    """
    from torchvision import transforms as T

    norm = T.Normalize(M.IMAGENET_MEAN, M.IMAGENET_STD)
    if not train:
        return T.Compose([T.Resize((img_size, img_size)), T.ToTensor(), norm])
    return T.Compose([
        T.RandomResizedCrop(img_size, scale=(0.75, 1.0), ratio=(0.9, 1.1)),
        T.RandomHorizontalFlip(),
        T.ToTensor(),
        norm,
    ])


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


def _evaluate(net, loader, heads: dict[str, list[str]], device) -> dict[str, Any]:
    """Per-head accuracy, macro-F1 and a confusion matrix over the val split.

    The confusion matrix is reported in full rather than summarised: with
    classes this rare, "macro-F1 0.61" says nothing about whether the model has
    simply stopped predicting `head_probing`, and the matrix says it at a glance.
    """
    import torch

    net.eval()
    n_cls = {h: len(cs) for h, cs in heads.items()}
    conf = {h: [[0] * n for _ in range(n)] for h, n in n_cls.items()}
    with torch.no_grad():
        for xs, ys in loader:
            out = net(xs.to(device))
            for h, logit in out.items():
                pred = logit.argmax(1).cpu()
                t = ys[h]
                for p, g in zip(pred.tolist(), t.tolist()):
                    if g >= 0:
                        conf[h][g][p] += 1

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
            # A class with no val examples is not a zero to average in — it is
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


def train(config: Config, *, dataset_dir: str | Path | None = None,
          out: str | Path | None = None, pretrained: bool = True) -> dict[str, Any]:
    """Train, early-stop on mean macro-F1, save the best checkpoint. Returns metrics."""
    import torch
    from torch.utils.data import DataLoader

    cfg = config.instance_attrs
    root = Path(dataset_dir) if dataset_dir is not None else Path(cfg.dataset_dir)
    man = load_manifest(root)
    heads = {h: list(cs) for h, cs in man["heads"].items()}
    device = resolve_device(cfg.device)
    torch.manual_seed(cfg.seed)

    CropDataset = _dataset_class()
    tr_items = [i for i in man["items"] if i["split"] == "train"]
    va_items = [i for i in man["items"] if i["split"] == "val"]
    tr = CropDataset(root, tr_items, heads, _transforms(cfg.img_size, train=True))
    va = CropDataset(root, va_items, heads, _transforms(cfg.img_size, train=False))
    tl = DataLoader(tr, batch_size=cfg.batch_size, shuffle=True,
                    num_workers=cfg.num_workers, collate_fn=_collate, drop_last=False)
    vl = DataLoader(va, batch_size=cfg.batch_size, shuffle=False,
                    num_workers=cfg.num_workers, collate_fn=_collate)

    net = M.build_model(heads, pretrained=pretrained).to(device)

    # Weights from the TRAIN split only. Deriving them from the whole manifest
    # would leak the val class balance into the training objective — a small
    # leak, but a free one to avoid.
    counts = {h: [0] * len(cs) for h, cs in heads.items()}
    for it in tr_items:
        for h, cs in heads.items():
            c = it["labels"].get(h)
            if c in cs:
                counts[h][cs.index(c)] += 1
    weights = {h: M.class_weights(counts[h], device=device) for h in heads}

    opt = torch.optim.AdamW(net.parameters(), lr=cfg.lr, weight_decay=cfg.weight_decay)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=max(1, cfg.epochs))

    best = -1.0
    best_state = None
    best_report: dict[str, Any] = {}
    since = 0
    history = []
    t0 = time.time()
    for epoch in range(1, cfg.epochs + 1):
        net.train()
        run = 0.0
        for xs, ys in tl:
            xs = xs.to(device)
            ys = {h: t.to(device) for h, t in ys.items()}
            opt.zero_grad(set_to_none=True)
            loss, _parts = M.masked_loss(net(xs), ys, weights)
            loss.backward()
            opt.step()
            run += float(loss.detach())
        sched.step()
        report = _evaluate(net, vl, heads, device)
        mean_f1 = sum(r["macro_f1"] for r in report.values()) / len(report)
        history.append({"epoch": epoch, "train_loss": round(run / max(1, len(tl)), 4),
                        "mean_macro_f1": round(mean_f1, 4),
                        **{f"f1_{h}": r["macro_f1"] for h, r in report.items()}})
        if mean_f1 > best:
            best, since = mean_f1, 0
            best_report = report
            # Kept on CPU so the best epoch survives regardless of what the
            # device does later, and so the saved tensors are device-agnostic.
            best_state = {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}
        else:
            since += 1
            if since >= cfg.patience:
                break

    if best_state is not None:
        net.load_state_dict(best_state)
    metrics = {
        "mean_macro_f1": round(best, 4),
        "epochs_run": len(history),
        "seconds": round(time.time() - t0, 1),
        "device": device,
        "train_size": len(tr_items),
        "val_size": len(va_items),
        "train_class_counts": {h: dict(zip(heads[h], counts[h])) for h in heads},
        "heads": best_report,
        "history": history,
    }
    dest = Path(out) if out is not None else Path(cfg.weights)
    rev = M.save_checkpoint(dest, model=net.cpu(), heads=heads, img_size=cfg.img_size,
                            metrics=metrics, manifest_config=man.get("config", {}))
    metrics["rev"] = rev
    metrics["weights"] = str(dest)
    (dest.parent / f"{dest.stem}.metrics.json").write_text(
        json.dumps(metrics, indent=2), encoding="utf-8")
    return metrics


def format_report(metrics: dict[str, Any]) -> str:
    """The human-readable summary the CLI prints and a reviewer reads."""
    out = [
        f"rev {metrics.get('rev', '?')} · {metrics['train_size']} train / "
        f"{metrics['val_size']} val · {metrics['epochs_run']} epochs on "
        f"{metrics['device']} in {metrics['seconds']}s",
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
