"""Numpy-only unit tests for solar-panel centre-line shelter bands.

No pytest. Run either way:
    .venv/bin/python -m tests.test_panels
    .venv/bin/python tests/test_panels.py

Prints each check and a final PASS; sys.exit(1) on the first failure.
"""
from __future__ import annotations

import os
import sys
import tempfile

import numpy as np

# Allow `python tests/test_panels.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.scene.panels import (  # noqa: E402
    _distance_to_polyline,
    assign_panels,
    camera_panels,
    load_panels,
    save_panels,
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


# --------------------------------------------------------------------------- 1: save/load round-trip
def test_round_trip():
    # Centre lines are OPEN polylines (3 points, NOT a closed ring).
    panels = {
        "ortho": [{"id": "A", "centerline": [[0, 5], [5, 5], [10, 5]]}],
        "cameras": {
            "camera_01": [{"id": "A", "centerline": [[0, 4], [5, 6], [10, 4]], "width": 4}]
        },
    }
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "sub", "panels.json")
        save_panels(path, panels)
        loaded = load_panels(path)
        check("round-trip: loads", loaded is not None)
        check("round-trip: ortho id preserved", loaded["ortho"][0]["id"] == "A")
        o_line = loaded["ortho"][0]["centerline"]
        check("round-trip: ortho line OPEN (first != last)", o_line[0] != o_line[-1],
              f"{o_line[0]} vs {o_line[-1]}")
        check("round-trip: ortho pts preserved (no closure added)", len(o_line) == 3,
              f"len {len(o_line)}")
        check("round-trip: ortho coords intact", o_line == [[0.0, 5.0], [5.0, 5.0], [10.0, 5.0]],
              str(o_line))
        cam = camera_panels(loaded, "camera_01")
        check("round-trip: camera panels present", len(cam) == 1 and cam[0]["id"] == "A")
        c_line = cam[0]["centerline"]
        check("round-trip: camera line OPEN (first != last)", c_line[0] != c_line[-1],
              f"{c_line[0]} vs {c_line[-1]}")
        check("round-trip: camera width preserved", cam[0]["width"] == 4, str(cam[0].get("width")))
        check("round-trip: camera_panels unknown cam -> []",
              camera_panels(loaded, "nope") == [])

    # No file -> None (a missing panels file must not break localize).
    check("round-trip: missing file -> None", load_panels("/no/such/panels.json") is None)


# --------------------------------------------------------------------------- 2: lenient loading
def test_lenient_loading():
    # A legacy `polygon`-only entry and a well-formed centre line coexist; the save
    # must not crash and must not close the ring.
    panels = {
        "ortho": [{"id": "L", "polygon": [[0, 0], [10, 0], [10, 10]]}],  # legacy, no centerline
        "cameras": {
            "camera_01": [
                {"id": "A", "centerline": [[0, 5], [10, 5]], "width": 4},
                {"id": "bad"},  # no centerline at all
            ]
        },
    }
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "panels.json")
        save_panels(path, panels)  # must not raise
        loaded = load_panels(path)
        check("lenient: saved+loaded without crash", loaded is not None)
        # Legacy polygon coerced into an OPEN centerline (not closed).
        legacy = loaded["ortho"][0]["centerline"]
        check("lenient: legacy polygon -> open centerline", legacy[0] != legacy[-1],
              str(legacy))
        cam = camera_panels(loaded, "camera_01")
        check("lenient: good camera panel survives", cam[0]["id"] == "A" and cam[0]["width"] == 4)
        check("lenient: centerline-less entry -> empty line",
              cam[1]["centerline"] == [], str(cam[1].get("centerline")))

    # A corrupt (non-dict) file -> None, not a crash.
    with tempfile.TemporaryDirectory() as d:
        bad = os.path.join(d, "bad.json")
        with open(bad, "w") as f:
            f.write("[1, 2, 3]")
        check("lenient: non-dict file -> None", load_panels(bad) is None)


# --------------------------------------------------------------------------- 3: _distance_to_polyline
def test_distance_to_polyline():
    # Horizontal open line from (0,5) to (10,5).
    line = [[0, 5], [10, 5]]
    pts = np.array([
        [5, 5],    # on the line -> 0
        [5, 7],    # 2 above
        [5, 2],    # 3 below
        [-4, 5],   # off the left end -> 4 (open: no wraparound)
    ])
    d = _distance_to_polyline(pts, line)
    check("dist: on-line -> 0", abs(d[0]) < 1e-9, str(d[0]))
    check("dist: 2 above -> 2", abs(d[1] - 2.0) < 1e-9, str(d[1]))
    check("dist: 3 below -> 3", abs(d[2] - 3.0) < 1e-9, str(d[2]))
    check("dist: past open end -> endpoint dist 4", abs(d[3] - 4.0) < 1e-9, str(d[3]))
    # Degenerate lines: empty -> inf, single point -> point distance.
    check("dist: empty line -> inf", np.isinf(_distance_to_polyline(np.array([[1, 1]]), []))[0])
    check("dist: single-point line -> point dist",
          abs(_distance_to_polyline(np.array([[3, 4]]), [[0, 0]])[0] - 5.0) < 1e-9)


# --------------------------------------------------------------------------- 4: assign_panels
def test_assign_panels():
    # Known HORIZONTAL centre line (0,5) -> (10,5), band width 4 (half = 2).
    panels = {
        "ortho": [],
        "cameras": {
            "camera_01": [{"id": "A", "centerline": [[0, 5], [10, 5]], "width": 4}]
        },
    }
    margin = 0.5
    pts = np.array([
        [5, 5],       # dist 0: under, not boundary, id A
        [5, 6.7],     # dist 1.7: inside band (<=half) but within margin of edge -> boundary + id A
        [5, 7.2],     # dist 2.2: just past the band edge, within margin -> boundary, no id
        [5, 20],      # dist 15: clearly outside -> all False/None
        [np.nan, 5],  # non-finite -> all False/None
    ])
    res = assign_panels(pts, "camera_01", panels, margin_px=margin)
    under, boundary, pid = res["under_panel"], res["boundary"], res["panel_id"]

    check("assign: centre point -> under True", bool(under[0]))
    check("assign: centre point -> boundary False", not bool(boundary[0]))
    check("assign: centre point -> panel_id 'A'", pid[0] == "A", str(pid[0]))

    check("assign: inside-edge -> under False (within margin of edge)", not bool(under[1]))
    check("assign: inside-edge -> boundary True", bool(boundary[1]))
    check("assign: inside-edge -> panel_id 'A' (dist <= half)", pid[1] == "A", str(pid[1]))

    check("assign: just-past-edge -> under False", not bool(under[2]))
    check("assign: just-past-edge -> boundary True", bool(boundary[2]))
    check("assign: just-past-edge -> panel_id None (dist > half)", pid[2] is None, str(pid[2]))

    check("assign: outside -> under False", not bool(under[3]))
    check("assign: outside -> boundary False", not bool(boundary[3]))
    check("assign: outside -> panel_id None", pid[3] is None, str(pid[3]))

    check("assign: NaN -> under False", not bool(under[4]))
    check("assign: NaN -> boundary False", not bool(boundary[4]))
    check("assign: NaN -> panel_id None", pid[4] is None, str(pid[4]))

    # margin_px = 0 -> the centre point is under, never boundary; the exact edge is boundary.
    res0 = assign_panels(np.array([[5, 5], [5, 7]]), "camera_01", panels, margin_px=0.0)
    check("assign: margin 0 centre under", bool(res0["under_panel"][0]))
    check("assign: margin 0 centre not boundary", not bool(res0["boundary"][0]))
    check("assign: margin 0 exact edge -> under True (dist == half)", bool(res0["under_panel"][1]))
    check("assign: margin 0 exact edge -> boundary True", bool(res0["boundary"][1]))


# --------------------------------------------------------------------------- 5: edge cases
def test_assign_edge_cases():
    panels = {
        "ortho": [],
        "cameras": {
            "camera_01": [{"id": "A", "centerline": [[0, 5], [10, 5]], "width": 4}]
        },
    }
    pts = np.array([[5, 5], [5, 20]])

    # A camera with no panels -> all False/None.
    empty = assign_panels(pts, "camera_99", panels)
    check("edge: no-panels camera -> none under/boundary",
          not empty["under_panel"].any() and not empty["boundary"].any())
    check("edge: no-panels camera -> all panel_id None",
          all(x is None for x in empty["panel_id"]))

    # A panel with < 2 centre-line points is skipped (needs a segment).
    one_pt = {"ortho": [], "cameras": {"camera_01": [{"id": "A", "centerline": [[5, 5]], "width": 4}]}}
    r = assign_panels(pts, "camera_01", one_pt)
    check("edge: <2 centreline pts skipped", not r["under_panel"].any())

    # A panel with width <= 0 is skipped.
    zero_w = {"ortho": [], "cameras": {"camera_01": [{"id": "A", "centerline": [[0, 5], [10, 5]], "width": 0}]}}
    r = assign_panels(pts, "camera_01", zero_w)
    check("edge: width 0 skipped", not r["under_panel"].any() and all(x is None for x in r["panel_id"]))

    # Empty point set -> empty arrays, no crash.
    r = assign_panels(np.empty((0, 2)), "camera_01", panels)
    check("edge: empty points -> empty under", len(r["under_panel"]) == 0)


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_panels ===")
    test_round_trip()
    test_lenient_loading()
    test_distance_to_polyline()
    test_assign_panels()
    test_assign_edge_cases()
    print("===================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
