"""Render an instance-segmentation overlay image for the dashboard seg view."""
from __future__ import annotations

from pathlib import Path

import cv2
import numpy as np

from .base import Instance
from .geometry import AP10K_SKELETON

_PALETTE = [
    (66, 135, 245), (245, 197, 66), (66, 245, 152), (245, 66, 167),
    (155, 66, 245), (245, 111, 66), (66, 245, 236), (160, 245, 66),
]

# posture -> keypoint colour (BGR): standing green, lying amber, grazing cyan,
# unknown grey (also the fallback for None / missing).
_POSE_COLOR = {
    "standing": (80, 220, 80),
    "lying": (40, 170, 245),
    "grazing": (245, 200, 40),
    "unknown": (150, 150, 150),
}
_POSE_DEFAULT = (150, 150, 150)


def render_overlay(image_bgr: np.ndarray, instances: list[Instance], out_path: str) -> str:
    canvas = image_bgr.copy()
    for i, inst in enumerate(instances):
        color = _PALETTE[i % len(_PALETTE)]
        if inst.mask is not None:
            colored = np.zeros_like(canvas)
            colored[inst.mask] = color
            canvas = cv2.addWeighted(canvas, 1.0, colored, 0.45, 0)
            cnts, _ = cv2.findContours(inst.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            cv2.drawContours(canvas, cnts, -1, color, 2)
        # masks only: bounding boxes, score/posture labels intentionally omitted
        gx, gy = int(inst.ground_px[0]), int(inst.ground_px[1])
        cv2.circle(canvas, (gx, gy), 4, (0, 0, 255), -1)  # ground-contact point
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(out_path, canvas)
    return out_path


def render_overlay_from_polygons(
    image_bgr: np.ndarray,
    items: "list[tuple[list | None, tuple[float, float] | None, bool]]",
    out_path: str,
) -> str:
    """Repaint an overlay from STORED outlines instead of live mask rasters.

    `items` is one entry per detection ON THIS FRAME, **in the same order the
    segmenter produced them** (detection_id ascending), as
    `(polygon | None, ground_point | None, draw)`. Order is load-bearing: the
    palette is indexed positionally, so a skipped detection must still consume
    its index — otherwise hiding one animal recolours every animal after it, on
    every frame, and the whole corpus shimmers. Hence `draw` as a flag and a
    `continue`, never a pre-filtered list.

    `draw=False` means the crop model said not-a-cow. Nothing is painted for it:
    no fill, no outline, and no ground dot either — a lone red dot hovering where
    a mask used to be is still "showing something there".

    This is deliberately a SECOND renderer rather than a parameter on
    `render_overlay`: that one consumes `Instance` objects carrying full boolean
    rasters that exist only inside a segmentation pass and are never persisted.
    This one runs from the database, months later, with no GPU and no segmenter —
    which is the entire point, because the cow/not-cow verdict does not exist yet
    when `segment` first paints the frame.

    FIDELITY: `mask_poly` holds the LARGEST external contour only
    (`mask_to_polygon`), so a cow split by an occluding panel comes back as its
    bigger half. `mask_parts` records how many there were. Re-running the
    segmenter is the only way to recover the rest, and it costs hours per corpus
    against minutes for this.
    """
    canvas = image_bgr.copy()
    for i, (poly, ground, draw) in enumerate(items):
        color = _PALETTE[i % len(_PALETTE)]   # positional: index consumed even if skipped
        if not draw:
            continue
        if poly:
            pts = np.asarray(poly, dtype=np.int32).reshape(-1, 1, 2)
            colored = np.zeros_like(canvas)
            cv2.fillPoly(colored, [pts], color)
            canvas = cv2.addWeighted(canvas, 1.0, colored, 0.45, 0)
            cv2.polylines(canvas, [pts], True, color, 2)
        if ground is not None:
            cv2.circle(canvas, (int(ground[0]), int(ground[1])), 4, (0, 0, 255), -1)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    # `imwrite` REPORTS failure, it does not raise: a read-only file, a full
    # disk, or a Windows sharing violation while a FileResponse still holds the
    # JPEG open all return False and log to stderr. The caller stamps
    # `frames.overlay_rev` on the strength of this call, and a frame stamped
    # current is never selected again — so a swallowed False is a frame frozen
    # on the old model's masks until someone thinks to pass force=True.
    if not cv2.imwrite(out_path, canvas):
        raise OSError(f"could not write overlay {out_path}")
    return out_path


def render_pose_overlay(image_bgr: np.ndarray, instances: list[Instance], out_path: str,
                        min_kpt_conf: float = 0.3) -> str:
    """Frame with AP-10K skeletons drawn per instance, coloured by posture.

    Baked as a separate image so the dashboard can toggle it via `kind=pose`
    (mirrors the `overlay`/`raw` served images). Skips instances without pose."""
    canvas = image_bgr.copy()
    for inst in instances:
        kpts = inst.keypoints
        if kpts is None:
            continue
        color = _POSE_COLOR.get(inst.posture, _POSE_DEFAULT)
        for a, b in AP10K_SKELETON:
            if kpts[a, 2] >= min_kpt_conf and kpts[b, 2] >= min_kpt_conf:
                pa = (int(kpts[a, 0]), int(kpts[a, 1]))
                pb = (int(kpts[b, 0]), int(kpts[b, 1]))
                cv2.line(canvas, pa, pb, color, 2, cv2.LINE_AA)
        for x, y, c in kpts:
            if c >= min_kpt_conf:
                cv2.circle(canvas, (int(x), int(y)), 4, color, -1, cv2.LINE_AA)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(out_path, canvas)
    return out_path
