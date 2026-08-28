"""Per-camera data-quality health, against REAL frames on disk + a REAL DuckDB.

No pytest. Run either way:
    python -m tests.test_quality
    python tests/test_quality.py

Writes synthetic grayscale frames to disk at the EXACT frame_path also inserted
into the frames table, so quality._p90_luminance samples real pixels (no mocks).
Reproduces the 2025-10-15 camera_02 failure — an obscured lens (near-black
frames) whose clip stopped early and detected 0 cows — beside a healthy full-day
camera, and checks the shape of the verdict fields plus the >=2-camera guard on
the "truncated" rule.

Brightness values and time spans are derived from the module constants
(quality.DARK_P90 / quality.TRUNCATED_FRAC), so the test tracks the thresholds
rather than hardcoding them.

Prints each check and a final PASS; sys.exit(1) on any failure.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
from PIL import Image

# Allow `python tests/test_quality.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, quality  # noqa: E402
from cownting.config import Config, PathsCfg  # noqa: E402

_FAILED = 0


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
# Pixel values keyed off DARK_P90 so the test tracks the constant: the obscured
# camera sits well below the line, the healthy one well above it.
_DARK_VAL = max(1, int(quality.DARK_P90 * 0.1))      # ~9 for DARK_P90 = 90
_BRIGHT_VAL = min(250, int(quality.DARK_P90) + 50)   # ~140 for DARK_P90 = 90
# 64x64 keeps the ~4% timestamp-banner crop from being the whole frame.
_SIZE = 64

WINDOW_START = datetime(2025, 10, 15, 6, 0, 0)
BRIGHT_SPAN = timedelta(hours=8)             # the day's longest stream
# Clearly under TRUNCATED_FRAC of the bright span (half the threshold), so the
# short camera trips "truncated" even if TRUNCATED_FRAC is retuned.
DARK_SPAN = BRIGHT_SPAN * (quality.TRUNCATED_FRAC * 0.5)


def _config(d: str) -> Config:
    data = os.path.join(d, "data")
    return Config(
        cameras=[],
        paths=PathsCfg(
            artifacts_dir=os.path.join(data, "artifacts"),
            db_path=os.path.join(data, "cownting.duckdb"),
            count_areas=os.path.join(data, "count_areas.json"),
            panel_areas=os.path.join(data, "panel_areas.json"),
        ),
    )


def _write_frames(root: str, dataset_id: str, camera_id: str, n: int,
                  first_ts: datetime, span: timedelta, value: int) -> pd.DataFrame:
    """Write n uniform grayscale PNG frames to disk and return the frame rows that
    point at them (ts evenly spaced across `span`, brightness == `value`)."""
    cam_dir = os.path.join(root, dataset_id, camera_id)
    os.makedirs(cam_dir, exist_ok=True)
    total = span.total_seconds()
    rows = []
    for i in range(n):
        frac = i / (n - 1) if n > 1 else 0.0
        ts = first_ts + timedelta(seconds=frac * total)
        fp = os.path.join(cam_dir, f"{i:08d}.png")
        Image.fromarray(np.full((_SIZE, _SIZE), value, dtype=np.uint8), mode="L").save(fp)
        rows.append(dict(dataset_id=dataset_id, camera_id=camera_id, frame_idx=i,
                         ts=ts, time_bin=int(ts.timestamp() // 2), frame_path=fp,
                         overlay_path=None, pose_overlay_path=None, processed=True))
    return pd.DataFrame(rows)


def _dets(dataset_id: str, camera_id: str, n: int, first_ts: datetime) -> pd.DataFrame:
    return pd.DataFrame([
        dict(dataset_id=dataset_id, camera_id=camera_id, ts=first_ts + timedelta(minutes=i))
        for i in range(n)
    ])


def _health_by_cam(config: Config, dataset_id: str) -> dict:
    return {e["camera_id"]: e for e in quality.camera_health(config, dataset_id)}


# --------------------------------------------------------------------------- 1: obscured vs healthy
def test_dark_vs_bright():
    ds = "2025-10-15"
    n_bright_det = 5
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        frames_root = os.path.join(d, "frames")
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, ds, date(2025, 10, 15), "Oct 15, 2025")
            # camera_bright: full-day span, bright, with detections -> healthy.
            db.insert_frames(con, _write_frames(frames_root, ds, "camera_bright", 12,
                                                WINDOW_START, BRIGHT_SPAN, _BRIGHT_VAL))
            # camera_dark: obscured lens (near-black), short span, 0 detections.
            db.insert_frames(con, _write_frames(frames_root, ds, "camera_dark", 6,
                                                WINDOW_START, DARK_SPAN, _DARK_VAL))
            db.insert_detections(con, _dets(ds, "camera_bright", n_bright_det, WINDOW_START))
        finally:
            con.close()

        h = _health_by_cam(config, ds)
        dark, bright = h["camera_dark"], h["camera_bright"]

        # The obscured / truncated / empty camera trips all three issue codes.
        check("dark camera flagged 'dark'", "dark" in dark["issues"], str(dark))
        check("dark camera flagged 'no_detections'", "no_detections" in dark["issues"], str(dark))
        check("dark camera flagged 'truncated'", "truncated" in dark["issues"], str(dark))
        check("dark camera ok is False", dark["ok"] is False, str(dark))

        # The healthy full-day camera is clean.
        check("bright camera issues empty", bright["issues"] == [], str(bright))
        check("bright camera ok is True", bright["ok"] is True, str(bright))

        # Verdict fields are shaped right.
        check("bright brightness_p90 is a float",
              isinstance(bright["brightness_p90"], float), str(bright["brightness_p90"]))
        check("dark brightness_p90 is a float (frames readable on disk)",
              isinstance(dark["brightness_p90"], float), str(dark["brightness_p90"]))
        check("bright span_seconds > 0", bright["span_seconds"] > 0, str(bright["span_seconds"]))
        check("bright n_frames == 12", bright["n_frames"] == 12, str(bright["n_frames"]))
        check("bright n_detections matches inserted",
              bright["n_detections"] == n_bright_det, str(bright["n_detections"]))
        check("dark n_detections == 0", dark["n_detections"] == 0, str(dark["n_detections"]))
        check("dark p90 below DARK_P90 <= bright p90",
              dark["brightness_p90"] < quality.DARK_P90 <= bright["brightness_p90"],
              f"dark={dark['brightness_p90']} bright={bright['brightness_p90']}")
        check("dark span clearly under TRUNCATED_FRAC of bright span",
              dark["span_seconds"] < quality.TRUNCATED_FRAC * bright["span_seconds"],
              f"dark={dark['span_seconds']} bright={bright['span_seconds']}")
        # first_ts / last_ts are ISO strings for a camera with frames.
        check("bright first_ts is an ISO string",
              isinstance(bright["first_ts"], str) and "T" in bright["first_ts"], str(bright["first_ts"]))


# --------------------------------------------------------------------------- 2: lone camera never truncated
def test_single_camera_never_truncated():
    ds = "solo"
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        frames_root = os.path.join(d, "frames")
        con = db.connect(config.paths.db_path)
        try:
            db.init_db(con)
            db.upsert_dataset(con, ds, date(2025, 10, 16), "solo")
            # A SHORT-span, bright, detected camera — but the ONLY camera in the day.
            # With <2 cameras there is nothing to be "short vs", so truncated must
            # not fire even though the span is well under any fraction of itself.
            db.insert_frames(con, _write_frames(frames_root, ds, "camera_only", 6,
                                                WINDOW_START, DARK_SPAN, _BRIGHT_VAL))
            db.insert_detections(con, _dets(ds, "camera_only", 3, WINDOW_START))
        finally:
            con.close()

        only = _health_by_cam(config, ds)["camera_only"]
        check("single-camera dataset never flagged 'truncated'",
              "truncated" not in only["issues"], str(only))
        check("single healthy camera ok is True", only["ok"] is True, str(only))


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_quality ===")
    test_dark_vs_bright()
    test_single_camera_never_truncated()
    print("====================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
