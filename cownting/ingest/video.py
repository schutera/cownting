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
    return datetime.fromtimestamp(os.path.getmtime(cam.video))


def index_video(cam: CameraCfg, ingest_cfg: IngestCfg, artifacts_dir: str) -> pd.DataFrame:
    """Sample frames, write them to artifacts, and return the frame index rows.

    Per-frame timestamp = start + frame_idx / video_fps.
    """
    if not Path(cam.video).exists():
        raise FileNotFoundError(cam.video)
    cap = cv2.VideoCapture(cam.video)
    if not cap.isOpened():
        raise RuntimeError(f"could not open video: {cam.video}")

    vfps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    step = max(1, round(vfps / max(ingest_cfg.target_fps, 1e-6)))
    start = _start_time(cam)
    out_dir = Path(artifacts_dir) / "frames" / cam.id
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
            ts = start + timedelta(seconds=idx / vfps)
            frame_path = ""
            if ingest_cfg.save_frames:
                frame_path = str(out_dir / f"{idx:08d}.jpg")
                cv2.imwrite(frame_path, frame)
            rows.append(
                dict(
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
