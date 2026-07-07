"""Integration tests for the count-area DB layer (cownting.db + cownting.scene.regions).

No pytest. Run either way:
    .venv/bin/python -m tests.test_count_areas
    .venv/bin/python tests/test_count_areas.py

Prints each check and a final PASS; sys.exit(1) on the first failure.

Builds a real DuckDB on a tempfile path (mirrors tests/test_shelter.py), inserts
detections with known ground_px + ts, assigns regions via regions.assign_regions,
writes them with db.update_region, then checks db.area_counts_over_time buckets.
detection_id is auto-assigned from a sequence, so we read it back.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import numpy as np
import pandas as pd

# Allow `python tests/test_count_areas.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db  # noqa: E402
from cownting.scene import regions  # noqa: E402

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


def _det_ids(con, camera_id: str | None = None) -> list[int]:
    sql = "SELECT detection_id FROM detections"
    params = []
    if camera_id is not None:
        sql += " WHERE camera_id = ?"
        params.append(camera_id)
    sql += " ORDER BY detection_id"
    return [int(r[0]) for r in con.execute(sql, params).fetchall()]


# Two non-overlapping square areas for camera_01.
#   left:  x in [0,10], y in [0,10]
#   right: x in [20,30], y in [0,10]
AREAS = {
    "camera_01": [
        {"id": "left", "name": "Left pen",
         "camera_polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
         "ortho_polygon": []},
        {"id": "right", "name": "Right pen",
         "camera_polygon": [[20.0, 0.0], [30.0, 0.0], [30.0, 10.0], [20.0, 10.0]],
         "ortho_polygon": []},
    ],
}


def _assign_and_write(con, camera_id: str):
    """Assign regions for every detection of a camera and persist via update_region."""
    rows = con.execute(
        "SELECT detection_id, ground_px_x, ground_px_y FROM detections "
        "WHERE camera_id = ? ORDER BY detection_id",
        [camera_id],
    ).fetchall()
    det_ids = [int(r[0]) for r in rows]
    ground = np.array([[float(r[1]), float(r[2])] for r in rows], dtype=float)
    region_ids = regions.assign_regions(ground, AREAS.get(camera_id, []), camera_id)
    df = pd.DataFrame({"detection_id": det_ids, "region_id": region_ids})
    db.update_region(con, df)
    return dict(zip(det_ids, region_ids))


# --------------------------------------------------------------------------- 1: update_region round-trip
def test_update_region_round_trip():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        dets = pd.DataFrame([
            # inside left area
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 0, 0),
             "ground_px_x": 5.0, "ground_px_y": 5.0},
            # inside right area
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 5, 0),
             "ground_px_x": 25.0, "ground_px_y": 5.0},
            # outside every area -> None
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 6, 0),
             "ground_px_x": 100.0, "ground_px_y": 100.0},
        ])
        db.insert_detections(con, dets)
        mapping = _assign_and_write(con, "camera_01")
        ids = _det_ids(con, "camera_01")

        check("update_region: point in left -> camera_01::left",
              mapping[ids[0]] == "camera_01::left", str(mapping))
        check("update_region: point in right -> camera_01::right",
              mapping[ids[1]] == "camera_01::right", str(mapping))
        check("update_region: outside point -> None", mapping[ids[2]] is None, str(mapping))

        got = con.execute(
            "SELECT detection_id, region_id FROM detections ORDER BY detection_id"
        ).fetchall()
        by_id = {int(r[0]): r[1] for r in got}
        check("update_region: persisted left id",
              by_id[ids[0]] == "camera_01::left", str(by_id))
        check("update_region: persisted right id",
              by_id[ids[1]] == "camera_01::right", str(by_id))
        check("update_region: persisted NULL for outside",
              by_id[ids[2]] is None, str(by_id))

        # Empty df is a no-op (does not wipe existing assignments).
        db.update_region(con, pd.DataFrame({"detection_id": [], "region_id": []}))
        still = con.execute(
            "SELECT count(*) FROM detections WHERE region_id IS NOT NULL"
        ).fetchone()[0]
        check("update_region: empty df no-op, 2 assigned remain", int(still) == 2,
              f"count={still}")
        con.close()


# --------------------------------------------------------------------------- 2: area_counts_over_time buckets
def test_area_counts_over_time():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        dets = pd.DataFrame([
            # hour bucket 10:00 -- left area x2, right area x1
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 10, 0),
             "ground_px_x": 5.0, "ground_px_y": 5.0},
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 40, 0),
             "ground_px_x": 6.0, "ground_px_y": 6.0},
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 45, 0),
             "ground_px_x": 25.0, "ground_px_y": 5.0},
            # hour bucket 11:00 -- left area x1
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 11, 15, 0),
             "ground_px_x": 4.0, "ground_px_y": 4.0},
            # outside every area -> not counted (region_id NULL)
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 11, 30, 0),
             "ground_px_x": 999.0, "ground_px_y": 999.0},
        ])
        db.insert_detections(con, dets)
        _assign_and_write(con, "camera_01")

        df = db.area_counts_over_time(con, "camera_01", "hour")
        check("area_counts_over_time: has t/region_id/cows cols",
              {"t", "region_id", "cows"}.issubset(set(df.columns)), str(list(df.columns)))

        # NULL region rows must be excluded.
        check("area_counts_over_time: NULL-region row excluded",
              df["region_id"].notna().all(), str(df))

        # Build (t-hour, region_id) -> cows lookup.
        counts = {}
        for _, r in df.iterrows():
            t = pd.Timestamp(r["t"])
            counts[(t.hour, r["region_id"])] = int(r["cows"])

        check("area_counts_over_time: 10:00 left == 2",
              counts.get((10, "camera_01::left")) == 2, str(counts))
        check("area_counts_over_time: 10:00 right == 1",
              counts.get((10, "camera_01::right")) == 1, str(counts))
        check("area_counts_over_time: 11:00 left == 1",
              counts.get((11, "camera_01::left")) == 1, str(counts))
        check("area_counts_over_time: 11:00 right absent (no rows)",
              (11, "camera_01::right") not in counts, str(counts))
        total = int(df["cows"].sum())
        check("area_counts_over_time: 4 assigned detections counted (outside excluded)",
              total == 4, f"total={total}")
        con.close()


# --------------------------------------------------------------------------- 3: camera filter vs aggregate
def test_area_counts_over_time_camera_filter():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        # camera_01 gets a left-area detection; camera_02 has areas too but we
        # register it with the same AREAS keyed only by camera_01, so add a
        # second camera whose points fall in a shared-shape area via AREAS.
        # Simpler: give camera_02 its own area entry inline.
        AREAS["camera_02"] = [
            {"id": "yard", "name": "Yard",
             "camera_polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
             "ortho_polygon": []},
        ]
        try:
            dets = pd.DataFrame([
                {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 12, 5, 0),
                 "ground_px_x": 5.0, "ground_px_y": 5.0},
                {"camera_id": "camera_02", "ts": datetime(2026, 7, 3, 12, 10, 0),
                 "ground_px_x": 5.0, "ground_px_y": 5.0},
            ])
            db.insert_detections(con, dets)
            _assign_and_write(con, "camera_01")
            _assign_and_write(con, "camera_02")

            # Per-camera filter: only camera_01 rows.
            df1 = db.area_counts_over_time(con, "camera_01", "hour")
            regs1 = set(df1["region_id"])
            check("area_counts_over_time(camera_01): only camera_01 regions",
                  regs1 == {"camera_01::left"}, str(regs1))

            # Aggregate: camera=None spans both cameras.
            agg = db.area_counts_over_time(con, None, "hour")
            regs = set(agg["region_id"])
            check("area_counts_over_time(None): spans both cameras",
                  {"camera_01::left", "camera_02::yard"}.issubset(regs), str(regs))
            check("area_counts_over_time(None): 2 detections total",
                  int(agg["cows"].sum()) == 2, str(agg))
        finally:
            del AREAS["camera_02"]
        con.close()


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_count_areas ===")
    test_update_region_round_trip()
    test_area_counts_over_time()
    test_area_counts_over_time_camera_filter()
    print("========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
