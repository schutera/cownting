"""Count-area regions: point-in-polygon assignment of ground points to named areas."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def point_in_polygon(pts, polygon):
    poly = np.asarray(polygon, dtype=float)
    pts = np.asarray(pts, dtype=float)
    if pts.ndim == 1:
        pts = pts.reshape(1, -1)
    n = len(poly)
    inside = np.zeros(len(pts), dtype=bool)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        cross = (yi > pts[:, 1]) != (yj > pts[:, 1])
        slope = (xj - xi) * (pts[:, 1] - yi) / (yj - yi + 1e-12) + xi
        inside ^= cross & (pts[:, 0] < slope)
        j = i
    return inside


def load_count_areas(path) -> dict[str, list[dict]]:
    """Return the count-area mapping (camera -> list of areas); {} if the file is missing."""
    p = Path(path)
    if not p.exists():
        return {}
    with open(p) as f:
        return json.load(f)


def save_count_areas(path, data) -> None:
    """Write the count-area mapping as pretty JSON, creating the parent dir if needed."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2)


def assign_regions(ground_px, areas, camera_id) -> list:
    """For each ground point, return the composite region id of the first containing area, else None.

    ground_px: (N, 2) image-space points. areas: the list of area dicts for THIS camera.
    Non-finite points (nan/inf) map to None.
    """
    pts = np.asarray(ground_px, dtype=float)
    if pts.ndim == 1:
        pts = pts.reshape(1, -1)
    n = len(pts)
    result: list = [None] * n
    finite = np.isfinite(pts).all(axis=1)
    for area in areas:
        remaining = np.array(
            [i for i in range(n) if result[i] is None and finite[i]], dtype=int
        )
        if len(remaining) == 0:
            break
        inside = point_in_polygon(pts[remaining], area["camera_polygon"])
        region_id = f"{camera_id}::{area['id']}"
        for idx, hit in zip(remaining, inside):
            if hit:
                result[idx] = region_id
    return result
