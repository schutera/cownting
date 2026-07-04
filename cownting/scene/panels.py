"""Solar-panel shelter test in camera image space — centre line + band width.

The panels sit ~2 m in the air, so their ground footprint is barely visible, but
the panel's **centre line** is easy to trace along the shaded ground strip where
cows stand under it. So the primitive is a centre line plus a band width: a cow
whose ground-contact point falls within ±width/2 of the line counts as
sheltering.

The test stays image-space, per camera (calibration-free — the cow→ortho
homography is too imperfect for naive ortho point-in-polygon), operating on each
detection's stored `ground_px`. Panels are NOT calibration anchors. Pure numpy,
JSON-serializable.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def load_panels(path: str) -> dict | None:
    """Return {"ortho": [...], "cameras": {...}} or None if unset/corrupt."""
    p = Path(path)
    if not p.exists():
        return None
    try:
        data = json.load(open(p))
    except Exception:  # noqa: BLE001 - a corrupt panels file must not break localize
        return None
    if not isinstance(data, dict):
        return None
    ortho = data.get("ortho") or []
    cameras = data.get("cameras") or {}
    if not isinstance(ortho, list) or not isinstance(cameras, dict):
        return None
    return {"ortho": ortho, "cameras": cameras}


def save_panels(path: str, panels: dict) -> None:
    """Persist panels as {ortho, cameras} of {id, centerline[, width]}. JSON indent=2.

    Centre lines are OPEN polylines — rings are NOT closed. Ortho panels carry a
    centre line only; camera panels also carry a band `width` (image px).
    """
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    data = {
        "ortho": _clean_panels(panels.get("ortho") or [], with_width=False),
        "cameras": {
            cam: _clean_panels(items or [], with_width=True)
            for cam, items in (panels.get("cameras") or {}).items()
        },
    }
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def _clean_panels(panels, with_width: bool) -> list:
    """Normalize panels to {id, centerline[, width]}, preserving order.

    Lenient like load_panels: an entry without a centre line (e.g. from the
    loosely-typed /api/panels body, or a legacy `polygon`-only entry) yields an
    empty centre line rather than crashing the save.
    """
    out = []
    for panel in panels:
        # Accept the new `centerline` key; tolerate a legacy `polygon` as a hint.
        line = panel.get("centerline")
        if line is None:
            line = panel.get("polygon") or []
        entry = {"id": panel.get("id"), "centerline": _as_points(line)}
        if with_width:
            entry["width"] = panel.get("width")
        out.append(entry)
    return out


def _as_points(line) -> list:
    """Coerce a polyline to a list of [x, y] float pairs; drop malformed points."""
    out = []
    for pt in line or []:
        try:
            x, y = float(pt[0]), float(pt[1])
        except (TypeError, ValueError, IndexError):
            continue
        out.append([x, y])
    return out


def camera_panels(panels, camera_id: str) -> list:
    """This camera's panels (image px), or [] if it sees none."""
    if not panels:
        return []
    return (panels.get("cameras") or {}).get(camera_id, [])


def _point_segment_distance(p, a, b) -> np.ndarray:
    """Distance from each point p (N,2) to segment a->b (2,). -> (N,)."""
    ab = b - a
    ap = p - a
    denom = float(ab @ ab)
    if denom < 1e-12:  # degenerate segment -> distance to the point
        return np.hypot(ap[:, 0], ap[:, 1])
    t = np.clip((ap @ ab) / denom, 0.0, 1.0)
    proj = a + t[:, None] * ab  # (N,2) closest point on the segment
    d = p - proj
    return np.hypot(d[:, 0], d[:, 1])


def _distance_to_polyline(pts, line) -> np.ndarray:
    """Min distance from each point (N,2) to an OPEN polyline. -> (N,).

    Iterates segments i -> i+1 with NO wraparound edge (unlike a closed ring).
    A single-vertex line degenerates to the point distance.
    """
    poly = np.asarray(line, dtype=np.float64)
    n = len(poly)
    best = np.full(len(pts), np.inf)
    if n == 0:
        return best
    if n == 1:  # a lone point: distance to it
        d = pts - poly[0]
        return np.hypot(d[:, 0], d[:, 1])
    for i in range(n - 1):  # open: last vertex has no edge back to the first
        best = np.minimum(best, _point_segment_distance(pts, poly[i], poly[i + 1]))
    return best


def assign_panels(ground_px, camera_id: str, panels, margin_px: float = 0.0) -> dict:
    """Per-point shelter test against this camera's centre-line bands.

    ground_px (N,2) image-space ground-contact points. For each camera panel with
    >= 2 centre-line points and width > 0, with `dist` the point-to-open-polyline
    distance and `half = width / 2`:
        "under_panel": bool (N,) — dist <= max(0, half - margin_px)
        "boundary":    bool (N,) — abs(dist - half) <= margin_px (band edge, uncertain)
        "panel_id":    object (N,) of str|None — first panel with dist <= half, else None
    Non-finite points -> under_panel False, boundary False, panel_id None. A camera
    with no panels -> all False/None.
    """
    p = np.asarray(ground_px, dtype=np.float64).reshape(-1, 2)
    n = len(p)
    under = np.zeros(n, dtype=bool)
    boundary = np.zeros(n, dtype=bool)
    panel_id = np.full(n, None, dtype=object)

    finite = np.isfinite(p[:, 0]) & np.isfinite(p[:, 1])
    cam_panels = camera_panels(panels, camera_id)
    if n == 0 or not cam_panels:
        return {"under_panel": under, "boundary": boundary, "panel_id": panel_id}

    for panel in cam_panels:
        line = panel.get("centerline")
        width = panel.get("width")
        if not line or len(line) < 2 or not width or width <= 0:
            continue
        half = float(width) / 2.0
        dist = _distance_to_polyline(p, line)
        under |= finite & (dist <= max(0.0, half - margin_px))
        boundary |= finite & (np.abs(dist - half) <= margin_px)
        # First band that contains the point (within half-width) wins the id.
        take = finite & (dist <= half) & (panel_id == None)  # noqa: E711 - object None compare
        panel_id[take] = panel.get("id")

    under &= finite
    boundary &= finite
    return {"under_panel": under, "boundary": boundary, "panel_id": panel_id}
