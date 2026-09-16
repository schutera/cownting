"""Size-stratified metrics: the bands partition, and each band's numbers are its own.

The labelled corpus is bigger-crop than the pool the model scores (val median
84px, production median 57px), so a single headline is optimistic for
production. This module cuts every report by size band. What can go wrong
silently: a band boundary off by one (an item at exactly 25px lands on the
wrong side of the floor it was named after), a band that double-counts or
drops items, or a band's report being computed over the wrong items.

No pytest. Run either way:
    py -3.11 -m tests.test_instance_attrs_strata
    py -3.11 tests/test_instance_attrs_strata.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.config import InstanceAttrsCfg  # noqa: E402
from cownting.instance_attrs import model as M  # noqa: E402
from cownting.instance_attrs import strata as S  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


HEADS = {"cow": ["not_cow", "cow"], "sun_exposure": ["a", "b"]}


def test_bands_partition_on_the_queue_boundaries():
    check("24.9 is under the floor", S.band_of(24.9) == "<25")
    check("exactly 25 is NOT — the floor is >=, and so is the band",
          S.band_of(25.0) == "25-60")
    check("59.9 is small", S.band_of(59.9) == "25-60")
    check("exactly 60 is clear", S.band_of(60.0) == "60-90")
    check("90 and above is the top band", S.band_of(90.0) == "90+" and S.band_of(5000) == "90+")
    check("long_side is the LONGER side, from any box origin",
          S.long_side({"bbox": [500, 500, 530, 700]}) == 200.0)


def _items(d: str, sides: list[float]) -> list[dict]:
    """Tiny real JPEGs so the predict path (open, transform, batch) is exercised."""
    import numpy as np
    from PIL import Image

    crops = Path(d) / "crops"
    crops.mkdir()
    items = []
    rng = np.random.default_rng(0)
    for i, s in enumerate(sides):
        name = f"crops/{i}.jpg"
        Image.fromarray(rng.integers(0, 255, (16, 16, 3), dtype=np.uint8)).save(Path(d) / name)
        items.append({"crop": name, "bbox": [0.0, 0.0, s, s * 0.6],
                      "labels": {"cow": "cow" if i % 3 else "not_cow",
                                 **({"sun_exposure": "a" if i % 2 else "b"} if i % 4 else {})}})
    return items


def test_bands_cover_every_item_exactly_once_and_reports_agree():
    with tempfile.TemporaryDirectory() as d:
        sides = [10, 20, 30, 45, 59, 60, 75, 89, 90, 150, 400, 24.99, 25.0]
        items = _items(d, sides)
        net = M.build_model(HEADS, pretrained=False)
        cfg = InstanceAttrsCfg(img_size=16, batch_size=4, device="cpu")
        pt = S.predict(net, Path(d), items, HEADS, cfg, "cpu")
        check("predictions come back in item order, one per item",
              all(len(pt[h][0]) == len(items) and len(pt[h][1]) == len(items) for h in HEADS))
        # A NO_LABEL target where the item carries no answer for that head.
        unl = [i for i, it in enumerate(items) if "sun_exposure" not in it["labels"]]
        check("unlabeled heads carry the sentinel, not a class",
              all(pt["sun_exposure"][1][i] == -1 for i in unl), str(unl))

        strat = S.stratified(pt, items, HEADS)
        counts = strat["band_counts"]
        check("the bands partition the items", sum(counts.values()) == len(items), str(counts))
        check("...on the right boundaries",
              counts == {"<25": 3, "25-60": 4, "60-90": 3, "90+": 3}, str(counts))
        # Every labeled item is counted in exactly one band, and the bands add
        # up to the overall matrix — the property that makes "0.9 on 25–60px"
        # a slice of the headline rather than a different measurement.
        for h in HEADS:
            total = strat["all"][h]["support"]
            parts = sum(r[h]["support"] for r in strat["by_size"].values())
            check(f"{h}: band supports sum to the overall support",
                  parts == total, f"{parts} vs {total}")
            summed = [[0] * len(HEADS[h]) for _ in HEADS[h]]
            for r in strat["by_size"].values():
                for a, row in enumerate(r[h]["confusion"]):
                    for b, v in enumerate(row):
                        summed[a][b] += v
            check(f"{h}: band confusions sum to the overall confusion",
                  summed == strat["all"][h]["confusion"])


def test_a_band_is_scored_only_on_its_own_items():
    """Stub predictions so the arithmetic is checkable by hand: the gate is right
    on every big crop and wrong on every small one. Then the 90+ band must read
    1.0 and the <25 band 0.0 — not a blend."""
    items = [{"bbox": [0, 0, s, s], "labels": {"cow": "cow"}} for s in (10, 12, 100, 120)]
    heads = {"cow": ["not_cow", "cow"]}
    pt = {"cow": ([0, 0, 1, 1], [1, 1, 1, 1])}          # wrong, wrong, right, right
    strat = S.stratified(pt, items, heads)
    small = strat["by_size"]["<25"]["cow"]
    big = strat["by_size"]["90+"]["cow"]
    check("small band: every prediction wrong", small["accuracy"] == 0.0, str(small["accuracy"]))
    check("big band: every prediction right", big["accuracy"] == 1.0, str(big["accuracy"]))
    check("overall is the blend", strat["all"]["cow"]["accuracy"] == 0.5)
    out = S.format_by_size(strat, title="t")
    check("the formatter prints one row per band plus ALL",
          out.count("\n") >= 5 and "ALL" in out, out)


def main():
    print("=== test_instance_attrs_strata ===")
    test_bands_partition_on_the_queue_boundaries()
    test_bands_cover_every_item_exactly_once_and_reports_agree()
    test_a_band_is_scored_only_on_its_own_items()
    print("==================================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
