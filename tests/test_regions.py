"""Unit tests for cownting.scene.regions.

No pytest. Run either way:
    .venv/bin/python -m tests.test_regions
    .venv/bin/python tests/test_regions.py

Prints each check and a final PASS; sys.exit(1) on the first failure.

Covers point_in_polygon (inside/outside/on-vertex/batch) and assign_regions
(composite id, first-match-wins on overlap, None for outside and non-finite rows).
"""
from __future__ import annotations

import os
import sys

import numpy as np

# Allow `python tests/test_regions.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.scene import regions  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1


# A unit square with corners (0,0)-(10,10).
SQUARE = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]


# --------------------------------------------------------------------------- 1: point_in_polygon
def test_point_in_polygon_single():
    inside = regions.point_in_polygon([5.0, 5.0], SQUARE)
    check("point_in_polygon: interior point inside", bool(inside[0]), str(inside))

    outside = regions.point_in_polygon([15.0, 5.0], SQUARE)
    check("point_in_polygon: exterior point outside", not bool(outside[0]), str(outside))

    # Negative coords also clearly outside.
    neg = regions.point_in_polygon([-1.0, -1.0], SQUARE)
    check("point_in_polygon: negative point outside", not bool(neg[0]), str(neg))


def test_point_in_polygon_on_vertex():
    # On-vertex/edge behaviour is deterministic for the even-odd ray cast; we
    # only assert it returns a single well-formed boolean (no crash / no NaN).
    res = regions.point_in_polygon([0.0, 0.0], SQUARE)
    check("point_in_polygon: on-vertex returns 1 bool", res.shape == (1,) and res.dtype == bool,
          f"shape={res.shape} dtype={res.dtype}")


def test_point_in_polygon_batch():
    pts = np.array([
        [5.0, 5.0],    # inside
        [1.0, 1.0],    # inside
        [15.0, 5.0],   # outside (right)
        [5.0, 15.0],   # outside (above)
        [-5.0, 5.0],   # outside (left)
    ])
    res = regions.point_in_polygon(pts, SQUARE)
    check("point_in_polygon: batch shape (5,)", res.shape == (5,), str(res.shape))
    expected = [True, True, False, False, False]
    check("point_in_polygon: batch membership correct",
          list(res) == expected, f"got={list(res)} want={expected}")


# --------------------------------------------------------------------------- 2: assign_regions
def test_assign_regions_composite_id():
    areas = [{"id": "north", "name": "North", "camera_polygon": SQUARE, "ortho_polygon": []}]
    pts = np.array([[5.0, 5.0], [50.0, 50.0]])  # inside, outside
    got = regions.assign_regions(pts, areas, "camera_01")
    check("assign_regions: inside -> composite id",
          got[0] == "camera_01::north", str(got))
    check("assign_regions: outside -> None", got[1] is None, str(got))


def test_assign_regions_first_match_wins():
    # Two overlapping polygons; the point (5,5) is inside both. First in the
    # list must win.
    big = [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0], [0.0, 10.0]]
    also = [[0.0, 0.0], [8.0, 0.0], [8.0, 8.0], [0.0, 8.0]]
    areas = [
        {"id": "first", "name": "First", "camera_polygon": big, "ortho_polygon": []},
        {"id": "second", "name": "Second", "camera_polygon": also, "ortho_polygon": []},
    ]
    got = regions.assign_regions(np.array([[5.0, 5.0]]), areas, "cam")
    check("assign_regions: first-match-wins on overlap",
          got[0] == "cam::first", str(got))

    # Reverse the order -> the other area wins for the same point.
    got2 = regions.assign_regions(np.array([[5.0, 5.0]]), list(reversed(areas)), "cam")
    check("assign_regions: order determines winner",
          got2[0] == "cam::second", str(got2))


def test_assign_regions_non_finite_and_empty():
    areas = [{"id": "a", "name": "A", "camera_polygon": SQUARE, "ortho_polygon": []}]
    pts = np.array([
        [5.0, 5.0],                 # inside -> cam::a
        [np.nan, 5.0],              # non-finite -> None
        [5.0, np.inf],              # non-finite -> None
        [-np.inf, -np.inf],         # non-finite -> None
        [50.0, 50.0],               # outside -> None
    ])
    got = regions.assign_regions(pts, areas, "cam")
    check("assign_regions: finite inside assigned", got[0] == "cam::a", str(got))
    check("assign_regions: nan row -> None", got[1] is None, str(got))
    check("assign_regions: inf row -> None", got[2] is None, str(got))
    check("assign_regions: -inf row -> None", got[3] is None, str(got))
    check("assign_regions: outside row -> None", got[4] is None, str(got))

    # No areas for this camera -> all None, correct length, no crash.
    none_res = regions.assign_regions(pts, [], "cam")
    check("assign_regions: no areas -> all None, right length",
          len(none_res) == 5 and all(r is None for r in none_res), str(none_res))


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_regions ===")
    test_point_in_polygon_single()
    test_point_in_polygon_on_vertex()
    test_point_in_polygon_batch()
    test_assign_regions_composite_id()
    test_assign_regions_first_match_wins()
    test_assign_regions_non_finite_and_empty()
    print("====================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
