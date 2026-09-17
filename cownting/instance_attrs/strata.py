"""Metrics by crop size — the report that says what production will see.

The labelled corpus is much bigger-crop than the pool the model scores. Half of
the 46,486 detections are 25–60px on the animal's longer side; that band is
~18% of the labelled data, because a crop that small was usually flagged
`low_resolution` and a flag carries no class answer, so it never entered the
dataset. Train and val agree with each other (median 84px both) and disagree
with production (median 57px). A single headline number is therefore
optimistic, and the head it flatters most is the gate, which scores every crop
there is.

So every report here is also cut by size band. "Gate F1 0.94" becomes "0.97 on
90px+, 0.9 on 25–60px, 0.7 under 25px", which is the number an operator needs
to decide `cow_threshold` and the number a reviewer needs to believe.

Bands match the queue's own vocabulary (`annotation.min_instance_px` = 25,
`clear_instance_px` = 60), so a figure here reads directly against what the
queue serves and what the floor removed.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from ..config import InstanceAttrsCfg
from . import train as T

SIZE_BANDS: tuple[tuple[str, float, float], ...] = (
    ("<25", 0.0, 25.0),
    ("25-60", 25.0, 60.0),
    ("60-90", 60.0, 90.0),
    ("90+", 90.0, math.inf),
)


def long_side(item: dict) -> float:
    """The animal's longer side in SOURCE pixels — what the queue floors on."""
    x1, y1, x2, y2 = item["bbox"]
    return max(float(x2) - float(x1), float(y2) - float(y1))


def band_of(side: float) -> str:
    for name, lo, hi in SIZE_BANDS:
        if lo <= side < hi:
            return name
    return SIZE_BANDS[-1][0]


def predict(net, root: Path, items: list[dict], heads: dict[str, list[str]],
            cfg: InstanceAttrsCfg, device: str
            ) -> dict[str, tuple[list[int], list[int], list[list[float]]]]:
    """Per head: (predictions, targets, softmax rows) in ITEM ORDER, NO_LABEL
    where unlabeled.

    Item order is the contract that lets a caller slice by any per-item
    attribute — size here, but a camera or a day would work the same way. The
    loader is unshuffled for exactly that reason.
    """
    import torch
    from torch.utils.data import DataLoader

    ds = T._dataset_class()(root, items, heads, T._transforms(cfg, train=False))
    dl = DataLoader(ds, batch_size=cfg.batch_size, shuffle=False, collate_fn=T._collate)
    preds: dict[str, list[int]] = {h: [] for h in heads}
    targs: dict[str, list[int]] = {h: [] for h in heads}
    probs: dict[str, list[list[float]]] = {h: [] for h in heads}
    net = net.to(device).eval()
    with torch.no_grad():
        for xs, ys in dl:
            out = T._forward(net, xs.to(device), tta_hflip=cfg.tta_hflip)
            for h in heads:
                sm = torch.softmax(out[h], dim=1).cpu()
                preds[h].extend(sm.argmax(1).tolist())
                targs[h].extend(ys[h].tolist())
                # The full distribution, not just the argmax: threshold and
                # calibration questions ("what if we only call not_visible
                # above p=0.3?") need it, and it costs nothing to keep.
                probs[h].extend([[round(v, 5) for v in row] for row in sm.tolist()])
    return {h: (preds[h], targs[h], probs[h]) for h in heads}


def confusion_from(pred_targ: dict[str, tuple[list[int], list[int]]],
                   heads: dict[str, list[str]],
                   keep: list[bool] | None = None) -> dict[str, list[list[int]]]:
    """Confusion matrices over the items where `keep` is True (all, if None)."""
    conf = {h: [[0] * len(cs) for _ in cs] for h, cs in heads.items()}
    for h in heads:
        p, t = pred_targ[h][0], pred_targ[h][1]
        for i, (pi, ti) in enumerate(zip(p, t)):
            if ti >= 0 and (keep is None or keep[i]):
                conf[h][ti][pi] += 1
    return conf


def stratified(pred_targ: dict[str, tuple[list[int], list[int]]], items: list[dict],
               heads: dict[str, list[str]]) -> dict[str, Any]:
    """{"all": report, "by_size": {band: report}, "band_counts": {band: n}}."""
    sides = [long_side(it) for it in items]
    bands = [band_of(s) for s in sides]
    out: dict[str, Any] = {
        "all": T.report_from_confusion(confusion_from(pred_targ, heads), heads),
        "by_size": {},
        "band_counts": {},
    }
    for name, _lo, _hi in SIZE_BANDS:
        keep = [b == name for b in bands]
        n = sum(keep)
        out["band_counts"][name] = n
        if n:
            out["by_size"][name] = T.report_from_confusion(
                confusion_from(pred_targ, heads, keep), heads)
    return out


def stratified_report(net, root: Path, items: list[dict], heads: dict[str, list[str]],
                      cfg: InstanceAttrsCfg, device: str) -> dict[str, Any]:
    return stratified(predict(net, root, items, heads, cfg, device), items, heads)


def format_by_size(strat: dict[str, Any], *, title: str = "") -> str:
    """One line per head per band: n and macro-F1, with the gate's not_cow F1
    called out because it is the number the size bias hides most."""
    lines = [title] if title else []
    heads = list(strat["all"].keys())
    hdr = f"  {'band':<7}{'n':>6}  " + "  ".join(f"{h:>14}" for h in heads)
    lines.append(hdr)
    for name, _lo, _hi in SIZE_BANDS:
        rep = strat["by_size"].get(name)
        n = strat["band_counts"].get(name, 0)
        if rep is None:
            lines.append(f"  {name:<7}{n:>6}  " + "  ".join(f"{'—':>14}" for _ in heads))
            continue
        cells = []
        for h in heads:
            r = rep[h]
            cells.append(f"{r['macro_f1']:.2f} (n={r['support']})".rjust(14))
        lines.append(f"  {name:<7}{n:>6}  " + "  ".join(cells))
    rep = strat["all"]
    cells = [f"{rep[h]['macro_f1']:.2f} (n={rep[h]['support']})".rjust(14) for h in heads]
    lines.append(f"  {'ALL':<7}{sum(strat['band_counts'].values()):>6}  " + "  ".join(cells))
    if "cow" in heads:
        lines.append("  gate not_cow F1 by band: " + "  ".join(
            f"{name} {strat['by_size'][name]['cow']['per_class'].get('not_cow', {}).get('f1', 0):.2f}"
            for name, _, _ in SIZE_BANDS if name in strat["by_size"]))
    return "\n".join(lines)
