"""Ground-plane homography: camera pixels -> orthophoto pixels.

Pure functions so the math is testable without the Dash UI. The calibration
config is a JSON map { camera_id: {H, reproj_error, orthophoto, n_points} }.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Sequence, Tuple

import cv2
import numpy as np


def compute_homography(
    cam_pts: Sequence[Tuple[float, float]],
    ortho_pts: Sequence[Tuple[float, float]],
) -> Tuple[np.ndarray, float]:
    """Solve H mapping camera px -> orthophoto px. Returns (H, mean reproj error px)."""
    src = np.asarray(cam_pts, dtype=np.float64)
    dst = np.asarray(ortho_pts, dtype=np.float64)
    if len(src) < 4 or len(src) != len(dst):
        raise ValueError("need >= 4 matched point pairs")
    H, _ = cv2.findHomography(src, dst, cv2.RANSAC, 5.0)
    if H is None:
        raise RuntimeError("homography estimation failed (degenerate points?)")
    proj = cv2.perspectiveTransform(src.reshape(-1, 1, 2), H).reshape(-1, 2)
    err = float(np.sqrt(((proj - dst) ** 2).sum(axis=1)).mean())
    return H, err


def project_points(H, pts: Sequence[Tuple[float, float]]) -> np.ndarray:
    """Project camera-pixel points through H to orthophoto pixels (Nx2)."""
    arr = np.asarray(pts, dtype=np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(arr, np.asarray(H, dtype=np.float64)).reshape(-1, 2)


def load_all(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def save_homography(
    path: str,
    camera_id: str,
    H,
    err: float,
    orthophoto: str | None,
    cam_points: Sequence[Tuple[float, float]],
    ortho_points: Sequence[Tuple[float, float]],
) -> None:
    data = load_all(path)
    data[camera_id] = {
        "H": np.asarray(H).tolist(),
        "reproj_error": err,
        "orthophoto": orthophoto,
        "n_points": len(cam_points),
        "cam_points": [[float(p[0]), float(p[1])] for p in cam_points],
        "ortho_points": [[float(p[0]), float(p[1])] for p in ortho_points],
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
