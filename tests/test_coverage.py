"""db.camera_coverage: per-camera frame coverage over the day's instant axis —
contiguous [start,end] segments (gaps split them) + an 'uneven' flag. Real DuckDB,
no mocks. Run: python -m tests.test_coverage  (or  python tests/test_coverage.py)."""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    print(f"[{'ok ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _FAILED += 1


def _frames(dataset_id: str, camera_id: str, minutes) -> list[dict]:
    return [
        dict(dataset_id=dataset_id, camera_id=camera_id, frame_idx=m,
             ts=datetime(2025, 1, 1, 0, m, 0),
             frame_path=f"/x/{camera_id}/{m:04d}.jpg", processed=True)
        for m in minutes
    ]


def main() -> None:
    print("=== test_coverage ===")
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        db.init_db(con)
        db.upsert_dataset(con, "D", date(2025, 1, 1), "D")
        rows = []
        rows += _frames("D", "camera_full", range(0, 10))      # 00:00..00:09, continuous
        rows += _frames("D", "camera_short", range(0, 3))      # 00:00..00:02, short
        rows += _frames("D", "camera_gap", [0, 1, 2, 7, 8, 9])  # two runs, gap 00:03..00:06
        db.insert_frames(con, pd.DataFrame(rows))
        cov = db.camera_coverage(con, "D", bin_seconds=60.0)
        con.close()

    cams = {c["camera_id"]: c for c in cov["cameras"]}
    check("three cameras reported", len(cams) == 3, str(sorted(cams)))
    check("camera_full: one segment", len(cams["camera_full"]["segments"]) == 1)
    check("camera_full: 10 frames", cams["camera_full"]["n_frames"] == 10)
    full = cams["camera_full"]["segments"][0]
    check("camera_full: segment spans 10 instants (end-start == 9)", full[1] - full[0] == 9, str(full))
    check("camera_short: one segment", len(cams["camera_short"]["segments"]) == 1)
    check("camera_short: 3 frames", cams["camera_short"]["n_frames"] == 3)
    check("camera_gap: two segments (gap split)", len(cams["camera_gap"]["segments"]) == 2,
          str(cams["camera_gap"]["segments"]))
    check("camera_gap: 6 frames", cams["camera_gap"]["n_frames"] == 6)
    check("uneven flagged (short covers < half of full)", cov["uneven"] is True)
    check("instant extent set", cov["max_instant"] > cov["min_instant"])
    check("ts extent present", cov["min_ts"] is not None and cov["max_ts"] is not None)
    check("first_ts/last_ts are ISO strings", isinstance(cams["camera_full"]["first_ts"], str))

    # A single-camera dataset is never 'uneven'.
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "c.duckdb"))
        db.init_db(con)
        db.upsert_dataset(con, "S", date(2025, 1, 1), "S")
        db.insert_frames(con, pd.DataFrame(_frames("S", "only", range(0, 3))))
        solo = db.camera_coverage(con, "S", bin_seconds=60.0)
        con.close()
    check("single camera never uneven", solo["uneven"] is False)

    print("============================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
