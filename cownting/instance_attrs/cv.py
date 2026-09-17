"""Grouped k-fold cross-validation over blocks — the number to trust.

One train/val split is not aussagekräftig for this corpus. The val side holds
~480 items, and the class that decides the headline (`head_probing`) has sixteen
of them: two predictions either way swing its F1 by 0.2, and macro-F1 weights
that swing equally with the 400-item classes. A recipe change worth +0.02 cannot
be told from noise on one split, and worse, the previous model "scored" 0.38 on
that class on one split and 0.00 on another.

K folds over BLOCKS (the same contiguous camera-day units the split uses, so no
near-duplicate crop ever straddles a fold) fix both problems at once. Every
instance is validated exactly once, so the pooled confusion matrix judges
`head_probing` on all sixty of its examples rather than sixteen — and the
per-fold spread is a real error bar on every number.

Two summaries come out, and they answer different questions:

  - POOLED: confusion matrices summed across folds, metrics derived from the sum.
    "How does this recipe do over the whole corpus." The headline.
  - PER-FOLD mean ± std: "how much does it vary with which camera-days are
    held out." The error bar, and the honest way to compare two recipes — a
    difference smaller than the std is not a difference.

Never saves a checkpoint. A CV run decides a recipe; `train` then ships it.
"""
from __future__ import annotations

import random
import time
from pathlib import Path
from typing import Any

from ..config import Config, resolve_device
from . import strata as S
from . import train as T
from .dataset import load_manifest


def fold_blocks(blocks: dict[str, int], *, k: int, seed: int) -> dict[str, int]:
    """block -> fold index in [0, k). Balanced by SAMPLE count, whole blocks only.

    Largest blocks first into whichever fold is currently lightest (the classic
    longest-processing-time heuristic), with a seeded shuffle breaking ties so a
    corpus of equal-sized blocks does not fold the same way regardless of seed.
    Deterministic given (blocks, k, seed): the shuffle runs over a sorted key
    list, so dict iteration order cannot change the folds.
    """
    keys = sorted(blocks)
    rng = random.Random(seed)
    rng.shuffle(keys)
    keys.sort(key=lambda b: -blocks[b])           # stable: ties keep shuffled order
    load = [0] * k
    out: dict[str, int] = {}
    for b in keys:
        i = min(range(k), key=lambda j: load[j])
        out[b] = i
        load[i] += blocks[b]
    return out


def _sum_conf(a: dict[str, list[list[int]]], b: dict[str, list[list[int]]]):
    return {h: [[x + y for x, y in zip(ra, rb)] for ra, rb in zip(a[h], b[h])] for h in a}


def cross_validate(config: Config, *, dataset_dir: str | Path | None = None,
                   folds: int | None = None, pretrained: bool = True,
                   log=None) -> dict[str, Any]:
    """Run `T.fit` once per fold; return pooled + per-fold metrics."""
    cfg = config.instance_attrs
    k = folds or cfg.cv_folds
    root = Path(dataset_dir) if dataset_dir is not None else Path(cfg.dataset_dir)
    man = load_manifest(root)
    heads = {h: list(cs) for h, cs in man["heads"].items()}
    device = resolve_device(cfg.device)

    items = man["items"]
    sizes: dict[str, int] = {}
    for it in items:
        sizes[it["block"]] = sizes.get(it["block"], 0) + 1
    assign = fold_blocks(sizes, k=k, seed=cfg.split_seed)

    t0 = time.time()
    pooled = None
    per_fold: list[dict[str, Any]] = []
    # Every fold's predictions, in item order, so the size cut at the end is
    # over ALL instances — the rare band and the rare class both get judged on
    # everything they have rather than on one fold's handful.
    all_items: list[dict] = []
    all_pt: dict[str, tuple[list[int], list[int], list[list[float]]]] = {
        h: ([], [], []) for h in heads}
    all_folds: list[int] = []
    for f in range(k):
        va = [it for it in items if assign[it["block"]] == f]
        tr = [it for it in items if assign[it["block"]] != f]
        r = T.fit(cfg, root, heads, tr, va, device=device, pretrained=pretrained)
        # Re-derive the fold's predictions from the best model rather than
        # trusting the report: the report was the early-stopping read and is the
        # same thing, but pooling needs the raw predictions.
        pt = S.predict(r["model"], root, va, heads, cfg, device)
        conf = S.confusion_from(pt, heads)
        pooled = conf if pooled is None else _sum_conf(pooled, conf)
        all_items.extend(va)
        all_folds.extend([f] * len(va))
        for h in heads:
            all_pt[h][0].extend(pt[h][0])
            all_pt[h][1].extend(pt[h][1])
            all_pt[h][2].extend(pt[h][2])
        rep = T.report_from_confusion(conf, heads)
        per_fold.append({"fold": f, "train": len(tr), "val": len(va),
                         "epochs": r["epochs"], "mean_macro_f1": T.mean_macro_f1(rep),
                         "heads": {h: rep[h]["macro_f1"] for h in heads},
                         "per_class": {h: {c: rep[h]["per_class"][c]["f1"] for c in cs}
                                       for h, cs in heads.items()}})
        if log is not None:
            log(per_fold[-1])

    pooled_report = T.report_from_confusion(pooled, heads)
    by_size = S.stratified(all_pt, all_items, heads)

    def mean_std(vals):
        n = len(vals)
        m = sum(vals) / n
        s = (sum((v - m) ** 2 for v in vals) / max(1, n - 1)) ** 0.5
        return round(m, 4), round(s, 4)

    spread = {
        "mean_macro_f1": mean_std([pf["mean_macro_f1"] for pf in per_fold]),
        "heads": {h: mean_std([pf["heads"][h] for pf in per_fold]) for h in heads},
        "per_class": {h: {c: mean_std([pf["per_class"][h][c] for pf in per_fold])
                          for c in cs} for h, cs in heads.items()},
    }
    return {
        "folds": k,
        "instances": len(items),
        "blocks": len(sizes),
        "seconds": round(time.time() - t0, 1),
        "device": device,
        "backbone": cfg.backbone,
        "recipe": {kk: getattr(cfg, kk) for kk in (
            "weight_power", "label_smoothing", "ema_decay", "warmup_epochs",
            "head_lr_mult", "aug_rotate_deg", "aug_erase_p", "aug_blur_p",
            "tta_hflip", "size_reweight", "lr", "batch_size", "img_size", "epochs",
            "patience")},
        "pooled": pooled_report,
        "pooled_mean_macro_f1": round(T.mean_macro_f1(pooled_report), 4),
        "spread": spread,
        "per_fold": per_fold,
        # The cut that says what production will see: the labelled corpus is
        # bigger-crop than the pool the model scores, so the pooled headline is
        # optimistic and this is the number to read alongside it.
        "by_size": by_size["by_size"],
        "band_counts": by_size["band_counts"],
        # OUT-OF-FOLD predictions for every instance: each was scored by the
        # one model that never saw it. This is what a threshold or calibration
        # question is answered on — 2,381 leakage-free probability rows rather
        # than a 477-item split — and it is why the CV keeps the softmax.
        "oof": [
            {"instance_key": it["instance_key"], "fold": all_folds[i],
             "block": it["block"], "bbox": it["bbox"],
             "target": {h: all_pt[h][1][i] for h in heads},
             "prob": {h: all_pt[h][2][i] for h in heads}}
            for i, it in enumerate(all_items)
        ],
    }


def format_cv(res: dict[str, Any]) -> str:
    out = [
        f"{res['folds']}-fold grouped CV · {res['backbone']} · {res['instances']} instances "
        f"over {res['blocks']} blocks · {res['seconds']}s on {res['device']}",
        f"mean macro-F1  pooled {res['pooled_mean_macro_f1']:.3f}   "
        f"per-fold {res['spread']['mean_macro_f1'][0]:.3f} ± {res['spread']['mean_macro_f1'][1]:.3f}",
    ]
    for h, r in res["pooled"].items():
        m, s = res["spread"]["heads"][h]
        out.append(f"\n  {h}:  pooled macro-F1 {r['macro_f1']:.3f}   per-fold {m:.3f} ± {s:.3f}"
                   f"   (n={r['support']})")
        for c, pc in r["per_class"].items():
            cm, cs = res["spread"]["per_class"][h][c]
            out.append(f"      {c.split('.')[-1]:<14} n={pc['support']:<4} "
                       f"pooled F1 {pc['f1']:.2f}   per-fold {cm:.2f} ± {cs:.2f}")
    if res.get("by_size"):
        out.append("")
        out.append(S.format_by_size({"all": res["pooled"], "by_size": res["by_size"],
                                     "band_counts": res["band_counts"]},
                                    title="  by crop size (pooled over all folds), macro-F1:"))
    return "\n".join(out)
