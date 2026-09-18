"""The two surfaces the dashboard reads the model through: the KPI rail's
attribute block and the per-frame detections route.

What must hold: `unsure` (looked, declined) is never folded into `unscored`
(never looked) or into a class; the KPI counts apply the same gate predicate as
every other count; the frame route returns full-frame boxes with the verdicts
and respects the gate; and an unscored day reports scored=0 rather than a row of
zeros that looks like a model saying "no cows do anything".

No pytest. Run either way:
    py -3.11 -m tests.test_attrs_surface
    py -3.11 tests/test_attrs_surface.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from cownting import db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402

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


def _seed(con) -> None:
    frames = [{"dataset_id": "d1", "camera_id": "cam1", "frame_idx": i, "frame_path": f"/f/{i}.jpg",
               "processed": True, "ts": pd.Timestamp("2025-07-03 10:00") + pd.Timedelta(minutes=i)}
              for i in range(2)]
    db.insert_frames(con, pd.DataFrame(frames))
    base = dict(dataset_id="d1", camera_id="cam1", score=0.9, area_px=100.0,
                ts=pd.Timestamp("2025-07-03 10:00"), time_bin=0, posture="standing")
    rows = [
        # frame 0: feeding (confident), unsure (looked, declined), rejected by the gate
        dict(base, frame_path="/f/0.jpg", bbox_x1=10.0, bbox_y1=10.0, bbox_x2=60.0, bbox_y2=50.0,
             attr_is_cow=True, attr_cow_p=0.99, attr_sun="sun_exposure.shaded", attr_sun_p=0.9,
             attr_behaviour="behaviour.feeding", attr_behaviour_p=0.8, attr_model_rev="r1",
             mask_poly="[[10,10],[60,10],[60,50],[10,50]]", mask_parts=1),
        dict(base, frame_path="/f/0.jpg", bbox_x1=100.0, bbox_y1=10.0, bbox_x2=150.0, bbox_y2=50.0,
             attr_is_cow=True, attr_cow_p=0.95, attr_sun="sun_exposure.direct_sun", attr_sun_p=0.7,
             attr_behaviour=None, attr_behaviour_p=0.45, attr_model_rev="r1"),
        dict(base, frame_path="/f/0.jpg", bbox_x1=200.0, bbox_y1=10.0, bbox_x2=230.0, bbox_y2=40.0,
             attr_is_cow=False, attr_cow_p=0.1, attr_sun="sun_exposure.shaded", attr_sun_p=0.5,
             attr_behaviour="behaviour.standing", attr_behaviour_p=0.7, attr_model_rev="r1"),
        # frame 1: never scored
        dict(base, frame_path="/f/1.jpg", bbox_x1=10.0, bbox_y1=10.0, bbox_x2=60.0, bbox_y2=50.0),
    ]
    db.insert_detections(con, pd.DataFrame(rows))
    db.upsert_dataset(con, "d1", date(2025, 7, 3), "Jul 03", status="localized")


def _client(d: str, *, gate: bool) -> TestClient:
    dbp = os.path.join(d, "c.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    _seed(con)
    con.close()
    cfg = Config(cameras=[CameraCfg(id="cam1", video="x.mp4")], auth=AuthCfg(enabled=False),
                 paths=PathsCfg(db_path=dbp, labels_db_path=os.path.join(d, "l.duckdb"),
                                count_areas=os.path.join(d, "a.json"),
                                backups_dir=os.path.join(d, "b")))
    cfg.instance_attrs.gate_enabled = gate
    return TestClient(create_app(cfg))


def test_kpi_attrs_keep_unsure_apart_and_apply_the_gate():
    with tempfile.TemporaryDirectory() as d:
        c = _client(d, gate=False)
        k = c.get("/api/site").json()["kpis"]["attrs"]
        check("scored counts rows the model looked at", k["scored"] == 3, str(k))
        check("unsure is its own bucket", k["behaviour"]["unsure"] == 1, str(k["behaviour"]))
        check("...not folded into a class",
              k["behaviour"]["feeding"] == 1 and k["behaviour"]["standing"] == 1, str(k["behaviour"]))
        check("sun counts come through", k["sun"]["shaded"] == 2 and k["sun"]["direct_sun"] == 1, str(k["sun"]))
        db.set_gate(False)
    with tempfile.TemporaryDirectory() as d:
        c = _client(d, gate=True)
        try:
            k = c.get("/api/site").json()["kpis"]["attrs"]
            check("with the gate on, the rejected detection leaves every attr count",
                  k["scored"] == 2 and k["behaviour"]["standing"] == 0 and k["sun"]["shaded"] == 1,
                  str(k))
        finally:
            db.set_gate(False)


def test_frame_detections_route():
    with tempfile.TemporaryDirectory() as d:
        c = _client(d, gate=False)
        r = c.get("/api/frame-detections?camera=cam1&frame_idx=0&dataset=d1")
        check("the route answers", r.status_code == 200, str(r.status_code))
        rows = r.json()
        check("every detection on the frame, gate off", len(rows) == 3, str(len(rows)))
        by = {r["bbox_x1"]: r for r in rows}
        check("boxes and verdicts ride along",
              by[10.0]["attr_behaviour"] == "behaviour.feeding" and by[10.0]["attr_sun"] == "sun_exposure.shaded")
        check("an unsure row has a NULL class but a rev and a probability",
              by[100.0]["attr_behaviour"] is None and by[100.0]["attr_model_rev"] == "r1"
              and by[100.0]["attr_behaviour_p"] == 0.45, str(by[100.0]))
        check("a gate-rejected row is returned with attr_is_cow=false so the viewer can mark it",
              by[200.0]["attr_is_cow"] is False)
        check("a traced row carries its outline as a parsed polygon, for hover hit-testing",
              by[10.0]["outline"] == [[10, 10], [60, 10], [60, 50], [10, 50]], str(by[10.0].get("outline")))
        check("an untraced row says so with null, so the client falls back to the box",
              by[100.0]["outline"] is None)
        r1 = c.get("/api/frame-detections?camera=cam1&frame_idx=1&dataset=d1").json()
        check("an unscored frame's row has no rev", len(r1) == 1 and r1[0]["attr_model_rev"] is None)
        db.set_gate(False)
    with tempfile.TemporaryDirectory() as d:
        c = _client(d, gate=True)
        try:
            rows = c.get("/api/frame-detections?camera=cam1&frame_idx=0&dataset=d1").json()
            check("with the gate on, the rejected detection is not drawn at all",
                  len(rows) == 2 and all(r["attr_is_cow"] for r in rows), str(len(rows)))
        finally:
            db.set_gate(False)


def main():
    print("=== test_attrs_surface ===")
    test_kpi_attrs_keep_unsure_apart_and_apply_the_gate()
    test_frame_detections_route()
    print("==========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
