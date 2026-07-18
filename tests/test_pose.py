"""Unit tests for cownting.detect.geometry.posture_from_pose (4-class).

No pytest. Run either way:
    .venv/bin/python -m tests.test_pose
    .venv/bin/python tests/test_pose.py

Hermetic: no model, no footage. We synthesize AP-10K keypoint arrays (the
17-joint ViTPose animal schema) with known spine / hoof / head geometry and
assert the classifier returns standing / lying / grazing / unknown from vertical
drops normalized by bbox height. `unknown` is a first-class outcome for garbage
poses (too few confident joints, missing spine/legs) — never a wrong guess.
"""
from __future__ import annotations

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np  # noqa: E402

from cownting.detect.geometry import (  # noqa: E402
    AP10K_HEAD,
    AP10K_HOOVES,
    AP10K_TOPLINE,
    posture_from_pose,
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


def _kpts(spine_y: float, hoof_y: float, head_y: float | None = None,
          conf: float = 0.9) -> np.ndarray:
    """17x3 AP-10K keypoints: spine joints at spine_y, hooves at hoof_y, head at
    head_y (default = above the spine, i.e. head up)."""
    k = np.zeros((17, 3), dtype=float)
    k[:, 2] = conf
    for i in AP10K_TOPLINE:
        k[i, 1] = spine_y
    for i in AP10K_HOOVES:
        k[i, 1] = hoof_y
    for i in AP10K_HEAD:
        k[i, 1] = spine_y - 40 if head_y is None else head_y
    return k


def test_standing():
    # legs drop 200px below spine over a 250px bbox -> 0.8 >= stand_lie_ratio
    k = _kpts(spine_y=100, hoof_y=300)
    check("legs extended, head up -> standing",
          posture_from_pose(k, (0, 80, 200, 330)) == "standing")


def test_lying():
    # hooves tucked near the spine -> small leg-drop ratio -> lying. Threshold is
    # passed explicitly: the shipped default is 0.0 (lying deferred — no lying
    # validation data), but the mechanism must still classify below the boundary.
    k = _kpts(spine_y=100, hoof_y=130)
    check("hooves tucked near spine -> lying",
          posture_from_pose(k, (0, 40, 300, 250), stand_lie_ratio=0.40) == "lying")


def test_grazing():
    # legs extended (would be standing) BUT head dropped toward the feet
    k = _kpts(spine_y=100, hoof_y=300, head_y=260)  # head_drop=(260-100)/250=0.64
    check("legs extended, head down -> grazing",
          posture_from_pose(k, (0, 80, 200, 330)) == "grazing")


def test_grazing_beats_standing_precedence():
    # same legs as standing; only the head position flips it to grazing
    up = _kpts(spine_y=100, hoof_y=300, head_y=60)
    down = _kpts(spine_y=100, hoof_y=300, head_y=250)
    check("head up -> standing", posture_from_pose(up, (0, 80, 200, 330)) == "standing")
    check("head down -> grazing", posture_from_pose(down, (0, 80, 200, 330)) == "grazing")


def test_unknown_too_few_confident():
    # nearly all joints low-confidence -> quality gate -> unknown
    k = _kpts(spine_y=100, hoof_y=300)
    k[:, 2] = 0.1
    check("garbage (all low conf) -> unknown",
          posture_from_pose(k, (0, 80, 200, 330)) == "unknown")


def test_unknown_no_legs():
    # plenty of confident joints but no confident hooves -> unknown
    k = _kpts(spine_y=100, hoof_y=300)
    for i in AP10K_HOOVES:
        k[i, 2] = 0.05
    check("no confident legs -> unknown",
          posture_from_pose(k, (0, 80, 200, 330)) == "unknown")


def test_unknown_no_spine():
    k = _kpts(spine_y=100, hoof_y=300)
    for i in AP10K_TOPLINE:
        k[i, 2] = 0.05
    check("no confident spine -> unknown",
          posture_from_pose(k, (0, 80, 200, 330)) == "unknown")


def test_unknown_small_bbox():
    # a well-formed pose but a tiny (distant) bbox -> pose unreliable -> unknown
    k = _kpts(spine_y=100, hoof_y=300)
    check("bbox below min height -> unknown",
          posture_from_pose(k, (0, 80, 60, 180)) == "unknown")  # h=100 < 130


def test_unknown_out_of_box():
    # confident joints scattered far outside the animal's box -> garbage -> unknown
    k = _kpts(spine_y=100, hoof_y=300)
    k[:, 0] = 5000  # every joint far to the right of the bbox
    check("joints outside bbox -> unknown",
          posture_from_pose(k, (0, 80, 200, 330)) == "unknown")


def test_unknown_hooves_above_spine():
    # scrambled skeleton: hooves resolved ABOVE the back line -> anatomically
    # impossible -> unknown (this is the case that mislabels large cows as lying)
    k = _kpts(spine_y=300, hoof_y=100)  # hooves well above spine
    check("hooves above spine (scrambled) -> unknown",
          posture_from_pose(k, (0, 80, 200, 380)) == "unknown")


def test_degenerate_inputs():
    check("None keypoints -> unknown", posture_from_pose(None, (0, 0, 10, 10)) == "unknown")
    check("short array -> unknown",
          posture_from_pose(np.zeros((5, 3)), (0, 0, 10, 10)) == "unknown")
    k = _kpts(100, 300)
    check("zero-height bbox -> unknown", posture_from_pose(k, (0, 50, 10, 50)) == "unknown")


def main():
    print("=== test_pose ===")
    test_standing()
    test_lying()
    test_grazing()
    test_grazing_beats_standing_precedence()
    test_unknown_too_few_confident()
    test_unknown_no_legs()
    test_unknown_no_spine()
    test_unknown_small_bbox()
    test_unknown_out_of_box()
    test_unknown_hooves_above_spine()
    test_degenerate_inputs()
    print("=================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
