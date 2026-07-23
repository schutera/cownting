"""Regression tests for cownting.ingest.video.index_video frame sampling.

No pytest. Run either way:
    python -m tests.test_video
    python tests/test_video.py

Covers the two sampling modes a real bug slipped through:
  * real-time video: target_fps subsamples (step = round(vfps / target_fps));
  * time-lapse (ingest.frame_interval_seconds set): EVERY captured frame is kept
    (no target_fps subsampling), and timestamps advance one interval per frame.
"""
from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta

import cv2
import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.config import CameraCfg, IngestCfg  # noqa: E402
from cownting.ingest import index_video  # noqa: E402

_FAILED = 0
FPS = 10
NFRAMES = 50


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


def _make_video(path: str) -> None:
    w = cv2.VideoWriter(path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (64, 48))
    for i in range(NFRAMES):
        w.write(np.full((48, 64, 3), i % 256, dtype=np.uint8))
    w.release()


def _readable_frames(path: str) -> int:
    cap = cv2.VideoCapture(path)
    n = 0
    while cap.grab():
        n += 1
    cap.release()
    return n


def main() -> None:
    print("=== test_video ===")
    with tempfile.TemporaryDirectory() as d:
        vid = os.path.join(d, "clip.mp4")
        _make_video(vid)
        if _readable_frames(vid) == 0:
            # No usable video codec in this environment — skip rather than false-fail.
            print("[skip] could not synthesize a readable test video here")
            print("PASS")
            return

        cam = CameraCfg(id="cam", video=vid, start="2025-01-01T00:00:00")

        # real-time: target_fps=2 on a 10 fps clip -> step 5 -> ~10 frames kept.
        rt = index_video(
            cam, IngestCfg(target_fps=2.0, frame_interval_seconds=None, save_frames=False), d, "rt"
        )
        check("real-time subsamples (target_fps applies)", 4 <= len(rt) <= 14,
              f"kept {len(rt)} of {NFRAMES}")

        # time-lapse: keep EVERY captured frame, spaced 60 s apart (the fix).
        tl = index_video(
            cam, IngestCfg(target_fps=2.0, frame_interval_seconds=60.0, save_frames=False), d, "tl"
        )
        check("time-lapse keeps (almost) every captured frame", len(tl) >= NFRAMES - 1,
              f"kept {len(tl)} of {NFRAMES}")
        check("time-lapse keeps far more than real-time", len(tl) > len(rt),
              f"tl={len(tl)} rt={len(rt)}")
        ts = list(tl["ts"])
        check("time-lapse first ts == start", ts[0] == datetime(2025, 1, 1, 0, 0, 0), str(ts[0]))
        check("time-lapse spacing == 60 s", (ts[1] - ts[0]) == timedelta(seconds=60),
              str(ts[1] - ts[0]))

    print("==================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
