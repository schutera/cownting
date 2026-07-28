"""Per-camera data-quality health for an uploaded dataset.

A field camera can silently produce unusable footage — an obscured/failed lens
(near-black frames), a clip that stops hours early, or a view with nothing to
detect. The pipeline ingests such data fine (valid JPEGs, segmented, stored), so
it never errors; the damage only surfaces on the dashboard as an empty/black
camera (as happened to camera_02 on 2025-10-15).

This scores each camera AFTER processing and returns a per-camera verdict so the
UI can warn ("camera_02 looks obscured and stopped early, 0 cows") and offer to
delete + re-upload that one stream. It is ADVISORY ONLY — nothing here blocks an
upload or deletes anything; every frame stays ingested.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import numpy as np

from . import db
from .config import Config

# A camera whose BRIGHTEST frames never get bright is obscured/failed, not merely
# recording at night. Measured on 2025-10-15: healthy daytime cameras sit at p90
# luminance ~113-140, while the obscured camera_02 sits at p90 ~71 with a near-
# black median. Using p90 (not mean/median) means a full-day camera's legitimate
# night frames don't drag a genuinely-fine camera below the line.
DARK_P90 = 90.0
# A camera covering FAR less real time than the day's longest stream is truncated
# — its clip stopped early. Deliberately conservative (a quarter, not a half):
# cameras legitimately record different windows (a daytime-only 8h camera alongside
# a 24h one is ~33% of the max and is NOT broken), so only a stream well under a
# quarter of the longest — as the obscured camera_02 was on 2025-10-15 (3.5h of a
# 24h day ≈ 15%) — is flagged, avoiding false alarms on genuinely-fine short clips.
TRUNCATED_FRAC = 0.25

_SAMPLE = 24            # frames sampled per camera for the brightness estimate
_BANNER_CROP = 0.96    # drop the bottom ~4% (Brinno timestamp bar) before averaging

# Human-facing labels for each issue code (shared by the API + upload messages).
ISSUE_LABELS = {
    "dark": "obscured (dark)",
    "truncated": "stopped early",
    "no_detections": "no cows detected",
}


def _p90_luminance(frame_paths: list[str], sample: int = _SAMPLE) -> Optional[float]:
    """90th-percentile of per-frame mean luminance over up to `sample` evenly
    spaced frames. None when no frame is readable (all missing/corrupt)."""
    if not frame_paths:
        return None
    from PIL import Image

    positions = np.unique(
        np.linspace(0, len(frame_paths) - 1, min(sample, len(frame_paths))).astype(int)
    )
    lums: list[float] = []
    for i in positions:
        p = frame_paths[int(i)]
        if not p or not Path(p).exists():
            continue
        try:
            with Image.open(p) as im:
                arr = np.asarray(im.convert("L"), dtype=np.float32)
        except Exception:  # noqa: BLE001 — a corrupt frame just doesn't contribute
            continue
        if arr.size == 0:
            continue
        arr = arr[: max(1, int(arr.shape[0] * _BANNER_CROP)), :]  # crop the timestamp bar
        lums.append(float(arr.mean()))
    if not lums:
        return None
    return float(np.percentile(lums, 90))


def camera_health(config: Config, dataset_id: str) -> list[dict]:
    """Per-camera quality verdict for one dataset, sorted by camera_id.

    Each entry:
      camera_id, n_frames, first_ts / last_ts (ISO str or None), span_seconds,
      n_detections, brightness_p90 (float or None),
      issues (subset of 'dark' / 'truncated' / 'no_detections'), ok (issues empty).

    Advisory only — the caller decides what to do with a flagged camera."""
    con = db.connect(config.paths.db_path)
    try:
        frames = con.execute(
            "SELECT camera_id, ts, frame_path FROM frames "
            "WHERE dataset_id = ? ORDER BY camera_id, frame_idx",
            [dataset_id],
        ).df()
        det = con.execute(
            "SELECT camera_id, count(*) AS n FROM detections "
            "WHERE dataset_id = ? GROUP BY camera_id",
            [dataset_id],
        ).df()
    finally:
        con.close()

    det_by_cam = {r.camera_id: int(r.n) for r in det.itertuples()}

    # First pass: per-camera frame stats (need every camera's span before we can
    # judge any single camera as truncated relative to the day's longest).
    per_cam: dict[str, tuple] = {}
    for cam, grp in frames.groupby("camera_id"):
        ts = grp["ts"].dropna()
        first = ts.min() if len(ts) else None
        last = ts.max() if len(ts) else None
        span = float((last - first).total_seconds()) if (first is not None and last is not None) else 0.0
        per_cam[cam] = (grp, first, last, span)

    max_span = max((v[3] for v in per_cam.values()), default=0.0)
    multi = len(per_cam) >= 2  # a lone camera can't be "short vs the others"

    out: list[dict] = []
    for cam in sorted(per_cam):
        grp, first, last, span = per_cam[cam]
        n_det = det_by_cam.get(cam, 0)
        p90 = _p90_luminance(grp["frame_path"].tolist())

        issues: list[str] = []
        if p90 is not None and p90 < DARK_P90:
            issues.append("dark")
        if multi and max_span > 0 and span < TRUNCATED_FRAC * max_span:
            issues.append("truncated")
        if n_det == 0:
            issues.append("no_detections")

        out.append({
            "camera_id": cam,
            "n_frames": int(len(grp)),
            "first_ts": first.isoformat() if first is not None else None,
            "last_ts": last.isoformat() if last is not None else None,
            "span_seconds": span,
            "n_detections": n_det,
            "brightness_p90": p90,
            "issues": issues,
            "ok": not issues,
        })
    return out


def describe(health_entry: dict) -> str:
    """A short human phrase for a flagged camera, e.g.
    'camera_02 (obscured (dark), no cows detected)'. Empty issues -> just the id."""
    if health_entry["ok"]:
        return health_entry["camera_id"]
    why = ", ".join(ISSUE_LABELS.get(i, i) for i in health_entry["issues"])
    return f"{health_entry['camera_id']} ({why})"
