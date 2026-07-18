"""Decode a prerecorded video into timestamped, fps-subsampled frames."""
from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import cv2
import pandas as pd

from ..config import CameraCfg, IngestCfg


def _start_time(cam: CameraCfg) -> datetime:
    if cam.start:
        return datetime.fromisoformat(cam.start)
    # No explicit start: read the capture time from the file itself — the
    # container creation_time, else the burned-in Brinno bar (see
    # ingest.capture_time), else the file's mtime.
    from .capture_time import read_burned_timestamp, read_container_time

    return (read_container_time(cam.video)
            or read_burned_timestamp(cam.video)
            or datetime.fromtimestamp(os.path.getmtime(cam.video)))


def index_video(cam: CameraCfg, ingest_cfg: IngestCfg, artifacts_dir: str,
                dataset_id: str | None = None) -> pd.DataFrame:
    """Sample frames, write them to artifacts, and return the frame index rows.

    Per-frame timestamp:
      * time-lapse (ingest.frame_interval_seconds set): start + frame_idx * frame_interval_seconds
        — each captured frame is one interval of real time apart (e.g. a Brinno at
        1 frame/minute -> 60.0), so 480 frames span 8 h, not the video's runtime.
      * real-time video (frame_interval_seconds is None): start + frame_idx / video_fps.

    `start` is the real capture start (CameraCfg.start when provided, else file mtime).

    Frames are written under a per-dataset subdir (<artifacts>/<dataset_id>/frames/
    <cam>/) when `dataset_id` is given, so a second day's frame_idx range for the
    same camera can't overwrite the first on disk and frame_path stays globally
    unique. When None (legacy single-day flow), the old flat layout is kept.
    """
    if not Path(cam.video).exists():
        raise FileNotFoundError(cam.video)
    cap = cv2.VideoCapture(cam.video)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video: {cam.video}")

    vfps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(vfps / max(ingest_cfg.target_fps, 1e-6)))
    start = _start_time(cam)
    base = Path(artifacts_dir) / dataset_id if dataset_id else Path(artifacts_dir)
    out_dir = base / "frames" / cam.id
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = []
    idx = 0
    while True:
        if not cap.grab():
            break
        if idx % step == 0:
            ok, frame = cap.retrieve()
            if not ok:
                break
            if ingest_cfg.frame_interval_seconds is not None:
                # Time-lapse: idx is the raw capture number, so real time advances
                # one interval per frame (independent of the file's playback fps).
                ts = start + timedelta(seconds=idx * ingest_cfg.frame_interval_seconds)
            else:
                ts = start + timedelta(seconds=idx / vfps)
            frame_path = ""
            if ingest_cfg.save_frames:
                frame_path = str(out_dir / f"{idx:08d}.jpg")
                cv2.imwrite(frame_path, frame)
            rows.append(
                dict(
                    dataset_id=dataset_id,
                    camera_id=cam.id,
                    frame_idx=idx,
                    ts=ts,
                    time_bin=int(ts.timestamp() // ingest_cfg.time_bin_seconds),
                    frame_path=frame_path,
                    overlay_path=None,
                    processed=False,
                )
            )
        idx += 1
    cap.release()
    return pd.DataFrame(rows)
