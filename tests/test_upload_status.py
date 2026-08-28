"""A day becomes visible at UPLOAD time, not at processing time.

No pytest. Run either way:
    python -m tests.test_upload_status
    python tests/test_upload_status.py

The upload endpoint returns 202 as soon as the videos are on disk and lets a
background thread do the model work. That only helps the user if the day is
actually LISTABLE during that window — otherwise the dashboard is asked for a
dataset that has no row and no frames, and sits on a loading skeleton forever.

Covers the `datasets.status` maturity ladder that makes it listable:

  * uploads.register_dataset writes the row as 'uploaded' with its day + label,
    before any frame or detection exists.
  * db.datasets() surfaces it immediately, with zeroed counts — what the day
    cards and the day picker read.
  * pipeline.ingest promotes 'uploaded' -> 'ingested' and keeps the day/label
    that registration set.
  * Re-uploading a day already processed resets it to 'uploaded' (the card must
    not keep claiming 'localized' while the replacement is being worked on).
  * Registration is best-effort: an unwritable db path must not raise, because
    the footage is already safely on disk by then.

And the queue that lets several days be sent up back-to-back:

  * Work is SERIAL — one worker, FIFO. Two days never process at once, because
    pipeline.segment snapshots every NOT-processed frame and two overlapping
    passes would each write detections for the shared ones.
  * uploads.active_job_for finds an in-flight day, which is what the endpoint
    uses to refuse a second upload of the SAME day with a 409.

Builds a REAL DuckDB on a tempfile and a REAL synthetic video (cv2.VideoWriter,
mirroring tests/test_camera_lifecycle.py). Never runs YOLO — segmentation is not
called here.

Prints each check and a final PASS; sys.exit(1) on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time

import cv2
import numpy as np

# Allow `python tests/test_upload_status.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, pipeline, uploads  # noqa: E402
from cownting.config import CameraCfg, Config, DatasetCfg, IngestCfg, PathsCfg  # noqa: E402

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
        # A check that only prints is invisible to pytest: the file reports
        # green while assertions inside it fail. `python -m tests` (the
        # pre-boot gate) counts them, but nothing else does.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


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


def _row(cfg: Config, dataset_id: str):
    con = db.connect(cfg.paths.db_path)
    try:
        rows = db.datasets(con)
    finally:
        con.close()
    hit = rows[rows["dataset_id"] == dataset_id]
    return None if hit.empty else hit.iloc[0]


# --------------------------------------------------------------------------- tests
def test_registered_before_any_processing():
    """The row exists, is listable and reads 'uploaded' with no frames yet."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d)
        ds = "2026-08-23"

        check("no row before registration", _row(cfg, ds) is None)

        uploads.register_dataset(cfg, ds, ds, "Aug 23, 2026")

        row = _row(cfg, ds)
        check("row exists straight after registration", row is not None)
        if row is None:
            return
        check("status is 'uploaded'", row["status"] == uploads.UPLOADED, str(row["status"]))
        check("label was kept", row["label"] == "Aug 23, 2026", str(row["label"]))
        check("day was kept", str(row["day"])[:10] == ds, str(row["day"]))
        # The whole point: it is listable with nothing processed behind it, so the
        # frontend can show the day (and say it is not ready) instead of nothing.
        check("no frames yet", int(row["n_frames"]) == 0, str(row["n_frames"]))
        check("no detections yet", int(row["n_detections"]) == 0, str(row["n_detections"]))
        check("no cameras yet", int(row["n_cameras"]) == 0, str(row["n_cameras"]))


def test_ingest_promotes_uploaded_to_ingested():
    """The ladder climbs, and ingest does not clobber the registered day/label."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d)
        ds = "2026-08-23"
        video = os.path.join(d, "camera_01.mp4")
        _make_video(video)

        uploads.register_dataset(cfg, ds, ds, "My label")
        before = _row(cfg, ds)

        cfg.cameras = [CameraCfg(id="camera_01", video=video, start="2026-08-23T09:00:00")]
        cfg.dataset = DatasetCfg(id=ds, day=ds, label="My label")
        n = pipeline.ingest(cfg)

        after = _row(cfg, ds)
        check("registered as 'uploaded' first",
              before is not None and before["status"] == uploads.UPLOADED)
        check("ingest promoted it to 'ingested'",
              after is not None and after["status"] == "ingested",
              str(None if after is None else after["status"]))
        check("frames landed", after is not None and int(after["n_frames"]) == n and n > 0, str(n))
        check("label survived the promotion",
              after is not None and after["label"] == "My label",
              str(None if after is None else after["label"]))


def test_reupload_resets_a_finished_day():
    """A day that already read 'localized' must not keep claiming it."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d)
        ds = "2026-08-23"

        uploads.register_dataset(cfg, ds, ds, "First")
        con = db.connect(cfg.paths.db_path)
        try:
            db.set_dataset_status(con, ds, "localized")
        finally:
            con.close()
        check("day reads 'localized' after a full run", _row(cfg, ds)["status"] == "localized")

        # Same day uploaded again — the replacement is not processed yet.
        uploads.register_dataset(cfg, ds, ds, "First")
        check("re-upload resets it to 'uploaded'",
              _row(cfg, ds)["status"] == uploads.UPLOADED,
              str(_row(cfg, ds)["status"]))


def test_registration_never_raises():
    """The videos are already on disk — a bookkeeping write must not fail the upload."""
    with tempfile.TemporaryDirectory() as d:
        cfg = _config(d)
        # A db_path whose parent is a FILE, not a directory: connecting cannot work.
        wedge = os.path.join(d, "wedge")
        with open(wedge, "w") as f:
            f.write("not a directory")
        cfg.paths.db_path = os.path.join(wedge, "cownting.duckdb")
        try:
            uploads.register_dataset(cfg, "2026-08-23", "2026-08-23", "doomed")
            raised = False
        except Exception:  # noqa: BLE001 — that is exactly what must not happen
            raised = True
        check("an unwritable db path is swallowed", not raised)


def test_queue_runs_days_one_at_a_time():
    """Several enqueued jobs never overlap, and they run in the order sent."""
    started: list[int] = []
    order: list[int] = []
    overlap = []
    live = {"n": 0}
    guard = threading.Lock()
    done = threading.Event()

    def work(i: int) -> None:
        with guard:
            live["n"] += 1
            started.append(i)
            if live["n"] > 1:
                overlap.append(i)   # someone else was mid-flight when we began
        time.sleep(0.05)            # long enough for a parallel impl to collide
        with guard:
            order.append(i)
            live["n"] -= 1
        if i == 4:
            done.set()

    for i in range(5):
        uploads._enqueue(work, i)

    finished = done.wait(timeout=15)
    check("every queued job ran", finished and len(order) == 5, str(order))
    check("never two at once", not overlap, f"overlapped: {overlap}")
    check("ran in the order queued", order == [0, 1, 2, 3, 4], str(order))


def test_worker_survives_a_failing_job():
    """One bad day must not take the worker down and strand everything behind it."""
    done = threading.Event()

    def boom() -> None:
        raise RuntimeError("simulated failure inside a job")

    def after() -> None:
        done.set()

    uploads._enqueue(boom)
    uploads._enqueue(after)
    check("the job queued behind a failure still ran", done.wait(timeout=15))


def test_active_job_for_finds_an_in_flight_day():
    """The predicate the endpoint's same-day 409 is built on."""
    ds = "2026-08-24"
    job = uploads.Job(job_id="j1", dataset_id=ds, label="Aug 24")
    with uploads._LOCK:
        uploads._JOBS[job.job_id] = job
    try:
        job.status = "queued"
        check("a queued day is in flight", uploads.active_job_for(ds) is not None)
        job.status = "running"
        check("a running day is in flight", uploads.active_job_for(ds) is not None)
        check("a different day is not", uploads.active_job_for("2026-01-01") is None)
        job.status = "done"
        check("a finished day is not in flight", uploads.active_job_for(ds) is None)
        job.status = "failed"
        # A failed day must be re-uploadable, so it must NOT read as in flight.
        check("a failed day is not in flight", uploads.active_job_for(ds) is None)
    finally:
        with uploads._LOCK:
            uploads._JOBS.pop(job.job_id, None)


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_upload_status ===")
    test_registered_before_any_processing()
    test_ingest_promotes_uploaded_to_ingested()
    test_reupload_resets_a_finished_day()
    test_registration_never_raises()
    test_queue_runs_days_one_at_a_time()
    test_worker_survives_a_failing_job()
    test_active_job_for_finds_an_in_flight_day()
    print("==========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
