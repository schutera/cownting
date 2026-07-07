"""API-contract smoke tests: every JSON endpoint answers 200 with the expected
shape, driven by a hermetic synthetic DuckDB (no real data, no image files).

This is the layer that would have caught the "whole day" regression: it asserts
`GET /api/area-counts` with NO frame returns non-empty counts when the DB has
data — even though the latest frame is empty.

No pytest. Run either way:
    .venv/bin/python -m tests.test_api
    .venv/bin/python tests/test_api.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from cownting import db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import CameraCfg, Config, PathsCfg  # noqa: E402

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


# frames 0/1/2 for one camera; frame 2 (latest) empty; peak occupancy is 3 in
# frame 1. Mirrors tests/test_whole_day.py so the endpoint sees the bug's shape.
def _seed(con) -> None:
    db.insert_frames(con, pd.DataFrame([
        {"camera_id": "camera_01", "frame_idx": 0, "ts": datetime(2026, 7, 3, 6, 0), "frame_path": "f0"},
        {"camera_id": "camera_01", "frame_idx": 1, "ts": datetime(2026, 7, 3, 6, 1), "frame_path": "f1"},
        {"camera_id": "camera_01", "frame_idx": 2, "ts": datetime(2026, 7, 3, 20, 0), "frame_path": "f2"},
    ]))
    rid = "camera_01::pen"
    rows = []
    for _ in range(2):  # frame 0 -> 2 cows
        rows.append({"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 6, 0),
                     "frame_path": "f0", "region_id": rid, "posture": "standing", "under_panel": False})
    for posture in ("standing", "standing", "lying"):  # frame 1 -> 3 cows (peak)
        rows.append({"camera_id": "camera_01", "ts": datetime(2026, 7, 3, 6, 1),
                     "frame_path": "f1", "region_id": rid, "posture": posture, "under_panel": False})
    db.insert_detections(con, pd.DataFrame(rows))
    # frame 2 (latest): no detections


def _client(d: str) -> TestClient:
    dbp = os.path.join(d, "cownting.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    _seed(con)
    con.close()
    config = Config(
        cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
        paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json")),
    )
    return TestClient(create_app(config))


def test_endpoints_answer_and_shape():
    with tempfile.TemporaryDirectory() as d:
        client = _client(d)

        r = client.get("/api/timeline")
        check("GET /api/timeline -> 200", r.status_code == 200, str(r.status_code))
        tl = r.json() if r.status_code == 200 else {}
        check("timeline: 3 frames", tl.get("frames") == [0, 1, 2], str(tl.get("frames")))

        # THE regression guard: no frame -> whole-day aggregate, not the empty latest frame.
        r = client.get("/api/area-counts")
        check("GET /api/area-counts (no frame) -> 200", r.status_code == 200, str(r.status_code))
        ac = r.json() if r.status_code == 200 else {}
        check("area-counts(no frame): frame is None", ac.get("frame") is None, str(ac.get("frame")))
        check("area-counts(no frame): NON-empty despite empty latest frame",
              ac.get("counts") == {"camera_01::pen": 3}, str(ac.get("counts")))
        check("area-counts(no frame): carries sheltering + postures keys",
              "sheltering" in ac and "postures" in ac, str(list(ac)))

        # Single-frame path still works and is frame-specific.
        r = client.get("/api/area-counts?frame=0")
        check("GET /api/area-counts?frame=0 -> 200", r.status_code == 200, str(r.status_code))
        ac0 = r.json() if r.status_code == 200 else {}
        check("area-counts(frame=0): counts that one frame (2)",
              ac0.get("counts") == {"camera_01::pen": 2}, str(ac0.get("counts")))

        r = client.get("/api/day-series")
        check("GET /api/day-series -> 200", r.status_code == 200, str(r.status_code))
        ds = r.json() if r.status_code == 200 else {}
        check("day-series: frames present", ds.get("frames") == [0, 1, 2], str(ds.get("frames")))

        r = client.get("/api/area-summary")
        check("GET /api/area-summary -> 200", r.status_code == 200, str(r.status_code))
        check("area-summary: returns a list", isinstance(r.json(), list), str(type(r.json())))


def main():
    print("=== test_api ===")
    test_endpoints_answer_and_shape()
    print("================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
