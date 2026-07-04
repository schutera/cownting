"""Stage 2 calibration: homography + division-model warp to the orthophoto."""
from .homography import compute_homography, load_all, project_points, save_homography
from .warp import (
    apply_polynomial,
    apply_transform,
    clip_to_extent,
    fence_lines_to_pairs,
    fit_calibration,
    fit_distortion,
    fit_polynomial,
    resolve_model,
    save_calibration,
    undistort,
)

__all__ = [
    "compute_homography",
    "load_all",
    "project_points",
    "save_homography",
    # 3-stage warp calibration
    "fit_calibration",
    "fence_lines_to_pairs",
    "apply_transform",
    "resolve_model",
    "clip_to_extent",
    "save_calibration",
    "fit_distortion",
    "undistort",
    "fit_polynomial",
    "apply_polynomial",
]
