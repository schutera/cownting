"""Numpy-only unit tests for the site-wide fence polygon.

No pytest. Run either way:
    .venv/bin/python -m tests.test_fence
    .venv/bin/python tests/test_fence.py

Prints each check and a final PASS; sys.exit(1) on the first failure.
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np

# Allow `python tests/test_fence.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.calib.fence import (  # noqa: E402
    close_ring,
    load_fence,
    point_in_polygon,
    save_fence,
)

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


# --------------------------------------------------------------------------- 1: close_ring
def test_close_ring():
    tri = [[0, 0], [10, 0], [5, 8]]
    ring = close_ring(tri)
    check("close_ring: first == last", ring[0] == ring[-1], f"{ring[0]} vs {ring[-1]}")
    check("close_ring: one closing vertex added", len(ring) == len(tri) + 1,
          f"len {len(ring)}")
    check("close_ring: corners preserved in order", ring[:-1] == [[0.0, 0.0], [10.0, 0.0], [5.0, 8.0]])

    # Idempotent: closing an already-closed ring keeps exactly one duplicate.
    check("close_ring: idempotent", close_ring(ring) == ring, str(close_ring(ring)))

    # Redundant trailing duplicates collapse to a single closure.
    doubled = tri + [[0, 0], [0, 0]]
    check("close_ring: collapses extra closures", close_ring(doubled) == ring)

    # Degenerate (< 3 vertices) passes through unchanged.
    check("close_ring: <3 left open", close_ring([[1, 1], [2, 2]]) == [[1.0, 1.0], [2.0, 2.0]])


# --------------------------------------------------------------------------- 2: save/load round-trip
def test_round_trip():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "fence.json")
        save_fence(path, [[0, 0], [100, 0], [100, 100], [0, 100]])
        poly = load_fence(path)
        check("round-trip: loads", poly is not None)
        check("round-trip: stored closed (first == last)", poly[0] == poly[-1],
              f"{poly[0]} vs {poly[-1]}")
        check("round-trip: n corners + closure", len(poly) == 5, f"len {len(poly)}")

    # No file -> None (a missing fence must not break localize).
    check("round-trip: missing file -> None", load_fence("/no/such/fence.json") is None)


# --------------------------------------------------------------------------- 3: point_in_polygon
def test_point_in_polygon():
    square = close_ring([[0, 0], [10, 0], [10, 10], [0, 10]])  # closed ring
    pts = np.array([
        [5, 5],       # inside
        [-1, 5],      # left, outside
        [11, 5],      # right, outside
        [5, 20],      # above, outside
        [np.nan, 3],  # non-finite -> outside
    ])
    inside = point_in_polygon(pts, square)
    check("pip: inside point True", bool(inside[0]))
    check("pip: outside points False", not inside[1] and not inside[2] and not inside[3])
    check("pip: NaN -> False", not inside[4])
    # Closure vertex must not change the answer vs an open polygon.
    open_sq = [[0, 0], [10, 0], [10, 10], [0, 10]]
    check("pip: closed == open result",
          bool(point_in_polygon([[5, 5]], square)[0]) == bool(point_in_polygon([[5, 5]], open_sq)[0]))


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_fence ===")
    test_close_ring()
    test_round_trip()
    test_point_in_polygon()
    print("==================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
