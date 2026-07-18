"""In-app multi-camera upload -> auto-process (ingest -> segment -> localize).

Single-box MVP of the roadmap's upload epic (DU2/DU3): a POST lands one video per
camera, a background thread runs the offline batch scoped to the new day, and a
job record exposes stage/progress so the frontend can show a progress bar.

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
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from .config import CameraCfg, Config, DatasetCfg
from .pipeline import ingest as run_ingest
from .pipeline import localize as run_localize
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
    stage: str = "queued"       # queued | ingesting | segmenting | localizing | done
    progress: float = 0.0       # 0..1, coarse (stage boundaries + per-frame during segment)
    message: str = "Queued"
    error: Optional[str] = None
    frames: int = 0
    detections: int = 0
    created_at: float = field(default_factory=time.time)  # epoch secs; newest-first ordering


_JOBS: dict[str, Job] = {}
_LOCK = threading.Lock()

ACTIVE = {"queued", "running"}

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
    threading.Thread(
        target=_run, args=(job, base, saved, dataset_id, day, label), daemon=True
    ).start()
    return job


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

        _update(job, status="done", stage="done", progress=1.0,
                message="Upload complete — the day is ready on the dashboard.")
    except Exception as e:  # noqa: BLE001 — surface any failure to the UI, don't crash the thread
        _update(job, status="failed", error=str(e), message=f"Failed: {e}")
