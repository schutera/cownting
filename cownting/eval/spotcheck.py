"""Predicted-vs-manual count error.

The cheapest ground truth a human can produce: a CSV of `frame_path,manual_count`
for a handful of sampled frames. Tells you whether the zero-shot detector is
trustworthy before you invest in fine-tuning (Stage 1b).
"""
from __future__ import annotations

import pandas as pd

from .. import db
from ..config import Config


def count_error(config: Config, manual_csv: str) -> dict:
    manual = pd.read_csv(manual_csv)  # columns: frame_path, manual_count
    con = db.connect(config.paths.db_path, read_only=True)
    pred = con.execute(
        "SELECT frame_path, count(*) AS pred_count FROM detections GROUP BY 1"
    ).df()
    con.close()

    merged = manual.merge(pred, on="frame_path", how="left").fillna({"pred_count": 0})
    err = merged["pred_count"] - merged["manual_count"]
    result = {
        "n_frames": int(len(merged)),
        "mae": round(float(err.abs().mean()), 3),
        "bias": round(float(err.mean()), 3),
        "total_manual": int(merged["manual_count"].sum()),
        "total_pred": int(merged["pred_count"].sum()),
    }
    return result
