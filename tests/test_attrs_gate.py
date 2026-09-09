"""The false-positive gate: what it removes, and what it must NOT remove.

Two failure modes, opposite in character, both silent:

  - THE GATE DOES NOTHING. A counting query that forgot the predicate leaves two
    surfaces of the same dashboard disagreeing about how many cows there were.
    Every read path below is therefore checked against the same fixture, so a
    new query that forgets it fails here rather than in production.
  - THE GATE REMOVES TOO MUCH. Several series LEFT JOIN detections onto frames
    precisely so a frame with no cows still contributes a zero. Put the
    predicate in their WHERE instead of their ON and the join turns inner,
    deleting every empty frame from the day curve — a worse bug than the one the
    gate fixes, and one that looks like a plausible chart.

Also pinned: NULL means "never scored" and stays counted, so enabling the flag
on an unscored corpus moves nothing.

No pytest. Run either way:
    py -3.11 -m tests.test_attrs_gate
    py -3.11 tests/test_attrs_gate.py
"""
from __future__ import annotations

import os
import sys
import tempfile

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

from cownting import db  # noqa: E402

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


def _fixture(con) -> None:
    """3 frames on one camera: 2 cows + 1 false positive, 1 unscored cow, 1 EMPTY
    frame. The empty frame is the whole point of the LEFT JOIN checks."""
    frames = [{"dataset_id": "d1", "camera_id": "cam1", "frame_idx": i,
               "frame_path": f"/f/{i}.jpg", "processed": True,
               "ts": pd.Timestamp("2025-07-03 10:00") + pd.Timedelta(minutes=i)}
              for i in range(3)]
    db.insert_frames(con, pd.DataFrame(frames))
    rows = [
        # frame 0: two real cows and one hallucination
        dict(frame_path="/f/0.jpg", attr_is_cow=True, attr_cow_p=0.99, posture="standing",
             region_id="cam1::a", under_panel=True, attr_sun="sun_exposure.shaded",
             attr_behaviour="behaviour.lying", attr_model_rev="r1"),
        dict(frame_path="/f/0.jpg", attr_is_cow=True, attr_cow_p=0.95, posture="lying",
             region_id="cam1::a", under_panel=False, attr_sun="sun_exposure.direct_sun",
             attr_behaviour="behaviour.lying", attr_model_rev="r1"),
        dict(frame_path="/f/0.jpg", attr_is_cow=False, attr_cow_p=0.02, posture="standing",
             region_id="cam1::a", under_panel=False, attr_sun="sun_exposure.shaded",
             attr_behaviour="behaviour.standing", attr_model_rev="r1"),
        # frame 1: one cow nobody has scored yet
        dict(frame_path="/f/1.jpg", attr_is_cow=None, attr_cow_p=None, posture="standing",
             region_id="cam1::a", under_panel=False, attr_sun=None,
             attr_behaviour=None, attr_model_rev=None),
        # frame 2: deliberately empty
    ]
    for i, r in enumerate(rows):
        r.update(dataset_id="d1", camera_id="cam1", score=0.9, area_px=100.0,
                 bbox_x1=0.0, bbox_y1=0.0, bbox_x2=10.0, bbox_y2=10.0,
                 ts=pd.Timestamp("2025-07-03 10:00"), time_bin=0)
    db.insert_detections(con, pd.DataFrame(rows))


def _open(d: str):
    con = db.connect(os.path.join(d, "c.duckdb"))
    db.init_db(con)
    _fixture(con)
    return con


def test_gate_off_is_the_old_behaviour():
    with tempfile.TemporaryDirectory() as d:
        db.set_gate(False)
        con = _open(d)
        k = db.kpi_summary(con)
        check("with the gate off every detection counts", k["detections"] == 4, str(k))
        check("...including the one the model rejected",
              db.crosstab(con, "camera")[0]["n"].sum() == 4)
        check("...and the CSV agrees", len(db.export_df(con)) == 4)
        con.close()


def test_gate_on_removes_only_the_rejected_row():
    with tempfile.TemporaryDirectory() as d:
        db.set_gate(True)
        con = _open(d)
        try:
            k = db.kpi_summary(con)
            check("the rejected detection leaves the headline count",
                  k["detections"] == 3, str(k))
            # The unscored row is NOT dropped: NULL means "never looked", and an
            # unscored corpus must behave exactly as it did before M5.
            check("...but the UNSCORED one is kept", k["detections"] == 3, str(k))
            check("posture totals drop it too",
                  k["standing"] == 2 and k["lying"] == 1, str(k))
            check("crosstab drops it", db.crosstab(con, "camera")[0]["n"].sum() == 3)
            check("the CSV drops it", len(db.export_df(con)) == 3)
            areas = db.area_summary(con)
            check("area totals drop it", int(areas["total"].sum()) == 3, str(areas))
            pot = db.posture_over_time(con, "cam1")
            check("posture-over-time drops it", int(pot["n"].sum()) == 3, str(pot))
            sot = db.shelter_over_time(con, "cam1")
            check("shelter-over-time drops it",
                  int(sot["detections"].sum()) == 3, str(sot))
        finally:
            db.set_gate(False)
            con.close()


def test_the_gate_never_deletes_an_empty_frame_from_a_series():
    """The LEFT JOIN trap. Frame 2 has no detections at all; frame 1's only cow
    is unscored. Both must still appear, with the right totals."""
    with tempfile.TemporaryDirectory() as d:
        db.set_gate(True)
        con = _open(d)
        try:
            ds = db.day_series(con)
            check("every frame still contributes a row to the day series",
                  len(ds) == 3, f"{len(ds)} rows")
            check("...and the totals reflect the gate",
                  int(ds["total"].sum()) == 3, str(ds["total"].tolist()))
            check("...with a genuine zero for the empty frame",
                  0 in ds["total"].tolist(), str(ds["total"].tolist()))

            tl = db.timeline_series(con)
            check("the scrubber timeline keeps every instant", len(tl) == 3, str(len(tl)))
            check("...with the gated total", int(tl["n"].sum()) == 3, str(tl["n"].tolist()))

            cot = db.counts_over_time(con, "cam1")
            check("counts-over-time still counts all 3 frames",
                  int(cot["frames"].sum()) == 3, str(cot.to_dict("records")))
            check("...while gating the detections",
                  int(cot["detections"].sum()) == 3, str(cot.to_dict("records")))
        finally:
            db.set_gate(False)
            con.close()


def test_an_unscored_corpus_is_unmoved_by_enabling_the_gate():
    """Both halves have to be deliberate before a single number moves: scoring
    with the flag off changes nothing, and flipping the flag on a corpus nobody
    has scored changes nothing either."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        db.init_db(con)
        _fixture(con)
        con.execute("UPDATE detections SET attr_is_cow = NULL, attr_model_rev = NULL")
        db.set_gate(False)
        before = db.kpi_summary(con)["detections"]
        db.set_gate(True)
        after = db.kpi_summary(con)["detections"]
        db.set_gate(False)
        check("an unscored corpus counts the same either way",
              before == after == 4, f"{before} vs {after}")
        con.close()


def test_the_predicate_lands_in_the_right_clause():
    check("gate off is a no-op predicate", db.gate_clause() == "1=1")
    db.set_gate(True)
    try:
        check("gate on coalesces NULL to counted",
              db.gate_clause() == "coalesce(attr_is_cow, TRUE)", db.gate_clause())
        check("...and qualifies with the alias when given",
              db.gate_clause("d") == "coalesce(d.attr_is_cow, TRUE)", db.gate_clause("d"))
    finally:
        db.set_gate(False)


def main():
    print("=== test_attrs_gate ===")
    test_the_predicate_lands_in_the_right_clause()
    test_gate_off_is_the_old_behaviour()
    test_gate_on_removes_only_the_rejected_row()
    test_the_gate_never_deletes_an_empty_frame_from_a_series()
    test_an_unscored_corpus_is_unmoved_by_enabling_the_gate()
    print("=======================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
