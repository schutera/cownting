"""The queue's size preselection: the floor, the priority, and the ordinal trap.

Measured motivation (1,164 served instances, 2026-09-10): only 43.8% of what the
queue handed out produced a substantive answer. 563 `low_resolution` flags plus
54 not_visible/cannot_tell answers cost the same annotator time as a real one and
taught nobody anything.

Three things are pinned, and the third is the dangerous one:

  - THE FLOOR REMOVES. A crop below `min_instance_px` carries ~13% odds of an
    answer and must never be served.
  - THE FLOOR IS NOT A BAN. Yield keeps rising as the floor rises, so the
    tempting move is to cut where yield peaks — and a corpus of only near
    animals teaches the model nothing about distant ones, which are exactly the
    ones the detector is worst at. So sub-clear instances are DEPRIORITISED, and
    a deliberate `small_share` of them is promoted anyway.
  - THE FLOOR MUST NOT MOVE `ordinal`. `bbox_*` are not partition columns of the
    ordinal window, so filtering on them in `_scan_sql`'s innermost CTE would
    renumber ordinals, change `instance_key`, and orphan every label already
    written for the rows that shift. It has to sit at the `scoped` level. That
    failure writes no error anywhere, so it gets a direct test.

No pytest. Run either way:
    py -3.11 -m tests.test_queue_preselect
    py -3.11 tests/test_queue_preselect.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from cownting import db, labeling, labels_db  # noqa: E402
from cownting.config import AnnotationCfg, Config, PathsCfg  # noqa: E402

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


def _cfg(d: str, **over) -> Config:
    ann = {"min_instance_px": 25, "clear_instance_px": 60, "small_share": 0.15,
           "targets_per_instance": 2, "overlap_fraction": 0.0, "skip_retire": 3,
           # The queue clamps a request to max_batch_size; these fixtures are
           # deliberately larger than the shipped default of 50.
           "batch_size": 500, "max_batch_size": 500}
    ann.update(over)
    return Config(cameras=[], annotation=AnnotationCfg(**ann),
                  paths=PathsCfg(db_path=os.path.join(d, "c.duckdb"),
                                 labels_db_path=os.path.join(d, "labels.duckdb"),
                                 artifacts_dir=os.path.join(d, "artifacts")))


def _seed(cfg: Config, sizes: list[float]) -> None:
    """One frame per instance so each gets ordinal 0 and a distinct key."""
    rows = []
    for i, side in enumerate(sizes):
        rows.append(dict(
            dataset_id="d1", camera_id="cam1", ts=pd.Timestamp("2025-07-03 10:00"),
            time_bin=0, frame_path=f"/f/{i:08d}.jpg", score=0.9,
            bbox_x1=0.0, bbox_y1=0.0, bbox_x2=side, bbox_y2=side,
            area_px=side * side, posture="standing"))
    con = db.connect(cfg.paths.db_path)
    db.init_db(con)
    db.insert_detections(con, pd.DataFrame(rows))
    con.close()
    labeling.ensure_labels_db(cfg)


def _side(item: dict) -> int:
    """The instance's longer side — what the floor is measured on. NOT bbox[2],
    which is the far corner and equals the side only for a box at the origin."""
    x1, y1, x2, y2 = item["bbox"]
    return round(max(x2 - x1, y2 - y1))


def _serve(cfg: Config, annotator="alice", n=200) -> list[dict]:
    con = db.connect(cfg.paths.db_path)
    try:
        return labeling.queue(con, cfg, annotator=annotator, limit=n)["items"]
    finally:
        con.close()


def test_the_floor_removes_what_cannot_be_answered():
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        _seed(cfg, [10.0, 15.0, 24.0, 25.0, 40.0, 120.0])
        got = sorted(_side(i) for i in _serve(cfg))
        check("everything under min_instance_px is gone", got == [25, 40, 120], str(got))
        check("...and the boundary itself is INCLUDED (>=, not >)", 25 in got, str(got))


def test_the_floor_is_reflected_in_the_pool_the_panel_counts_down():
    """If progress() and queue() disagree, the panel counts toward instances the
    queue will never serve and never reaches "you're caught up"."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d)
        _seed(cfg, [10.0, 12.0, 40.0, 120.0])
        con = db.connect(cfg.paths.db_path)
        try:
            p = labeling.progress(con, cfg, annotator="alice")
        finally:
            con.close()
        check("the pool counts only what is servable", p["pool_total"] == 2, str(p))
        check("...and so does `remaining`", p["remaining"] == 2, str(p))
        check("queue and progress agree exactly", len(_serve(cfg)) == p["remaining"])


def test_clear_crops_are_served_before_small_ones():
    with tempfile.TemporaryDirectory() as d:
        # small_share 0 so the priority is visible without promotions mixed in.
        cfg = _cfg(d, small_share=0.0)
        _seed(cfg, [30.0] * 10 + [200.0] * 10)
        head = [_side(i) for i in _serve(cfg)][:10]
        check("the first page is all comfortably-legible crops",
              set(head) == {200}, str(head))


def test_small_instances_are_deprioritised_but_not_banished():
    """A corpus of only near animals teaches the model nothing about distant
    ones — which are exactly the ones the detector is worst at."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, small_share=0.5)
        _seed(cfg, [30.0] * 40 + [200.0] * 20)
        items = _serve(cfg)
        sizes = [round(i["bbox"][2]) for i in items]
        check("every instance above the floor is still reachable",
              len(items) == 60, str(len(items)))
        promoted = sum(1 for s in sizes[:40] if s == 30)
        check("a real share of small crops rides in the front of the queue",
              promoted > 0, f"{promoted} small in the first 40")
        # ...and the promotion is a property of the INSTANCE, not the viewer, so
        # two annotators can still meet on the same small crop.
        a = {i["instance_key"] for i in _serve(cfg)[:30]}
        b = {i["instance_key"] for i in _serve(cfg, annotator="bob")[:30]}
        check("two annotators walk the pool in different orders",
              a != b, f"{len(a - b)} of 30 differ")
        check("...but overlap, so pairs still form", len(a & b) > 0, str(len(a & b)))


def test_the_floor_does_not_renumber_ordinals_or_move_keys():
    """THE TRAP. Filtering on bbox in the innermost CTE would renumber `ordinal`,
    change `instance_key`, and orphan every label already written for the rows
    that shift — silently, with no error anywhere.

    Two instances share one frame: a tiny one at ordinal 0 and a big one at
    ordinal 1. Introducing a floor that removes the tiny one must leave the big
    one's key EXACTLY as it was.
    """
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, min_instance_px=0)
        con = db.connect(cfg.paths.db_path)
        db.init_db(con)
        db.insert_detections(con, pd.DataFrame([
            dict(dataset_id="d1", camera_id="cam1", ts=pd.Timestamp("2025-07-03 10:00"),
                 time_bin=0, frame_path="/f/00000001.jpg", score=0.9,
                 bbox_x1=0.0, bbox_y1=0.0, bbox_x2=10.0, bbox_y2=10.0, area_px=100.0),
            dict(dataset_id="d1", camera_id="cam1", ts=pd.Timestamp("2025-07-03 10:00"),
                 time_bin=0, frame_path="/f/00000001.jpg", score=0.9,
                 bbox_x1=500.0, bbox_y1=500.0, bbox_x2=700.0, bbox_y2=700.0,
                 area_px=40000.0),
        ]))
        con.close()
        labeling.ensure_labels_db(cfg)

        before = {_side(i): i["instance_key"] for i in _serve(cfg)}
        check("both instances are served with no floor", len(before) == 2, str(before))

        cfg.annotation.min_instance_px = 25          # now exclude the tiny one
        after = {_side(i): i["instance_key"] for i in _serve(cfg)}
        check("the tiny instance is gone", set(after) == {200}, str(after))
        check("THE SURVIVOR'S KEY IS UNCHANGED — its labels are not orphaned",
              after[200] == before[200], f"{before[200]} -> {after[200]}")


def test_a_label_already_written_below_the_floor_is_not_destroyed():
    """The floor changes what is OFFERED, never what is stored. Work done on a
    small crop before the floor existed stays valid and stays counted."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _cfg(d, min_instance_px=0)
        _seed(cfg, [12.0, 200.0])
        tiny = next(i for i in _serve(cfg) if _side(i) == 12)
        lcon = labeling.labels_connect(cfg)
        labels_db.submit_annotation(
            lcon, instance_key=tiny["instance_key"], annotator="alice",
            choices=[{"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
                      "class_name": "Shaded"}])
        n = lcon.execute("SELECT count(*) FROM annotations").fetchone()[0]
        lcon.close()
        check("the answer was stored", n == 1, str(n))

        cfg.annotation.min_instance_px = 25
        lcon = labeling.labels_connect(cfg)
        still = lcon.execute(
            "SELECT count(*) FROM annotations WHERE instance_key = ?",
            [tiny["instance_key"]]).fetchone()[0]
        lcon.close()
        check("raising the floor does not touch it", still == 1, str(still))
        check("...it is simply no longer offered",
              tiny["instance_key"] not in {i["instance_key"] for i in _serve(cfg)})


def main():
    print("=== test_queue_preselect ===")
    test_the_floor_removes_what_cannot_be_answered()
    test_the_floor_is_reflected_in_the_pool_the_panel_counts_down()
    test_clear_crops_are_served_before_small_ones()
    test_small_instances_are_deprioritised_but_not_banished()
    test_the_floor_does_not_renumber_ordinals_or_move_keys()
    test_a_label_already_written_below_the_floor_is_not_destroyed()
    print("============================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
