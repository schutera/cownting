"""Add/replace + delete building blocks for ONE camera stream in a day.

No pytest. Run either way:
    python -m tests.test_camera_lifecycle
    python tests/test_camera_lifecycle.py

Covers the pieces the per-camera management endpoints compose, WITHOUT running
YOLO (segmentation needs the model, so pipeline.segment is never called here):

  * pipeline.ingest_one_camera adds/replaces ONE camera in an existing dataset
    without disturbing the others, and is idempotent for that camera.
  * The DELETE path building blocks: db.purge_dataset(..., camera_id=cam) removes
    only that camera's rows, and dropping a camera key from the per-dataset area
    file (regions.load_count_areas / dataset_area_path / save_count_areas) drops
    only that camera — exactly what api.delete_camera composes.
  * db.unprocessed_frames(camera_id=X, dataset_id=ds) returns ONLY camera X — the
    scoping pipeline.segment relies on for the add-one-camera flow.

Builds a REAL DuckDB on a tempfile and a REAL synthetic video (cv2.VideoWriter,
mirroring tests/test_video.py). Never holds a DB connection open across an
ingest_one_camera call (it opens and closes its own short-lived connection).

Prints each check and a final PASS; sys.exit(1) on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import cv2
import numpy as np
import pandas as pd

# Allow `python tests/test_camera_lifecycle.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, pipeline  # noqa: E402
from cownting.config import CameraCfg, Config, IngestCfg, PathsCfg  # noqa: E402
from cownting.scene import regions  # noqa: E402

_FAILED = 0
_FPS = 10
_NFRAMES = 40


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


# --------------------------------------------------------------------------- fixtures
def _config(d: str) -> Config:
    data = os.path.join(d, "data")
    return Config(
        cameras=[],
        ingest=IngestCfg(target_fps=2.0, save_frames=True),
        paths=PathsCfg(
            artifacts_dir=os.path.join(data, "artifacts"),
            db_path=os.path.join(data, "cownting.duckdb"),
            count_areas=os.path.join(data, "count_areas.json"),
            panel_areas=os.path.join(data, "panel_areas.json"),
        ),
    )


def _make_video(path: str) -> None:
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), _FPS, (64, 48))
    for i in range(_NFRAMES):
        w.write(np.full((48, 64, 3), (i * 6) % 256, dtype=np.uint8))
    w.release()


def _readable_frames(path: str) -> int:
    cap = cv2.VideoCapture(path)
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


def _seed_frames(con, dataset_id: str, camera_id: str, n: int, processed: bool = True) -> None:
    """Insert n frame rows for one camera (dummy on-disk paths — never read here)."""
    first = datetime(2025, 10, 15, 6, 0, 0)
    db.insert_frames(con, pd.DataFrame([
        dict(dataset_id=dataset_id, camera_id=camera_id, frame_idx=i,
             ts=first + timedelta(minutes=i), time_bin=i,
             frame_path=f"{dataset_id}/{camera_id}/{i:08d}.jpg",
             overlay_path=None, pose_overlay_path=None, processed=processed)
        for i in range(n)
    ]))


def _seed_dets(con, dataset_id: str, camera_id: str, n: int) -> None:
    first = datetime(2025, 10, 15, 6, 0, 0)
    db.insert_detections(con, pd.DataFrame([
        dict(dataset_id=dataset_id, camera_id=camera_id, ts=first + timedelta(minutes=i))
        for i in range(n)
    ]))


def _count(con, table: str, dataset_id: str, camera_id: str) -> int:
    return con.execute(
        f"SELECT count(*) FROM {table} WHERE dataset_id = ? AND camera_id = ?",
        [dataset_id, camera_id],
    ).fetchone()[0]


def _area(area_id: str) -> dict:
    return {"id": area_id, "name": area_id.title(),
            "camera_polygon": [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]],
            "ortho_polygon": []}


# --------------------------------------------------------------------------- 1: add + replace one camera
def test_ingest_one_camera_add_and_replace():
    ds = "2025-10-15"
    keep, new = "camera_keep", "camera_new"
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        vid = os.path.join(d, "new.mp4")
        _make_video(vid)
        if _readable_frames(vid) == 0:
            # No usable video codec in this environment — skip rather than false-fail.
            print("[skip] could not synthesize a readable test video here")
            return

        # A dataset that ALREADY holds a DIFFERENT camera with frames + detections.
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, ds, date(2025, 10, 15), "Oct 15, 2025")
            _seed_frames(con, ds, keep, 8)
            _seed_dets(con, ds, keep, 5)
        finally:
            con.close()

        cam = CameraCfg(id=new, video=vid, start="2025-10-15T06:00:00")
        n1 = pipeline.ingest_one_camera(config, ds, cam)

        con = db.connect(config.paths.db_path)
        try:
            new_frames = _count(con, "frames", ds, new)
            keep_frames = _count(con, "frames", ds, keep)
            keep_dets = _count(con, "detections", ds, keep)
            ds_rows = con.execute(
                "SELECT count(*) FROM datasets WHERE dataset_id = ?", [ds]
            ).fetchone()[0]
        finally:
            con.close()

        check("ingest_one_camera landed camera_new frames",
              new_frames == n1 and n1 > 0, f"count={new_frames} returned={n1}")
        check("camera_keep frames untouched by add", keep_frames == 8, str(keep_frames))
        check("camera_keep detections untouched by add", keep_dets == 5, str(keep_dets))
        check("dataset dimension row still present", ds_rows == 1, str(ds_rows))

        # Re-ingest the SAME camera: must REPLACE (same count), not duplicate.
        n2 = pipeline.ingest_one_camera(config, ds, cam)
        con = db.connect(config.paths.db_path)
        try:
            new_frames2 = _count(con, "frames", ds, new)
            keep_frames2 = _count(con, "frames", ds, keep)
        finally:
            con.close()
        check("re-ingest replaces camera_new (same frame count, not doubled)",
              new_frames2 == n1 == n2, f"first={n1} again={n2} rows_after={new_frames2}")
        check("camera_keep still untouched after replace", keep_frames2 == 8, str(keep_frames2))


# --------------------------------------------------------------------------- 2: delete one camera building blocks
def test_delete_one_camera_building_blocks():
    ds = "2025-10-15"
    a, b = "camera_a", "camera_b"
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, ds, date(2025, 10, 15), "Oct 15, 2025")
            _seed_frames(con, ds, a, 6)
            _seed_frames(con, ds, b, 7)
            _seed_dets(con, ds, a, 4)
            _seed_dets(con, ds, b, 9)
        finally:
            con.close()

        # Per-dataset count-area file keyed by BOTH cameras (what the map editor writes).
        area_path = regions.dataset_area_path(config, ds, "count")
        regions.save_count_areas(area_path, {a: [_area("pen")], b: [_area("yard")]})

        # DELETE camera_a: purge its rows + drop its area key (what the endpoint composes).
        con = db.connect(config.paths.db_path)
        try:
            db.purge_dataset(con, ds, camera_id=a)
        finally:
            con.close()
        areas = regions.load_count_areas(area_path)
        areas.pop(a, None)
        regions.save_count_areas(area_path, areas)

        # Verify ONLY camera_a is gone, everywhere.
        con = db.connect(config.paths.db_path)
        try:
            a_frames = _count(con, "frames", ds, a)
            a_dets = _count(con, "detections", ds, a)
            b_frames = _count(con, "frames", ds, b)
            b_dets = _count(con, "detections", ds, b)
        finally:
            con.close()
        reloaded = regions.load_count_areas(area_path)

        check("purge_dataset(camera_a): camera_a frames removed", a_frames == 0, str(a_frames))
        check("purge_dataset(camera_a): camera_a detections removed", a_dets == 0, str(a_dets))
        check("camera_b frames fully intact", b_frames == 7, str(b_frames))
        check("camera_b detections fully intact", b_dets == 9, str(b_dets))
        check("area file: camera_a key dropped", a not in reloaded, str(sorted(reloaded)))
        check("area file: camera_b key intact", b in reloaded, str(sorted(reloaded)))


# --------------------------------------------------------------------------- 3: segment scoping guard
def test_segment_scoping_unprocessed_frames():
    ds = "2025-10-15"
    x, y = "camera_x", "camera_y"
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, ds, date(2025, 10, 15), "Oct 15, 2025")
            # Two cameras of UNPROCESSED frames in the same dataset.
            _seed_frames(con, ds, x, 5, processed=False)
            _seed_frames(con, ds, y, 4, processed=False)

            only_x = db.unprocessed_frames(con, camera_id=x, dataset_id=ds)
            both = db.unprocessed_frames(con, dataset_id=ds)
        finally:
            con.close()

        cams_x = set(only_x["camera_id"].tolist())
        check("unprocessed_frames(camera_x) returns ONLY camera_x", cams_x == {x}, str(cams_x))
        check("unprocessed_frames(camera_x) count matches seeded", len(only_x) == 5, str(len(only_x)))
        check("unprocessed_frames(no camera) sees both cameras",
              set(both["camera_id"].tolist()) == {x, y}, str(set(both["camera_id"].tolist())))


# --------------------------------------------------------------------------- 4: clip a stream to a time window
def test_clip_camera_window():
    ds = "2025-10-15"
    x, keep = "camera_x", "camera_keep"
    lo, hi = "2025-10-15T06:05:00", "2025-10-15T06:14:00"  # keep [06:05, 06:14] inclusive
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, ds, date(2025, 10, 15), "Oct 15, 2025")
            _seed_frames(con, ds, x, 20)     # 06:00..06:19 (one per minute)
            _seed_dets(con, ds, x, 20)
            _seed_frames(con, ds, keep, 8)   # control camera, must stay untouched
            _seed_dets(con, ds, keep, 5)

            res = db.clip_camera(con, ds, x, lo, hi)

            x_frames = _count(con, "frames", ds, x)
            x_dets = _count(con, "detections", ds, x)
            keep_frames = _count(con, "frames", ds, keep)
            keep_dets = _count(con, "detections", ds, keep)
            outside = con.execute(
                "SELECT count(*) FROM frames WHERE dataset_id=? AND camera_id=? AND (ts < ? OR ts > ?)",
                [ds, x, lo, hi],
            ).fetchone()[0]
        finally:
            con.close()

    check("clip kept the 10 frames inside the window", x_frames == 10, str(x_frames))
    check("clip removed the 10 frames outside", res["removed"] == 10, str(res["removed"]))
    check("clip reports kept == frames remaining", res["kept"] == 10 == x_frames, str(res["kept"]))
    check("clip removed out-of-window detections too", x_dets == 10, str(x_dets))
    check("clip returned an image path per removed frame", len(res["paths"]) == res["removed"],
          f"paths={len(res['paths'])} removed={res['removed']}")
    check("no frames remain outside the window", outside == 0, str(outside))
    check("other camera frames untouched by clip", keep_frames == 8, str(keep_frames))
    check("other camera detections untouched by clip", keep_dets == 5, str(keep_dets))


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_camera_lifecycle ===")
    test_ingest_one_camera_add_and_replace()
    test_delete_one_camera_building_blocks()
    test_segment_scoping_unprocessed_frames()
    test_clip_camera_window()
    print("=============================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
