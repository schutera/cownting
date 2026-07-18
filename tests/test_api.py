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
from datetime import date, datetime

import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402

from cownting import db  # noqa: E402
from cownting.api import create_app  # noqa: E402
from cownting.config import AuthCfg, CameraCfg, Config, PathsCfg  # noqa: E402

# These contract tests hit /api/* directly, so run the app with the login gate
# off (auth itself is covered in tests/test_auth.py).
_NO_AUTH = AuthCfg(enabled=False)

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
        auth=_NO_AUTH,
        paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json")),
    )
    return TestClient(create_app(config))


def test_endpoints_answer_and_shape():
    with tempfile.TemporaryDirectory() as d:
        client = _client(d)

        r = client.get("/api/timeline")
        check("GET /api/timeline -> 200", r.status_code == 200, str(r.status_code))
        tl = r.json() if r.status_code == 200 else {}
        # Axis is now timestamp *instant* buckets (cameras linked by ts, not
        # frame_idx): 3 distinct capture times -> 3 instants, with wall-clock labels.
        instants = tl.get("frames", [])
        check("timeline: 3 instants", len(instants) == 3, str(instants))
        check("timeline: carries wall-clock times",
              tl.get("times") == ["2026-07-03T06:00:00", "2026-07-03T06:01:00", "2026-07-03T20:00:00"],
              str(tl.get("times")))

        # THE regression guard: no frame -> whole-day aggregate, not the empty latest frame.
        r = client.get("/api/area-counts")
        check("GET /api/area-counts (no frame) -> 200", r.status_code == 200, str(r.status_code))
        ac = r.json() if r.status_code == 200 else {}
        check("area-counts(no frame): frame is None", ac.get("frame") is None, str(ac.get("frame")))
        check("area-counts(no frame): NON-empty despite empty latest frame",
              ac.get("counts") == {"camera_01::pen": 3}, str(ac.get("counts")))
        check("area-counts(no frame): carries sheltering + postures keys",
              "sheltering" in ac and "postures" in ac, str(list(ac)))

        # Single-instant path still works and is instant-specific: the first
        # instant (06:00) has 2 cows, the second (06:01) has 3.
        first = instants[0] if instants else 0
        r = client.get(f"/api/area-counts?frame={first}")
        check("GET /api/area-counts?frame=<instant> -> 200", r.status_code == 200, str(r.status_code))
        ac0 = r.json() if r.status_code == 200 else {}
        check("area-counts(first instant): counts that instant (2)",
              ac0.get("counts") == {"camera_01::pen": 2}, str(ac0.get("counts")))

        # frame-map resolves the per-camera frame_idx for that instant.
        fm = client.get(f"/api/frame-map?frame={first}").json()
        check("frame-map(first instant): camera_01 -> frame_idx 0",
              fm == {"camera_01": 0}, str(fm))

        r = client.get("/api/day-series")
        check("GET /api/day-series -> 200", r.status_code == 200, str(r.status_code))
        ds = r.json() if r.status_code == 200 else {}
        check("day-series: 3 instants present", len(ds.get("frames", [])) == 3, str(ds.get("frames")))
        check("day-series: totals per instant", ds.get("total") == [2, 3, 0], str(ds.get("total")))

        r = client.get("/api/area-summary")
        check("GET /api/area-summary -> 200", r.status_code == 200, str(r.status_code))
        check("area-summary: returns a list", isinstance(r.json(), list), str(type(r.json())))

        # Whole-DB CSV export: header + one row per detection (2 in f0 + 3 in f1 = 5),
        # frame_idx joined in, no fan-out from the frames join.
        r = client.get("/api/export.csv")
        check("GET /api/export.csv -> 200", r.status_code == 200, str(r.status_code))
        check("export.csv: text/csv content-type",
              r.headers.get("content-type", "").startswith("text/csv"),
              r.headers.get("content-type", ""))
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        check("export.csv: header + 5 detection rows", len(lines) == 6, f"{len(lines)} lines")
        header = lines[0].split(",") if lines else []
        check("export.csv: header carries frame_idx + posture + region_id",
              {"frame_idx", "posture", "region_id", "detection_id"}.issubset(set(header)),
              str(header))


def _client_with_dataset(d: str) -> TestClient:
    """Same seed, but stamped into one data-package + a datasets dim row, so the
    dataset dimension (picker + ?dataset= filter) is exercised."""
    dbp = os.path.join(d, "cownting.duckdb")
    con = db.connect(dbp)
    db.init_db(con)
    _seed(con)
    con.execute("UPDATE frames SET dataset_id = '2026-07-03'")
    con.execute("UPDATE detections SET dataset_id = '2026-07-03'")
    db.upsert_dataset(con, "2026-07-03", date(2026, 7, 3), "Jul 03, 2026", status="localized")
    con.close()
    config = Config(
        cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
        auth=_NO_AUTH,
        paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json")),
    )
    return TestClient(create_app(config))


def test_dataset_dimension():
    with tempfile.TemporaryDirectory() as d:
        client = _client_with_dataset(d)

        r = client.get("/api/datasets")
        check("GET /api/datasets -> 200", r.status_code == 200, str(r.status_code))
        rows = r.json() if r.status_code == 200 else []
        check("datasets: one package", len(rows) == 1 and rows[0]["dataset_id"] == "2026-07-03", str(rows))
        check("datasets: live counts (3 frames / 5 dets)",
              bool(rows) and rows[0]["n_frames"] == 3 and rows[0]["n_detections"] == 5,
              str(rows[:1]))

        # site echoes the resolved (latest) dataset.
        r = client.get("/api/site")
        st = r.json() if r.status_code == 200 else {}
        check("site: resolves to latest dataset", st.get("dataset") == "2026-07-03", str(st.get("dataset")))

        # ?dataset= scoping equals whole-day here (single package): peak 3.
        r = client.get("/api/area-counts?dataset=2026-07-03")
        ac = r.json() if r.status_code == 200 else {}
        check("area-counts?dataset: peak 3", ac.get("counts") == {"camera_01::pen": 3}, str(ac.get("counts")))

        # A non-existent dataset filters everything out (not a fallback to whole-DB).
        r = client.get("/api/area-counts?dataset=nope")
        acn = r.json() if r.status_code == 200 else {}
        check("area-counts?dataset=nope: empty", acn.get("counts") == {}, str(acn.get("counts")))

        # Export scoped to the package: header + 5 detection rows.
        r = client.get("/api/export.csv?dataset=2026-07-03")
        lines = [ln for ln in r.text.splitlines() if ln.strip()]
        check("export.csv?dataset: header + 5 rows", len(lines) == 6, f"{len(lines)} lines")


def test_delete_dataset():
    """Deleting a day archives it (moves rows to the archive DB) and is gated on
    typing the capture date as ddmmyy."""
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "cownting.duckdb")
        archive = os.path.join(d, "archive.duckdb")
        con = db.connect(dbp)
        db.init_db(con)
        _seed(con)
        con.execute("UPDATE frames SET dataset_id = '2026-07-03'")
        con.execute("UPDATE detections SET dataset_id = '2026-07-03'")
        db.upsert_dataset(con, "2026-07-03", date(2026, 7, 3), "Jul 03, 2026", status="localized")
        con.close()
        config = Config(
            cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
            auth=_NO_AUTH,
            paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json"),
                           archive_db_path=archive),
        )
        client = TestClient(create_app(config))

        # Wrong confirmation phrase -> 400, nothing deleted.
        r = client.delete("/api/datasets/2026-07-03?confirm=010101")
        check("delete wrong confirm -> 400", r.status_code == 400, str(r.status_code))
        check("delete wrong confirm: day still present",
              len(client.get("/api/datasets").json()) == 1, "")

        # Unknown id -> 404.
        r = client.delete("/api/datasets/nope?confirm=030726")
        check("delete unknown id -> 404", r.status_code == 404, str(r.status_code))

        # Correct ddmmyy (2026-07-03 -> 030726) -> 200, moves 5 detections.
        r = client.delete("/api/datasets/2026-07-03?confirm=030726")
        check("delete correct confirm -> 200", r.status_code == 200, str(r.status_code))
        check("delete: reports 5 detections archived",
              r.json().get("detections_archived") == 5, str(r.json()))

        # Day is gone from the live DB / picker.
        check("delete: day list now empty", client.get("/api/datasets").json() == [], "")
        live = db.connect(dbp)
        check("delete: live detections cleared",
              live.execute("SELECT count(*) FROM detections").fetchone()[0] == 0, "")
        live.close()

        # ...but preserved in the archive DB (dim row + all 5 detections).
        arc = db.connect(archive)
        n_ds = arc.execute("SELECT count(*) FROM datasets WHERE dataset_id='2026-07-03'").fetchone()[0]
        n_det = arc.execute("SELECT count(*) FROM detections WHERE dataset_id='2026-07-03'").fetchone()[0]
        arc.close()
        check("delete: archive keeps the dimension row", n_ds == 1, str(n_ds))
        check("delete: archive keeps all 5 detections", n_det == 5, str(n_det))


def test_crosstab():
    with tempfile.TemporaryDirectory() as d:
        client = _client(d)  # 5 detections: 4 standing + 1 lying, all 'open', region camera_01::pen

        # 1-D distribution of posture.
        r = client.get("/api/crosstab?primary=posture")
        check("GET /api/crosstab?primary=posture -> 200", r.status_code == 200, str(r.status_code))
        ct = r.json() if r.status_code == 200 else {}
        check("crosstab posture totals: 4 standing / 1 lying",
              ct.get("primary_totals") == {"standing": 4, "lying": 1}, str(ct.get("primary_totals")))
        check("crosstab posture domain in fixed order, present only",
              ct.get("primary_domain") == ["standing", "lying"], str(ct.get("primary_domain")))

        # 2-D pivot: posture x under-panel (the user's literal ask).
        r = client.get("/api/crosstab?primary=posture&breakdown=panel")
        cx = r.json() if r.status_code == 200 else {}
        cells = {(c["primary"], c["breakdown"]): c["n"] for c in cx.get("cells", [])}
        check("crosstab posture x panel: standing/open=4, lying/open=1",
              cells == {("standing", "open"): 4, ("lying", "open"): 1}, str(cells))
        check("crosstab breakdown_domain = [open]", cx.get("breakdown_domain") == ["open"],
              str(cx.get("breakdown_domain")))

        # Swap primary<->breakdown.
        r = client.get("/api/crosstab?primary=panel&breakdown=posture")
        sw = r.json() if r.status_code == 200 else {}
        swc = {(c["primary"], c["breakdown"]): c["n"] for c in sw.get("cells", [])}
        check("crosstab swap panel x posture", swc == {("open", "standing"): 4, ("open", "lying"): 1}, str(swc))

        # Unknown feature -> 400 (injection guard: key must be in the registry).
        r = client.get("/api/crosstab?primary=DROP")
        check("crosstab unknown feature -> 400", r.status_code == 400, str(r.status_code))

        # Feature availability: posture populated, shade (in_shade) not yet.
        r = client.get("/api/features")
        feats = {f["key"]: f["available"] for f in (r.json() if r.status_code == 200 else [])}
        check("features: posture available", feats.get("posture") is True, str(feats.get("posture")))
        check("features: shade unavailable (in_shade NULL)", feats.get("shade") is False, str(feats.get("shade")))


def test_panel_null_is_open():
    # A cow on a camera with NO panel areas has under_panel = NULL. It must count
    # as 'open' in the panel dimension, never as a separate 'unknown' bucket.
    with tempfile.TemporaryDirectory() as d:
        dbp = os.path.join(d, "cownting.duckdb")
        con = db.connect(dbp)
        db.init_db(con)
        db.insert_frames(con, pd.DataFrame([
            {"camera_id": "camera_09", "frame_idx": 0, "ts": datetime(2026, 7, 3, 6, 0), "frame_path": "g0"},
        ]))
        db.insert_detections(con, pd.DataFrame([
            {"camera_id": "camera_09", "ts": datetime(2026, 7, 3, 6, 0), "frame_path": "g0",
             "region_id": "camera_09::pen", "posture": "standing", "under_panel": None},
            {"camera_id": "camera_09", "ts": datetime(2026, 7, 3, 6, 0), "frame_path": "g0",
             "region_id": "camera_09::pen", "posture": "lying", "under_panel": True},
        ]))
        con.close()
        config = Config(
            cameras=[CameraCfg(id="camera_09", video="unused.mp4")],
            auth=_NO_AUTH,
            paths=PathsCfg(db_path=dbp, count_areas=os.path.join(d, "areas.json")),
        )
        client = TestClient(create_app(config))

        r = client.get("/api/crosstab?primary=panel")
        cx = r.json() if r.status_code == 200 else {}
        totals = cx.get("primary_totals", {})
        check("panel crosstab: no 'unknown' bucket", "unknown" not in totals, str(totals))
        check("panel crosstab: NULL under_panel -> open=1",
              totals.get("open") == 1 and totals.get("under panel") == 1, str(totals))

        # day-series 'open' folds the NULL-under_panel cow in (open + sheltering = total).
        ds = client.get("/api/day-series").json()
        check("day-series: open counts the NULL-panel cow",
              sum(ds.get("open", [])) == 1 and sum(ds.get("sheltering", [])) == 1,
              f"open={ds.get('open')} sheltering={ds.get('sheltering')}")


def main():
    print("=== test_api ===")
    test_endpoints_answer_and_shape()
    test_dataset_dimension()
    test_delete_dataset()
    test_crosstab()
    test_panel_null_is_open()
    print("================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
