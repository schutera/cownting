"""Panel-area shelter model: a cow inside a panel-area polygon is 'under a panel'.

Panel areas reuse the count-area point-in-polygon test (regions.assign_regions),
exactly as pipeline.localize does. No pytest:
    .venv/bin/python -m tests.test_panel_areas
    .venv/bin/python tests/test_panel_areas.py
"""
from __future__ import annotations

import os
import sys

import numpy as np

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.scene.regions import assign_regions  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    print(f"[{'ok ' if cond else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))
    if not cond:
        _FAILED += 1


def _under_panel(pts, panel_areas, camera_id):
    """Mirror pipeline.localize's shelter derivation from panel-area polygons."""
    pids = assign_regions(np.asarray(pts, dtype=float), panel_areas, camera_id)
    return [p is not None for p in pids], pids


def test_inside_outside():
    # A 10x10 square panel area for camera_01.
    panel_areas = [{"id": "row-a", "camera_polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]}]
    pts = [
        [5, 5],       # inside  -> under panel
        [50, 50],     # outside -> open
        [0.1, 0.1],   # just inside a corner -> under panel
        [np.nan, 5],  # non-finite -> open
    ]
    under, pids = _under_panel(pts, panel_areas, "camera_01")
    check("inside point -> under_panel True", under[0] is True, str(under[0]))
    check("inside point -> panel_id composite", pids[0] == "camera_01::row-a", str(pids[0]))
    check("outside point -> under_panel False", under[1] is False, str(under[1]))
    check("outside point -> panel_id None", pids[1] is None, str(pids[1]))
    check("corner point -> under_panel True", under[2] is True, str(under[2]))
    check("NaN point -> under_panel False", under[3] is False, str(under[3]))
    check("NaN point -> panel_id None", pids[3] is None, str(pids[3]))


def test_no_panel_areas():
    # No polygons for this camera -> nothing sheltering.
    under, pids = _under_panel([[5, 5], [1, 1]], [], "camera_01")
    check("no panel areas -> none under panel", under == [False, False], str(under))
    check("no panel areas -> all ids None", all(p is None for p in pids), str(pids))


def test_first_matching_panel_wins():
    # Two overlapping panels: the first containing polygon assigns the id.
    panel_areas = [
        {"id": "a", "camera_polygon": [[0, 0], [10, 0], [10, 10], [0, 10]]},
        {"id": "b", "camera_polygon": [[5, 5], [15, 5], [15, 15], [5, 15]]},
    ]
    _, pids = _under_panel([[6, 6]], panel_areas, "camera_02")  # in both a and b
    check("overlap -> first polygon wins", pids[0] == "camera_02::a", str(pids[0]))


def main():
    print("=== test_panel_areas ===")
    test_inside_outside()
    test_no_panel_areas()
    test_first_matching_panel_wins()
    print("========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
