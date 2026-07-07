"""Regression test for the map's "whole day" toggle (cownting.db.area_counts_whole_day).

No pytest. Run either way:
    .venv/bin/python -m tests.test_whole_day
    .venv/bin/python tests/test_whole_day.py

Prints each check and a final PASS; sys.exit(1) on the first failure.

The bug this guards: "whole day" used to resolve to the LATEST frame, which at
dusk holds no cows, so the occupancy map blanked out. The fix aggregates over the
whole day (PEAK simultaneous occupancy per area). We build a DB whose last frame
is deliberately empty and assert the peak from an earlier frame still shows up.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import pandas as pd

# Allow `python tests/test_whole_day.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


def _fresh_db(d: str):
    con = db.connect(os.path.join(d, "cownting.duckdb"))
    db.init_db(con)
    return con


# frame_idx 0/1/2 for one camera; frame 2 (the LATEST) has NO detections. The
# peak occupancy is 3, in frame 1. Regions are written directly (region_id column)
# to keep the fixture focused on the whole-day aggregation, not region assignment.
def _seed(con) -> None:
    db.insert_frames(con, pd.DataFrame([
        {"camera_id": "camera_01", "frame_idx": 0, "ts": datetime(2026, 7, 3, 6, 0), "frame_path": "f0"},
        {"camera_id": "camera_01", "frame_idx": 1, "ts": datetime(2026, 7, 3, 6, 1), "frame_path": "f1"},
        {"camera_id": "camera_01", "frame_idx": 2, "ts": datetime(2026, 7, 3, 20, 0), "frame_path": "f2"},
    ]))
    rid = "camera_01::pen"
    rows = []
    # frame 0 -> 2 cows (both standing)
    for _ in range(2):
        rows.append({"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 6, 0),
                     "frame_path": "f0", "region_id": rid, "posture": "standing", "under_panel": False})
    # frame 1 -> 3 cows (peak): 2 standing, 1 lying; 1 of them under a panel
    rows.append({"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 6, 1),
                 "frame_path": "f1", "region_id": rid, "posture": "standing", "under_panel": False})
    rows.append({"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 6, 1),
                 "frame_path": "f1", "region_id": rid, "posture": "standing", "under_panel": False})
    rows.append({"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 6, 1),
                 "frame_path": "f1", "region_id": rid, "posture": "lying", "under_panel": True})
    # frame 2 (latest) -> deliberately empty
    db.insert_detections(con, pd.DataFrame(rows))


# --------------------------------------------------------------------------- 1: peak survives an empty last frame
def test_whole_day_peak_ignores_empty_last_frame():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        _seed(con)

        # Sanity: the latest frame really is empty (this is what used to blank the map).
        last = con.execute("SELECT max(frame_idx) FROM frames").fetchone()[0]
        n_last = con.execute(
            "SELECT count(*) FROM detections d JOIN frames f "
            "ON d.camera_id=f.camera_id AND d.frame_path=f.frame_path "
            "WHERE f.frame_idx = ? AND d.region_id IS NOT NULL", [last],
        ).fetchone()[0]
        check("latest frame is empty (reproduces the old blank-map trigger)",
              int(n_last) == 0, f"frame={last} dets={n_last}")

        df = db.area_counts_whole_day(con)
        by = {r.region_id: r for r in df.itertuples()}

        check("whole day: region present despite empty last frame",
              "camera_01::pen" in by, str(list(by)))
        row = by.get("camera_01::pen")
        check("whole day: peak == 3 (the busiest frame, not the empty latest one)",
              row is not None and int(row.peak) == 3,
              f"peak={None if row is None else row.peak}")
        check("whole day: standing cumulative == 4",
              row is not None and int(row.standing) == 4,
              f"standing={None if row is None else row.standing}")
        check("whole day: lying cumulative == 1",
              row is not None and int(row.lying) == 1,
              f"lying={None if row is None else row.lying}")
        check("whole day: unknown cumulative == 0",
              row is not None and int(row.unknown) == 0,
              f"unknown={None if row is None else row.unknown}")
        check("whole day: sheltering peak == 1",
              row is not None and int(row.sheltering) == 1,
              f"sheltering={None if row is None else row.sheltering}")
        con.close()


# --------------------------------------------------------------------------- 2: empty DB -> empty frame (no crash)
def test_whole_day_empty_db():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        df = db.area_counts_whole_day(con)
        check("whole day: empty DB -> no rows (not a crash)", df.empty, f"rows={len(df)}")
        con.close()


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_whole_day ===")
    test_whole_day_peak_ignores_empty_last_frame()
    test_whole_day_empty_db()
    print("======================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
