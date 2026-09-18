"""The crop model's verdicts in the downloadable CSV.

The CSV is where this project's numbers leave the building — it is read months
later, by someone with no access to this codebase, in R or a spreadsheet. So
what has to hold is not "the columns exist" but "the three states are
distinguishable by someone who only has the file":

  - a CONFIDENT row carries a class and its probability;
  - an UNSURE row (the model looked and declined, M5 §9.3) carries NO class but
    still carries the probability it would have shown and the model rev — an
    empty class cell with a rev beside it is the only thing that separates it
    from the third state;
  - an UNSCORED row carries no rev at all.

Collapse unsure into unscored and the file quietly claims the model never saw
four in ten of the corpus. That is the regression this pins.

Also pinned: the gate applies to the export exactly as it applies to every
count, and the columns keep their names (a renamed column breaks a script
nobody here will ever see).

No pytest. Run either way:
    py -3.11 -m tests.test_attrs_export
    py -3.11 tests/test_attrs_export.py
"""
from __future__ import annotations

import io
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


ATTR_COLS = ["attr_is_cow", "attr_cow_p", "attr_sun", "attr_sun_p",
             "attr_behaviour", "attr_behaviour_p", "attr_model_rev"]


def _seed(con) -> None:
    frames = [{"dataset_id": "d1", "camera_id": "cam1", "frame_idx": 0,
               "frame_path": "/f/0.jpg", "processed": True,
               "ts": pd.Timestamp("2025-07-03 10:00")}]
    db.insert_frames(con, pd.DataFrame(frames))
    base = dict(dataset_id="d1", camera_id="cam1", frame_path="/f/0.jpg", score=0.9,
                area_px=100.0, ts=pd.Timestamp("2025-07-03 10:00"), time_bin=0,
                posture="standing")
    rows = [
        # confident on both heads
        dict(base, bbox_x1=10.0, bbox_y1=10.0, bbox_x2=60.0, bbox_y2=50.0,
             attr_is_cow=True, attr_cow_p=0.99,
             attr_sun="sun_exposure.shaded", attr_sun_p=0.91,
             attr_behaviour="behaviour.feeding", attr_behaviour_p=0.83,
             attr_model_rev="e27d6d4d6638"),
        # looked at, declined the posture; the sun head still answered
        dict(base, bbox_x1=100.0, bbox_y1=10.0, bbox_x2=150.0, bbox_y2=50.0,
             attr_is_cow=True, attr_cow_p=0.95,
             attr_sun="sun_exposure.not_visible", attr_sun_p=0.77,
             attr_behaviour=None, attr_behaviour_p=0.44,
             attr_model_rev="e27d6d4d6638"),
        # never scored
        dict(base, bbox_x1=200.0, bbox_y1=10.0, bbox_x2=230.0, bbox_y2=40.0),
        # scored and rejected by the gate
        dict(base, bbox_x1=300.0, bbox_y1=10.0, bbox_x2=330.0, bbox_y2=40.0,
             attr_is_cow=False, attr_cow_p=0.08,
             attr_sun="sun_exposure.direct_sun", attr_sun_p=0.6,
             attr_behaviour="behaviour.standing", attr_behaviour_p=0.7,
             attr_model_rev="e27d6d4d6638"),
    ]
    db.insert_detections(con, pd.DataFrame(rows))


def _csv(con) -> pd.DataFrame:
    """Round-trip through actual CSV text, not the DataFrame: the question is
    what a reader of the FILE can tell, and to_csv is where a NULL becomes an
    empty cell."""
    text = db.export_df(con).to_csv(index=False)
    return pd.read_csv(io.StringIO(text))


def test_every_attribute_column_reaches_the_file():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con)
            _seed(con)
            out = _csv(con)
            missing = [c for c in ATTR_COLS if c not in out.columns]
            check("every attr_* column is in the CSV header", not missing, str(missing))
            check("so is the identity a reader joins on",
                  {"detection_id", "dataset_id", "camera_id", "frame_idx"} <= set(out.columns))
        finally:
            con.close()


def test_the_three_states_are_distinguishable_from_the_file_alone():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con)
            _seed(con)
            by = {r.bbox_x1: r for r in _csv(con).itertuples()}

            confident = by[10.0]
            check("a confident row carries its class",
                  confident.attr_behaviour == "behaviour.feeding", str(confident.attr_behaviour))
            check("...and its probability", abs(confident.attr_behaviour_p - 0.83) < 1e-9)
            check("...and the sun head's answer",
                  confident.attr_sun == "sun_exposure.shaded")

            unsure = by[100.0]
            check("an unsure row has an EMPTY class cell",
                  pd.isna(unsure.attr_behaviour), str(unsure.attr_behaviour))
            check("...but keeps the probability it would have shown",
                  abs(unsure.attr_behaviour_p - 0.44) < 1e-9, str(unsure.attr_behaviour_p))
            check("...and the model rev, which is what makes it UNSURE and not UNSCORED",
                  unsure.attr_model_rev == "e27d6d4d6638", str(unsure.attr_model_rev))
            check("...and its other head is unaffected by the abstention",
                  unsure.attr_sun == "sun_exposure.not_visible")

            unscored = by[200.0]
            check("an unscored row has no rev",
                  pd.isna(unscored.attr_model_rev), str(unscored.attr_model_rev))
            check("...and no class and no probability on either head",
                  pd.isna(unscored.attr_behaviour) and pd.isna(unscored.attr_behaviour_p)
                  and pd.isna(unscored.attr_sun) and pd.isna(unscored.attr_cow_p),
                  str(unscored))

            check("the rev distinguishes the two empty-class rows",
                  pd.isna(unsure.attr_behaviour) and pd.isna(unscored.attr_behaviour)
                  and (not pd.isna(unsure.attr_model_rev)) and pd.isna(unscored.attr_model_rev))
        finally:
            con.close()


def test_a_gate_rejected_row_is_marked_and_then_removed_when_the_gate_is_on():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        try:
            db.init_db(con)
            _seed(con)
            out = _csv(con)
            check("with the gate off every detection is exported", len(out) == 4, str(len(out)))
            rejected = {r.bbox_x1: r for r in out.itertuples()}[300.0]
            check("...and the rejected one is exported MARKED, not silently dropped",
                  rejected.attr_is_cow in (False, "False", 0), str(rejected.attr_is_cow))
            check("...with the probability behind that call",
                  abs(rejected.attr_cow_p - 0.08) < 1e-9)

            db.set_gate(True)
            try:
                gated = _csv(con)
                check("with the gate on it is gone from the file",
                      len(gated) == 3 and 300.0 not in set(gated["bbox_x1"]), str(len(gated)))
                check("...and nothing else moved",
                      set(gated["bbox_x1"]) == {10.0, 100.0, 200.0}, str(set(gated["bbox_x1"])))
            finally:
                db.set_gate(False)
        finally:
            con.close()


def main():
    print("=== test_attrs_export ===")
    test_every_attribute_column_reaches_the_file()
    test_the_three_states_are_distinguishable_from_the_file_alone()
    test_a_gate_rejected_row_is_marked_and_then_removed_when_the_gate_is_on()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
