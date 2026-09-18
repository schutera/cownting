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


# The group a detection falls into when it is inside no count area. A literal
# rather than NULL so it survives the GROUP BY, and readable enough that a
# client with no label mapping still renders something meaningful.
REGION_NONE = "(outside any area)"

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
    # --- M5: what the crop classifier saw (docs/roadmap/M5_instance_attrs.md) ---
    # `sun` is NOT the `shade` entry above. That one reads `in_shade`, which is
    # M2's geometric-shadow column and has never been populated; overloading it
    # would leave a pivot unable to say whether it was showing a computed shadow
    # or a learned judgement. Two columns, two provenances, two features.
    #
    # 'unscored' is a real bucket rather than a dropped row: with scoring resumable
    # and yielding to uploads, a partially-scored corpus is an ordinary state, and
    # a pivot that silently omitted those rows would show a total that disagrees
    # with every other total on the page.
    # Three tail buckets, and the distinction matters: 'unscored' = the model
    # never looked (attr_model_rev NULL); 'unsure' = it looked and declined to
    # name a posture (the abstention rule in config.InstanceAttrsCfg); a class =
    # it looked and is confident. Collapsing unsure into unscored would hide
    # exactly the number the abstention exists to make visible.
    "sun": FeatureSpec(
        "sun",
        "CASE WHEN d.attr_model_rev IS NULL THEN 'unscored' "
        "WHEN d.attr_sun IS NULL THEN 'unsure' "
        "ELSE replace(d.attr_sun, 'sun_exposure.', '') END",
        "categorical", False,
        ("shaded", "direct_sun", "not_visible", "unsure", "unscored"), "attr_model_rev"),
    "behaviour": FeatureSpec(
        "behaviour",
        "CASE WHEN d.attr_model_rev IS NULL THEN 'unscored' "
        "WHEN d.attr_behaviour IS NULL THEN 'unsure' "
        "ELSE replace(d.attr_behaviour, 'behaviour.', '') END",
        "categorical", False,
        ("feeding", "lying", "standing", "not_visible", "unsure", "unscored"),
        "attr_model_rev"),
    # A cow the segmenter found but that stands outside every drawn count area
    # gets NO region_id, and that is the MAJORITY of them: 53.8% of the corpus on
    # 2026-09-18, and 100% on two whole days whose areas were never drawn. This
    # used to be `drop_null=True`, so a breakdown by count area silently reported
    # 4 cows where the map beside it drew 15 — a discrepancy indistinguishable
    # from a bug, and on those two days a breakdown of nothing at all.
    #
    # Now they are their own bucket. The totals reconcile with every other
    # surface, and the share of the herd that no count area covers becomes a
    # number on the screen instead of a footnote — which is the actionable part,
    # since the fix for it is to draw more areas.
    "region": FeatureSpec(
        "region", f"coalesce(d.region_id, '{REGION_NONE}')", "categorical", False,
        None, "region_id"),
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
