"""Whitelisted 'features' for the cross-filter analysis: categorical buckets
derivable from a `detections` row via TRUSTED SQL expressions.

This registry is the injection boundary — the `/api/crosstab` endpoint takes
feature *keys* from the client and resolves them here; the SQL never comes from
user input. Adding a new pivotable feature (shade, head-pose, ...) is one entry.
`d` aliases detections, `f` aliases frames.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FeatureSpec:
    key: str                      # stable id used in the URL — matches the frontend key
    sql: str                      # TRUSTED SQL expr producing the bucket (never from user input)
    kind: str                     # 'categorical' | 'temporal_frame' | 'temporal_hour'
    needs_frames: bool            # True -> the query must JOIN frames (frame_idx lives there)
    fixed_domain: tuple | None = None   # None -> domain derived from data
    avail_col: str | None = None  # detections column whose non-null count gates availability
    drop_null: bool = False       # add "<sql> IS NOT NULL" (e.g. region: skip unassigned)


FEATURES: dict[str, FeatureSpec] = {
    "posture": FeatureSpec(
        "posture", "coalesce(d.posture, 'unknown')", "categorical", False,
        ("standing", "lying", "grazing", "unknown"), "posture"),
    "panel": FeatureSpec(
        "panel",
        # under_panel TRUE -> under a panel; anything else is open. FALSE (camera
        # has panel areas, cow outside them) AND NULL (camera has no panel areas
        # at all) both fold into 'open' -- a cow is never "unknown" w.r.t. panels.
        "CASE WHEN d.under_panel THEN 'under panel' ELSE 'open' END",
        "categorical", False, ("under panel", "open"), "under_panel"),
    "shade": FeatureSpec(  # RESERVED — unavailable until in_shade is populated
        "shade",
        "CASE WHEN d.in_shade THEN 'shade' "
        "WHEN d.in_shade = false THEN 'sun' ELSE 'unknown' END",
        "categorical", False, ("shade", "sun", "unknown"), "in_shade"),
    "region": FeatureSpec(
        "region", "d.region_id", "categorical", False, None, "region_id", drop_null=True),
    "camera": FeatureSpec(
        "camera", "d.camera_id", "categorical", False, None),
    "hour": FeatureSpec(
        "hour", "cast(extract('hour' FROM d.ts) AS INTEGER)", "temporal_hour", False),
    "frame": FeatureSpec(
        "frame", "f.frame_idx", "temporal_frame", True),
}


def resolve(key: str) -> FeatureSpec:
    try:
        return FEATURES[key]
    except KeyError:
        raise ValueError(f"unknown feature {key!r}")  # -> HTTP 400; never interpolated
