"""Site-wide cow-enclosure polygon on the orthophoto.

A single polygon in orthophoto pixels. Localized detections outside it are
dropped — a physical bound on where cows can be, tighter than the per-camera
calibration hull. Pure numpy, JSON-serializable.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def load_fence(path: str) -> list | None:
    """Return the polygon [[x,y],...] (>=3 vertices) or None if unset."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        poly = json.load(open(p)).get("polygon")
    except Exception:  # noqa: BLE001 - a corrupt fence file must not break localize
        return None
    return poly if poly and len(poly) >= 3 else None


def close_ring(polygon) -> list:
    """Return the polygon as a closed ring: the first vertex repeated as the last.

    A fence is a closed polygon, so its first and last points are always the
    same. Idempotent — an already-closed ring keeps a single trailing duplicate.
    Fewer than 3 vertices are returned unchanged (nothing to close).
    """
    pts = [[float(p[0]), float(p[1])] for p in polygon]
    if len(pts) < 3:
        return pts
    while len(pts) > 1 and pts[-1] == pts[0]:  # drop any existing closure(s) first
        pts.pop()
    pts.append(list(pts[0]))
    return pts


def save_fence(path: str, polygon) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {"polygon": close_ring(polygon)}
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def point_in_polygon(pts, polygon) -> np.ndarray:
    """Vectorized even-odd ray-cast test. pts (N,2) -> bool (N,).

    Non-finite points (NaN from an out-of-hull clip) test False (outside).
    """
    poly = np.asarray(polygon, dtype=np.float64)
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    x, y = p[:, 0], p[:, 1]
    inside = np.zeros(len(p), dtype=bool)
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cond = ((yi > y) != (yj > y)) & (
            x < (xj - xi) * (y - yi) / (yj - yi + 1e-12) + xi
        )
        inside ^= cond
        j = i
    return inside & np.isfinite(x) & np.isfinite(y)
