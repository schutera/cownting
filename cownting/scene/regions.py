"""Count-area regions: point-in-polygon assignment of ground points to named areas."""
from __future__ import annotations

import json
import os
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
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def save_count_areas(path, data) -> None:
    """Write the count-area mapping as pretty JSON, creating the parent dir if needed.

    Written to a sibling temp file and moved into place, because `open(p, "w")`
    TRUNCATES FIRST: a kill, an OOM or a full disk between the truncate and the
    flush leaves a half-written JSON document where the areas used to be. Nothing
    ever repairs that file, and every reader of it — the day picker, the area
    editor, the localize worker — then fails on a day that previously worked.
    `os.replace` is atomic on both POSIX and Windows, so a reader sees either the
    old file or the new one and never a partial one."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def dataset_area_path(config, dataset_id, kind):
    """Resolve the area file for a dataset (kind: "count" or "panel").

    Per-dataset areas live under data/areas/<dataset_id>/ — a sibling of
    artifacts/, NOT under artifacts/<dataset_id>/ which pipeline.ingest rmtrees on
    re-ingest. `dataset_id` None falls back to the legacy flat config path."""
    legacy = config.paths.count_areas if kind == "count" else config.paths.panel_areas
    if dataset_id is None:
        return Path(legacy)
    root = (Path(config.paths.artifacts_dir).parent / "areas").resolve()
    path = root / dataset_id / f"{kind}_areas.json"
    # `dataset_id` reaches here from a query param. The API validates it, but the
    # invariant belongs with the path builder as well: this function is called
    # from pipeline and CLI paths that never saw that check, and a traversal here
    # is an arbitrary file read on the load side and an arbitrary JSON write
    # (with mkdir -p) on the save side. Resolve and confirm containment.
    try:
        resolved = path.resolve()
    except (ValueError, OSError) as e:
        raise ValueError(f"unusable dataset id {dataset_id!r}") from e
    if resolved != root and root not in resolved.parents:
        raise ValueError(f"dataset id {dataset_id!r} escapes the areas directory")
    return path


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
