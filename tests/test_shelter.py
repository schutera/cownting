"""Unit tests for the shelter/panel DB layer in cownting.db.

No pytest. Run either way:
    .venv/bin/python -m tests.test_shelter
    .venv/bin/python tests/test_shelter.py

Prints each check and a final PASS; sys.exit(1) on the first failure.

Uses a real DuckDB on a tempfile path (mirrors tests/test_fence's tempfile use).
detection_id is auto-assigned from a sequence, so we never set it: we read it
back to build the update_shelter frame.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import pandas as pd

# Allow `python tests/test_shelter.py` (no package context) to find the package.
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
    """Open a real DuckDB on a tempfile path and init the schema."""
    con = db.connect(os.path.join(d, "cownting.duckdb"))
    db.init_db(con)
    return con


def _make_dets(rows: list[dict]) -> pd.DataFrame:
    """Minimal detection frame; missing DET_COLS are filled with None by insert_detections.

    We never set detection_id (auto from seq_det).
    """
    return pd.DataFrame(rows)


def _det_ids(con, camera_id: str | None = None) -> list[int]:
    """Read back detection_ids in insert order so we can target update_shelter."""
    sql = "SELECT detection_id FROM detections"
    params = []
    if camera_id is not None:
        sql += " WHERE camera_id = ?"
        params.append(camera_id)
    sql += " ORDER BY detection_id"
    return [int(r[0]) for r in con.execute(sql, params).fetchall()]


# --------------------------------------------------------------------------- 1: init_db idempotent
def test_init_db_columns_and_idempotent():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "cownting.duckdb"))
        db.init_db(con)

        # PRAGMA table_info columns: (cid, name, type, notnull, dflt_value, pk) -> name is idx 1.
        cols = {r[1] for r in con.execute("PRAGMA table_info('detections')").fetchall()}
        check("init_db: under_panel column exists", "under_panel" in cols, str(sorted(cols)))
        check("init_db: panel_id column exists", "panel_id" in cols)
        check("init_db: near_infra column exists (boundary flag)", "near_infra" in cols)

        # Idempotent: the ALTER ... ADD COLUMN IF NOT EXISTS must not raise on re-init.
        raised = False
        try:
            db.init_db(con)
        except Exception as exc:  # noqa: BLE001
            raised = True
            check("init_db: second call did not raise", False, repr(exc))
        if not raised:
            check("init_db: second call did not raise", True)

        cols2 = {r[1] for r in con.execute("PRAGMA table_info('detections')").fetchall()}
        check("init_db: columns stable after re-init", cols2 == cols)
        con.close()


# --------------------------------------------------------------------------- 2: insert + update_shelter round-trip
def test_update_shelter_round_trip():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        dets = _make_dets([
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 0, 0),
             "ground_px_x": 100.0, "ground_px_y": 200.0, "posture": "standing"},
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 5, 0),
             "ground_px_x": 110.0, "ground_px_y": 210.0, "posture": "lying"},
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 6, 0),
             "ground_px_x": 120.0, "ground_px_y": 220.0, "posture": "standing"},
        ])
        db.insert_detections(con, dets)
        ids = _det_ids(con, "camera_01")
        check("update_shelter: 3 detections inserted", len(ids) == 3, f"ids={ids}")

        # Panel areas are polygons (no band boundary flag): row0 under panel A, rest open.
        upd = pd.DataFrame({
            "detection_id": ids,
            "under_panel": [True, False, False],
            "panel_id": ["A", None, None],
        })
        db.update_shelter(con, upd)

        got = con.execute(
            "SELECT detection_id, under_panel, panel_id "
            "FROM detections ORDER BY detection_id"
        ).fetchall()
        by_id = {r[0]: (r[1], r[2]) for r in got}
        check("update_shelter: row0 under_panel=True, panel_id=A",
              by_id[ids[0]] == (True, "A"), str(by_id[ids[0]]))
        check("update_shelter: row1 open (not under_panel, no id)",
              by_id[ids[1]] == (False, None), str(by_id[ids[1]]))
        check("update_shelter: row2 open (not under_panel, no id)",
              by_id[ids[2]] == (False, None), str(by_id[ids[2]]))

        # Targeting: only the intended rows changed; empty df is a no-op.
        db.update_shelter(con, upd.iloc[0:0])
        after = con.execute(
            "SELECT count(*) FROM detections WHERE under_panel"
        ).fetchone()[0]
        check("update_shelter: empty df no-op, exactly 1 sheltering", int(after) == 1,
              f"count={after}")
        con.close()


# --------------------------------------------------------------------------- 3: kpi_summary pct_sheltering
def test_kpi_summary_pct_sheltering():
    # 3a: 2 of 4 under_panel -> 50.0
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        dets = _make_dets([
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 0, 0),
             "ground_px_x": float(i), "ground_px_y": float(i)} for i in range(4)
        ])
        db.insert_detections(con, dets)
        ids = _det_ids(con)
        upd = pd.DataFrame({
            "detection_id": ids,
            "under_panel": [True, True, False, False],
            "panel_id": ["A", "B", None, None],
        })
        db.update_shelter(con, upd)
        kpi = db.kpi_summary(con)
        check("kpi: detections == 4", kpi["detections"] == 4, str(kpi))
        check("kpi: pct_sheltering == 50.0 (2 of 4)", kpi["pct_sheltering"] == 50.0,
              str(kpi["pct_sheltering"]))
        con.close()

    # 3b: 0 detections -> 0.0 (no divide-by-zero)
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        kpi = db.kpi_summary(con)
        check("kpi: 0 detections -> detections == 0", kpi["detections"] == 0, str(kpi))
        check("kpi: 0 detections -> pct_sheltering == 0.0 (no ZeroDivision)",
              kpi["pct_sheltering"] == 0.0, str(kpi["pct_sheltering"]))
        con.close()


# --------------------------------------------------------------------------- 4: shelter_over_time buckets/agg
def test_shelter_over_time():
    with tempfile.TemporaryDirectory() as d:
        con = _fresh_db(d)
        # Two hour buckets on camera_01; a third detection on camera_02 for the
        # aggregate test. ts chosen so date_trunc('hour', ts) yields >=2 buckets.
        dets = _make_dets([
            # bucket 10:00 -- camera_01, one sheltering, one not
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 10, 0),
             "ground_px_x": 1.0, "ground_px_y": 1.0},
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 10, 40, 0),
             "ground_px_x": 2.0, "ground_px_y": 2.0},
            # bucket 11:00 -- camera_01, one sheltering
            {"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 11, 15, 0),
             "ground_px_x": 3.0, "ground_px_y": 3.0},
            # bucket 11:00 -- camera_02, one sheltering (for aggregate)
            {"camera_id": "camera_02", "ts": datetime(2026, 7, 3, 11, 30, 0),
             "ground_px_x": 4.0, "ground_px_y": 4.0},
        ])
        db.insert_detections(con, dets)
        ids_cam1 = _det_ids(con, "camera_01")
        ids_cam2 = _det_ids(con, "camera_02")
        # Sheltering: cam1 rows [0]=True, [1]=False, [2]=True ; cam2 row=True.
        upd = pd.DataFrame({
            "detection_id": ids_cam1 + ids_cam2,
            "under_panel": [True, False, True, True],
            "panel_id": ["A", None, "A", "A"],
        })
        db.update_shelter(con, upd)

        # --- per-camera: camera_01 only, 2 hour buckets ---
        df = db.shelter_over_time(con, "camera_01", "hour")
        check("shelter_over_time: has t/sheltering/detections cols",
              {"t", "sheltering", "detections"}.issubset(set(df.columns)), str(list(df.columns)))
        check("shelter_over_time(camera_01): 2 buckets", len(df) == 2, f"n={len(df)}")
        df = df.sort_values("t").reset_index(drop=True)
        check("shelter_over_time(camera_01): bucket0 detections==2, sheltering==1",
              int(df.loc[0, "detections"]) == 2 and int(df.loc[0, "sheltering"]) == 1,
              f"det={df.loc[0, 'detections']} shel={df.loc[0, 'sheltering']}")
        check("shelter_over_time(camera_01): bucket1 detections==1, sheltering==1",
              int(df.loc[1, "detections"]) == 1 and int(df.loc[1, "sheltering"]) == 1,
              f"det={df.loc[1, 'detections']} shel={df.loc[1, 'sheltering']}")

        # --- aggregate: camera_id=None spans both cameras ---
        agg = db.shelter_over_time(con, None, "hour").sort_values("t").reset_index(drop=True)
        check("shelter_over_time(None): 2 buckets across cameras", len(agg) == 2, f"n={len(agg)}")
        total_det = int(agg["detections"].sum())
        total_shel = int(agg["sheltering"].sum())
        check("shelter_over_time(None): all 4 detections counted", total_det == 4, f"det={total_det}")
        check("shelter_over_time(None): 3 sheltering counted", total_shel == 3, f"shel={total_shel}")
        # The 11:00 bucket must include camera_02 (aggregate > per-camera there).
        agg_11 = int(agg.loc[1, "detections"])
        check("shelter_over_time(None): 11:00 bucket aggregates both cameras (det==2)",
              agg_11 == 2, f"det={agg_11}")
        con.close()


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_shelter ===")
    test_init_db_columns_and_idempotent()
    test_update_shelter_round_trip()
    test_kpi_summary_pct_sheltering()
    test_shelter_over_time()
    print("====================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
