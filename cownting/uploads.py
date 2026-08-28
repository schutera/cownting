"""In-app multi-camera upload -> auto-process (ingest -> segment -> localize).

Single-box MVP of the roadmap's upload epic (DU2/DU3): a POST lands one video per
camera, a background worker runs the offline batch scoped to the new day, and a
job record exposes stage/progress so the frontend can show a progress bar.

Days are processed by ONE worker thread draining a FIFO queue, not a thread per
job. Uploading is the part the user waits on, so several days can be sent up
back-to-back; the model work behind them is deliberately serial:

  * Inference is CPU-only here (yolo11x-seg). Two segmentation passes on the same
    cores finish no sooner than one after the other, they just both crawl.
  * pipeline.segment picks up work by scanning for NOT-processed frames and
    snapshots that list at the start. Two passes overlapping would each run the
    model over the shared frames and each write detections for them - silently
    doubling a day's cow count.
  * ingest purges a dataset's rows and rmtree's its artifact subtree, so two runs
    touching one day race destructively.

A queued job reports status 'queued' until the worker reaches it, which is what
the frontend shows as its place in line.

The job registry is an in-memory dict in the single serve process, so it's shared
across every request/client — any browser (a refresh, a second tab, another user)
can list the running jobs and reconnect to the progress bar, not just the tab that
started the upload. It's also mirrored to a small JSON file (throttled) so a server
restart doesn't silently strip the progress bar off an in-flight day: on boot the
snapshot is reloaded and any job that was mid-flight is marked interrupted (the
processed rows themselves are durable in DuckDB — re-upload the day to finish it).
"""
from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from . import db
from .config import CameraCfg, Config, DatasetCfg
from .pipeline import ingest as run_ingest
from .pipeline import ingest_one_camera as run_ingest_one
from .pipeline import localize as run_localize
from .pipeline import remask as run_remask
from .pipeline import segment as run_segment

# A camera id is used verbatim as a filesystem subdir and as a region_id prefix
# (`{camera_id}::{area_id}`), so it must be a strict slug — a '/', '..', ':', or
# space would corrupt paths, joins, and region parsing.
CAMERA_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
ALLOWED_EXT = {".mp4", ".mov", ".avi", ".mkv", ".m4v"}


def valid_camera_id(name: str) -> bool:
    return bool(CAMERA_ID_RE.match(name))


def allowed_ext(filename: str) -> bool:
    return Path(filename).suffix.lower() in ALLOWED_EXT


@dataclass
class Job:
    job_id: str
    dataset_id: str
    label: str
    status: str = "queued"      # queued | running | done | failed
    stage: str = "queued"       # queued | ingesting | segmenting | localizing | remasking | done
    # What this job IS. 'upload' covers both a whole day and an added camera —
    # they differ in scope, not in kind. 'remask' is the outline backfill, which
    # is NOT tied to one day's arrival and must therefore be kept out of the
    # per-dataset job map the dashboard builds, or it would displace the upload
    # job for whichever day it happens to name.
    kind: str = "upload"        # upload | remask
    progress: float = 0.0       # 0..1, coarse (stage boundaries + per-frame during segment)
    message: str = "Queued"
    error: Optional[str] = None
    frames: int = 0
    detections: int = 0
    # Advisory per-camera data-quality warnings from the post-process health check,
    # e.g. ["camera_02 (obscured (dark), no cows detected)"]. Never blocks the job.
    warnings: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)  # epoch secs; newest-first ordering


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()

ACTIVE = {"queued", "running"}

# datasets.status — DATA MATURITY, deliberately distinct from a Job's lifecycle
# status (see docs/roadmap/ROADMAP.md: the two must not be conflated). A day climbs
# this ladder as the pipeline fills it in; 'uploaded' is the new bottom rung, set
# the instant the videos are on disk and before any model has run.
UPLOADED = "uploaded"

# Work queue + the single thread draining it. The thread starts lazily on the first
# job and lives for the process; it is a daemon so it never blocks shutdown. Queued
# work is in-memory only - recover_jobs() fails anything still pending after a
# restart rather than pretending it will resume.
_QUEUE: "queue.Queue[tuple]" = queue.Queue()
_WORKER: Optional[threading.Thread] = None
_WORKER_LOCK = threading.Lock()


def _worker_loop() -> None:
    while True:
        fn, args = _QUEUE.get()
        try:
            fn(*args)
        except BaseException:  # noqa: BLE001 - _run/_run_add_camera already record
            pass               # failure on the job; one bad day must not kill the worker
        finally:
            _QUEUE.task_done()


def _enqueue(fn: Callable, *args) -> None:
    """Hand work to the serial worker, starting it on first use."""
    global _WORKER
    with _WORKER_LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_worker_loop, daemon=True, name="cownting-uploads")
            _WORKER.start()
    _QUEUE.put((fn, args))


def active_job_for(dataset_id: str) -> Optional[Job]:
    """The queued/running job for `dataset_id`, if any.

    Guards the one case a queue cannot make safe: re-uploading a day that is
    already in flight. The endpoint rmtree's `_uploads/<day>` before landing the
    replacement, which would pull the videos out from under a running ingest, and
    ingest itself purges the dataset. Different days queue up fine; the same day
    twice at once does not.
    """
    with _LOCK:
        for j in _JOBS.values():
            if j.dataset_id == dataset_id and j.status in ACTIVE:
                return j
    return None


# JSON snapshot of _JOBS for restart-recovery. Set once via recover_jobs(config)
# at app boot; None means "not persisting" (e.g. tests) and every write is a no-op.
_STORE_PATH: Optional[Path] = None
_last_flush = 0.0


def _persist(force: bool = False) -> None:
    """Mirror _JOBS to the JSON store. Throttled to ~once/sec unless `force` (stage
    boundaries / terminal states persist immediately) so the per-frame segment
    progress doesn't hammer the disk. Caller must hold _LOCK."""
    global _last_flush
    if _STORE_PATH is None:
        return
    now = time.time()
    if not force and now - _last_flush < 1.0:
        return
    _last_flush = now
    try:
        tmp = _STORE_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps([asdict(j) for j in _JOBS.values()]))
        os.replace(tmp, _STORE_PATH)  # atomic swap so a crash mid-write can't corrupt it
    except OSError:
        pass  # persistence is best-effort; never fail a job over a disk hiccup


def recover_jobs(config: Config) -> None:
    """Point the job store at data/upload_jobs.json and reload any prior snapshot.

    Called once at app boot. Jobs that were still queued/running when the process
    died can't resume (their worker thread is gone), so they're marked failed —
    'interrupted' — rather than left forever pretending to run. Idempotent."""
    global _STORE_PATH
    _STORE_PATH = Path(config.paths.db_path).parent / "upload_jobs.json"
    if not _STORE_PATH.exists():
        return
    try:
        raw = json.loads(_STORE_PATH.read_text())
    except (OSError, ValueError):
        return
    fields = {f for f in Job.__dataclass_fields__}
    with _LOCK:
        for d in raw:
            job = Job(**{k: v for k, v in d.items() if k in fields})
            if job.status in ACTIVE:
                job.status = "failed"
                job.error = "interrupted by a server restart"
                job.message = "Interrupted by a server restart — re-upload this day to finish it."
            _JOBS[job.job_id] = job
        _persist(force=True)


def get_job(job_id: str) -> Optional[Job]:
    with _LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> list[dict]:
    """Every known job, newest first — active ones lead so the frontend can spot a
    running upload and reconnect its progress bar after a refresh / from any tab."""
    with _LOCK:
        jobs = sorted(_JOBS.values(),
                      key=lambda j: (j.status in ACTIVE, j.created_at), reverse=True)
        return [asdict(j) for j in jobs]


def job_dict(job: Job) -> dict:
    with _LOCK:
        return asdict(job)


def _update(job: Job, **fields) -> None:
    with _LOCK:
        for k, v in fields.items():
            setattr(job, k, v)
        # Terminal + stage transitions persist immediately; per-frame progress is
        # throttled inside _persist so segmentation doesn't thrash the disk.
        _persist(force=job.status in ("done", "failed") or "stage" in fields)


def register_dataset(base: Config, dataset_id: str, day: str | None, label: str) -> None:
    """Create the datasets row the moment the videos have landed, as 'uploaded'.

    Without this the row only appears once the worker thread reaches ingest, so a
    day the user just spent minutes uploading was invisible everywhere — no card,
    no entry in the day picker — and the dashboard, asked for a dataset with no
    frames, sat on its loading skeleton forever. Registering up front makes the
    upload itself the visible milestone: the day shows up immediately, flagged as
    not-yet-processed, and matures through ingested -> segmented -> localized as
    the model works through it.

    Best-effort: a day that is safely on disk must not fail its upload over a
    bookkeeping write. The worker re-upserts the row at ingest anyway.
    """
    try:
        con = db.connect(base.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, dataset_id, day, label, status=UPLOADED)
        finally:
            con.close()
    except Exception:  # noqa: BLE001 — the videos are landed; never fail on bookkeeping
        pass


def start_upload_job(
    base: Config,
    saved: list[tuple[str, str, str]],  # (camera_id, video_path, start_iso) per camera
    dataset_id: str,
    day: str,
    label: str,
) -> Job:
    """Register a queued job and kick off processing on a daemon thread. Returns
    the Job immediately (202-style) so the request doesn't block on segmentation."""
    job = Job(job_id=uuid.uuid4().hex, dataset_id=dataset_id, label=label)
    with _LOCK:
        _JOBS[job.job_id] = job
        _persist(force=True)
    # Row first, work second: by the time the 202 reaches the browser the day is
    # already listable, so the frontend can send the user straight to it.
    register_dataset(base, dataset_id, day, label)
    _enqueue(_run, job, base, saved, dataset_id, day, label)
    return job


def _camera_health(config: Config, dataset_id: str) -> list[dict]:
    """Per-camera health for `dataset_id`, or [] if the check itself fails — a
    quality warning must never turn a successful upload into a failed job."""
    try:
        from .quality import camera_health
        return camera_health(config, dataset_id)
    except Exception:  # noqa: BLE001 — advisory only; swallow and report no warnings
        return []


def _warnings_from(health: list[dict]) -> list[str]:
    from .quality import describe
    return [describe(h) for h in health if not h["ok"]]


def start_add_camera_job(
    base: Config, dataset_id: str, camera_id: str, video_path: str,
    start_iso: str, label: str,
) -> Job:
    """Register a queued job that ADDS/REPLACES one camera stream in an existing
    dataset (ingest that one camera -> segment it -> re-localize the day), then
    kicks it off on a daemon thread. Returns the Job immediately."""
    job = Job(job_id=uuid.uuid4().hex, dataset_id=dataset_id, label=label)
    with _LOCK:
        _JOBS[job.job_id] = job
        _persist(force=True)
    # Same queue as whole-day uploads: an added camera runs the same segmenter over
    # the same database, so it must not overlap one.
    _enqueue(_run_add_camera, job, base, dataset_id, camera_id, video_path, start_iso)
    return job


def start_remask_job(base: Config, dataset_id: str | None = None,
                     camera_id: str | None = None, limit: int | None = None) -> Job:
    """Queue the outline backfill and return immediately.

    IT RUNS IN THIS PROCESS, ON THE SAME SERIAL QUEUE as uploads, and that is the
    whole reason this exists rather than leaving `cownting remask` as the only
    entry point. DuckDB allows one read-write PROCESS per file, and `remask` holds
    a write handle for its entire pass — so running the CLI against a live
    deployment does not slow the app down, it takes it OFF THE AIR for the
    duration: every request burns its ~9 s connect-retry budget and then 500s.
    In-process, the handle is already ours and there is no contention at all.

    The cost of that choice, stated plainly: the backfill occupies the upload
    queue, so a day uploaded while it runs waits behind it. On a CPU-only
    deployment a whole-corpus pass is hours, which is why `dataset_id` /
    `camera_id` / `limit` are here — the pass is resumable, so an operator can
    run it in bounded chunks and let uploads through between them.
    """
    scope = dataset_id or "all days"
    job = Job(job_id=uuid.uuid4().hex, dataset_id=dataset_id or "",
              label=f"Outlines · {scope}", kind="remask")
    with _LOCK:
        _JOBS[job.job_id] = job
        _persist(force=True)
    _enqueue(_run_remask, job, base, dataset_id, camera_id, limit)
    return job


def _run_remask(job: Job, base: Config, dataset_id: str | None,
                camera_id: str | None, limit: int | None) -> None:
    try:
        _update(job, status="running", stage="remasking", progress=0.02,
                message="Looking for detections without an outline…")

        def on_frame(done: int, total: int) -> None:
            frac = done / total if total else 1.0
            _update(job, progress=0.02 + 0.96 * frac,
                    message=f"Tracing outlines… frame {done}/{total}")

        stats = run_remask(base, dataset_id=dataset_id, camera_id=camera_id,
                           limit=limit, on_progress=on_frame)
        matched, dets = stats["matched"], stats["detections"]
        rate = (100.0 * matched / dets) if dets else 0.0
        msg = (f"Traced {matched} of {dets} outlines ({rate:.0f}%) "
               f"across {stats['frames']} frames.")
        if dets and rate < 80.0:
            # Not a failure — the rows that matched are correct — but the operator
            # has to know the segmenter no longer reproduces these detections, or
            # they will trust outlines that came from different weights than the
            # boxes beside them.
            msg += (" LOW MATCH RATE — check the weights match the ones this "
                    "footage was segmented with.")
        _update(job, status="done", stage="done", progress=1.0,
                detections=matched, frames=stats["frames"], message=msg)
    except Exception as e:  # noqa: BLE001 — surface it in the UI, never kill the worker
        _update(job, status="failed", error=str(e), message=f"Failed: {e}")


def _run_add_camera(
    job: Job, base: Config, dataset_id: str, camera_id: str,
    video_path: str, start_iso: str,
) -> None:
    try:
        cfg = base.model_copy(deep=True)
        cfg.cameras = [CameraCfg(id=camera_id, video=video_path, start=start_iso)]
        cfg.dataset = DatasetCfg(id=dataset_id)  # existing day/label kept by ingest_one

        _update(job, status="running", stage="ingesting", progress=0.05,
                message=f"Reading {camera_id} and sampling frames…")
        n_frames = run_ingest_one(cfg, dataset_id, cfg.cameras[0])

        _update(job, stage="segmenting", progress=0.15, frames=n_frames,
                message=f"Detecting cows across {n_frames} frames…")

        def on_seg(done: int, total: int) -> None:
            frac = done / total if total else 1.0
            _update(job, progress=0.15 + 0.75 * frac,
                    message=f"Detecting cows… frame {done}/{total}")

        # Scope segmentation to just this camera's new frames so it can't sweep up
        # unprocessed frames left elsewhere.
        n_det = run_segment(cfg, on_progress=on_seg, dataset_id=dataset_id, camera_id=camera_id)

        _update(job, stage="localizing", progress=0.92, detections=n_det,
                message="Assigning cows to count areas…")
        run_localize(cfg, dataset_id=dataset_id)

        health = _camera_health(cfg, dataset_id)
        warnings = _warnings_from(health)
        mine = next((h for h in health if h["camera_id"] == camera_id), None)
        if mine is not None and not mine["ok"]:
            from .quality import describe
            msg = (f"Added {camera_id}, but it still looks off — {describe(mine)}. "
                   "Check the footage before trusting it.")
        else:
            msg = f"Added {camera_id} — {n_frames} frames, {n_det} cows detected."
        _update(job, status="done", stage="done", progress=1.0, warnings=warnings, message=msg)
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI, don't crash the thread
        _update(job, status="failed", error=str(e), message=f"Failed: {e}")


def _run(
    job: Job,
    base: Config,
    saved: list[tuple[str, str, str]],
    dataset_id: str,
    day: str,
    label: str,
) -> None:
    try:
        cfg = base.model_copy(deep=True)
        cfg.cameras = [CameraCfg(id=cid, video=path, start=start) for cid, path, start in saved]
        cfg.dataset = DatasetCfg(id=dataset_id, day=day, label=label)

        _update(job, status="running", stage="ingesting", progress=0.05,
                message="Reading video and sampling frames…")
        n_frames = run_ingest(cfg)

        _update(job, stage="segmenting", progress=0.15, frames=n_frames,
                message=f"Detecting cows across {n_frames} frames…")

        def on_seg(done: int, total: int) -> None:
            # Segmentation is the long pole; map its per-frame progress into 0.15..0.9.
            frac = done / total if total else 1.0
            _update(job, progress=0.15 + 0.75 * frac,
                    message=f"Detecting cows… frame {done}/{total}")

        n_det = run_segment(cfg, on_progress=on_seg)

        _update(job, stage="localizing", progress=0.92, detections=n_det,
                message="Assigning cows to count areas…")
        run_localize(cfg, dataset_id=dataset_id)

        # Advisory post-process quality check: warn (never block) if a camera looks
        # obscured / stopped early / found no cows, so the user can delete + re-upload
        # just that stream from the day's camera manager.
        warnings = _warnings_from(_camera_health(cfg, dataset_id))
        if warnings:
            n = len(warnings)
            msg = (f"Upload complete, but {n} camera{'s' if n > 1 else ''} need a look: "
                   + "; ".join(warnings) + ".")
        else:
            msg = "Upload complete — the day is ready on the dashboard."
        _update(job, status="done", stage="done", progress=1.0, warnings=warnings, message=msg)
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI, don't crash the thread
        _update(job, status="failed", error=str(e), message=f"Failed: {e}")
