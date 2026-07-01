"""Pick a diverse frame subset to hand-correct.

480 frames at 1/min are highly redundant (cows barely move minute-to-minute)
and partly fogged. Labeling all of them is wasted effort. We stratify across
time-of-day (hour) x detection-count tier and round-robin across buckets, so
dawn-fog, midday-clear, empty, and crowded scenes are all represented in the
~150 frames a human actually corrects.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from .. import db
from ..config import Config

_MANIFEST = "selected.csv"
_LIST = "selected.txt"

# detection-count tiers -> scene-density buckets
_TIER_BINS = [-1, 0, 2, 5, np.inf]
_TIER_LABELS = ["empty", "low", "mid", "high"]


def _frame_stats(config: Config) -> pd.DataFrame:
    """One row per frame: quality + how many cows the zero-shot pass found."""
    con = db.connect(config.paths.db_path, read_only=True)
    df = con.execute(
        """
        SELECT f.camera_id, f.frame_idx, f.ts, f.frame_path,
               coalesce(f.frame_quality, 'ok')        AS quality,
               count(d.detection_id)                  AS n_det
        FROM frames f
        LEFT JOIN detections d ON d.frame_path = f.frame_path
        GROUP BY 1, 2, 3, 4, 5
        ORDER BY f.camera_id, f.frame_idx
        """
    ).df()
    con.close()
    return df


def select_frames(config: Config) -> pd.DataFrame:
    """Write the stratified selection to <workspace>/selected.{txt,csv}."""
    cfg = config.label
    df = _frame_stats(config)
    if df.empty:
        raise RuntimeError("no frames in the DB — run `cownting ingest` (and `segment`) first")

    if cfg.exclude_blind:
        df = df[~df["quality"].isin(["blind", "dark", "missing"])].copy()
    if df.empty:
        raise RuntimeError("every frame was excluded as blind/dark — relax label.exclude_blind")

    df["hour"] = pd.to_datetime(df["ts"]).dt.hour
    df["det_tier"] = pd.cut(df["n_det"], bins=_TIER_BINS, labels=_TIER_LABELS)

    n = min(cfg.n_select, len(df))
    rng = np.random.default_rng(cfg.seed)

    # shuffle each (hour, tier) bucket, then round-robin across buckets for even coverage
    buckets: dict = {}
    for key, g in df.groupby(["hour", "det_tier"], observed=True):
        idx = g.index.to_numpy().copy()   # to_numpy() may be read-only
        rng.shuffle(idx)
        buckets[key] = list(idx)
    order = sorted(buckets)  # deterministic bucket visiting order

    picked: list = []
    while len(picked) < n:
        advanced = False
        for key in order:
            if buckets[key]:
                picked.append(buckets[key].pop(0))
                advanced = True
                if len(picked) >= n:
                    break
        if not advanced:
            break

    sel = df.loc[picked].sort_values(["camera_id", "frame_idx"]).reset_index(drop=True)

    ws = Path(cfg.workspace)
    ws.mkdir(parents=True, exist_ok=True)
    sel.to_csv(ws / _MANIFEST, index=False)
    (ws / _LIST).write_text("\n".join(sel["frame_path"].tolist()) + "\n")

    by_hour = sel.groupby("hour").size().to_dict()
    by_tier = sel.groupby("det_tier", observed=True).size().to_dict()
    print(f"[label-select] {len(sel)} frames -> {ws / _LIST}")
    print(f"[label-select] by hour:  {by_hour}")
    print(f"[label-select] by tier:  {by_tier}")
    return sel


def selected_paths(config: Config) -> list[str]:
    """Read back the selection written by `select_frames`."""
    p = Path(config.label.workspace) / _LIST
    if not p.exists():
        raise RuntimeError(f"{p} not found — run `cownting label-select` first")
    return [line for line in p.read_text().splitlines() if line.strip()]
