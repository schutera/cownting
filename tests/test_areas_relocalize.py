"""Editing a polygon re-assigns the cows. Every time, or the map quietly lies.

`region_id` is computed, not stored by the annotator: a detection is inside a
count area or it is not, and the answer changes the moment somebody drags a
corner. Nothing recomputes it on read — every counting query joins on the
column — so if a save does not trigger a re-localize, the dashboard keeps
reporting yesterday's polygon against today's cows and says nothing.

That failure is invisible by construction, which is why it gets a test rather
than a careful reading of `set_areas`. `pipeline.localize` is already covered
(test_dataset_area_scoping proves per-dataset scoping); what was NOT covered is
the wiring — that saving through the API actually reaches it. A refactor that
dropped `request_localize` from the handler would have passed the whole suite.

Four things, matching what an operator actually does:

  - a polygon DRAWN for the first time assigns the cows standing in it;
  - a polygon ADJUSTED re-assigns — including un-assigning cows it no longer
    covers, which is the half a naive "assign what's inside" pass forgets;
  - a polygon for a SECOND camera assigns that camera without disturbing the
    first;
  - polygons REMOVED clear the assignments rather than leaving them frozen.

Panel areas ride the same path and get the same guarantee, because
`under_panel` feeds the shelter numbers the same way.

No pytest. Run either way:
    py -3.11 -m tests.test_areas_relocalize
    py -3.11 tests/test_areas_relocalize.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
from datetime import date

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cownting import db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402

_FAILED = 0
DS = "2025-07-03"


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


# Ground points at x = 10, 50, 90 on each of two cameras. A polygon covering
# x < 60 therefore takes the first two and leaves the third outside.
_XS = [10.0, 50.0, 90.0]


def _box(x0, y0, x1, y1):
    return [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]


def _app(d: str):
    dbp = os.path.join(d, "c.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    frames, dets = [], []
    for cam in ("cam1", "cam2"):
        frames.append({"dataset_id": DS, "camera_id": cam, "frame_idx": 0,
                       "frame_path": f"/f/{cam}.jpg", "processed": True,
                       "ts": pd.Timestamp("2025-07-03 10:00")})
        for x in _XS:
            dets.append(dict(dataset_id=DS, camera_id=cam, frame_path=f"/f/{cam}.jpg",
                             ts=pd.Timestamp("2025-07-03 10:00"), time_bin=0, score=0.9,
                             bbox_x1=x, bbox_y1=10.0, bbox_x2=x + 20, bbox_y2=40.0,
                             area_px=100.0, posture="standing",
                             ground_px_x=x, ground_px_y=20.0))
    db.insert_frames(con, pd.DataFrame(frames))
    db.insert_detections(con, pd.DataFrame(dets))
    db.upsert_dataset(con, DS, date(2025, 7, 3), "Jul 03", status="localized")
    con.close()
    cfg = Config(
        cameras=[CameraCfg(id="cam1", video="a.mp4"), CameraCfg(id="cam2", video="b.mp4")],
        auth=AuthCfg(enabled=False),
        paths=PathsCfg(db_path=dbp, labels_db_path=os.path.join(d, "l.duckdb"),
                       count_areas=os.path.join(d, "count.json"),
                       panel_areas=os.path.join(d, "panel.json"),
                       artifacts_dir=os.path.join(d, "art"),
                       backups_dir=os.path.join(d, "b")),
    )
    return TestClient(create_app(cfg)), cfg


def _drain(c: TestClient, timeout: float = 20.0) -> None:
    """Wait for the background worker to finish. The save returns immediately —
    it queues, debounces ~1.5s to coalesce a burst, then runs."""
    deadline = time.time() + timeout
    time.sleep(0.2)
    while time.time() < deadline:
        st = c.get("/api/localize/status").json()
        if st.get("status") in ("idle", "done", "failed") and not st.get("busy"):
            return
        time.sleep(0.2)


def _assigned(cfg: Config) -> dict:
    """{(camera, x) -> region_id} straight from the DB, after the worker ran."""
    con = db.connect(cfg.paths.db_path)
    try:
        return {(r[0], float(r[1])): r[2] for r in con.execute(
            "SELECT camera_id, ground_px_x, region_id FROM detections "
            "ORDER BY camera_id, ground_px_x").fetchall()}
    finally:
        con.close()


def _panels(cfg: Config) -> dict:
    con = db.connect(cfg.paths.db_path)
    try:
        return {(r[0], float(r[1])): (None if r[2] is None else bool(r[2]))
                for r in con.execute(
                    "SELECT camera_id, ground_px_x, under_panel FROM detections "
                    "ORDER BY camera_id, ground_px_x").fetchall()}
    finally:
        con.close()


def _save(c: TestClient, areas: dict, kind: str = "count"):
    route = "/api/areas" if kind == "count" else "/api/panel-areas"
    r = c.post(f"{route}?dataset={DS}", json={"areas": areas})
    _drain(c)
    return r


def test_drawing_adjusting_and_removing_a_polygon_each_reassign():
    with tempfile.TemporaryDirectory() as d:
        c, cfg = _app(d)
        try:
            before = _assigned(cfg)
            check("nothing is assigned before any polygon exists",
                  set(before.values()) == {None}, str(before))

            # 1. DRAWN — covers x < 60 on cam1 only.
            r = _save(c, {"cam1": [{"id": "pen", "name": "Pen",
                                    "camera_polygon": _box(0, 0, 60, 40),
                                    "ortho_polygon": _box(0, 0, 60, 40)}]})
            check("the save is accepted", r.status_code == 200, str(r.status_code))
            check("...and reports that it queued a localize",
                  "localize" in (r.json() or {}), str(r.json())[:120])
            a = _assigned(cfg)
            check("cows inside the new polygon are assigned",
                  a[("cam1", 10.0)] == "cam1::pen" and a[("cam1", 50.0)] == "cam1::pen", str(a))
            check("...the one outside it is not", a[("cam1", 90.0)] is None, str(a))
            check("...and the untouched camera is still unassigned",
                  all(a[("cam2", x)] is None for x in _XS), str(a))

            # 2. ADJUSTED — stretched to cover every x. The cow at 90 joins.
            _save(c, {"cam1": [{"id": "pen", "name": "Pen",
                                "camera_polygon": _box(0, 0, 200, 40),
                                "ortho_polygon": _box(0, 0, 200, 40)}]})
            a = _assigned(cfg)
            check("widening the polygon picks up the cow it now covers",
                  a[("cam1", 90.0)] == "cam1::pen", str(a))

            # THE HALF THAT IS EASY TO FORGET: shrink it back and the cow must
            # be UN-assigned. A pass that only writes what is inside leaves the
            # old value behind, and the map keeps counting a cow that moved out.
            _save(c, {"cam1": [{"id": "pen", "name": "Pen",
                                "camera_polygon": _box(0, 0, 20, 40),
                                "ortho_polygon": _box(0, 0, 20, 40)}]})
            a = _assigned(cfg)
            check("shrinking it un-assigns the cows it no longer covers",
                  a[("cam1", 50.0)] is None and a[("cam1", 90.0)] is None, str(a))
            check("...while keeping the one still inside", a[("cam1", 10.0)] == "cam1::pen", str(a))

            # 3. A SECOND CAMERA gets its first polygon.
            _save(c, {
                "cam1": [{"id": "pen", "name": "Pen", "camera_polygon": _box(0, 0, 20, 40),
                          "ortho_polygon": _box(0, 0, 20, 40)}],
                "cam2": [{"id": "yard", "name": "Yard", "camera_polygon": _box(0, 0, 60, 40),
                          "ortho_polygon": _box(0, 0, 60, 40)}],
            })
            a = _assigned(cfg)
            check("a newly added camera's cows are assigned",
                  a[("cam2", 10.0)] == "cam2::yard" and a[("cam2", 50.0)] == "cam2::yard", str(a))
            check("...without disturbing the first camera",
                  a[("cam1", 10.0)] == "cam1::pen" and a[("cam1", 50.0)] is None, str(a))

            # 4. REMOVED entirely — assignments must clear, not freeze.
            _save(c, {})
            a = _assigned(cfg)
            check("removing every polygon clears every assignment",
                  set(a.values()) == {None}, str(a))
        finally:
            _drain(c)


def test_panel_areas_get_the_same_guarantee():
    """`under_panel` drives the shelter numbers exactly as region_id drives the
    counts, so it needs the same re-run on edit."""
    with tempfile.TemporaryDirectory() as d:
        c, cfg = _app(d)
        try:
            _save(c, {"cam1": [{"id": "roof", "name": "Roof",
                                "camera_polygon": _box(0, 0, 60, 40),
                                "ortho_polygon": _box(0, 0, 60, 40)}]}, kind="panel")
            p = _panels(cfg)
            check("cows under a newly drawn panel area are flagged",
                  p[("cam1", 10.0)] is True and p[("cam1", 50.0)] is True, str(p))
            check("...and the one outside is not flagged",
                  p[("cam1", 90.0)] in (False, None), str(p))

            _save(c, {"cam1": [{"id": "roof", "name": "Roof",
                                "camera_polygon": _box(0, 0, 20, 40),
                                "ortho_polygon": _box(0, 0, 20, 40)}]}, kind="panel")
            p = _panels(cfg)
            check("shrinking the panel un-flags the cows it no longer covers",
                  p[("cam1", 50.0)] in (False, None), str(p))
            check("...while keeping the one still under it", p[("cam1", 10.0)] is True, str(p))
        finally:
            _drain(c)


def main():
    print("=== test_areas_relocalize ===")
    test_drawing_adjusting_and_removing_a_polygon_each_reassign()
    test_panel_areas_get_the_same_guarantee()
    print("\n" + ("PASS" if not _FAILED else f"{_FAILED} FAILED"))
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
