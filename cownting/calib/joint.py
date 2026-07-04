"""Multi-camera joint calibration: re-fit every camera together, coupled through
shared landmarks, so overlapping views agree in the orthophoto.

Each camera's warp maps cam px -> ortho px (see warp.fit_calibration). Two kinds
of shared landmark couple the cameras:

  * fence corners — when cameras trace the same fence corner, their fence-link
    ortho vertices snap to the same polygon point (frontend Phase 2). Clustered
    here; anchored to the polygon.
  * tie points — the same physical GROUND feature clicked in >= 2 cameras
    (frontend cross-camera tab). No known ortho position: a *free* landmark whose
    location is the multi-camera consensus.

Coordinate-descent bundle adjustment: (1) place each landmark at the
quality-weighted consensus of where the cameras project it (good cameras — low
reproj — weigh more, so an off camera is pulled toward the accurate ones without
dragging them back), then (2) refit each camera to its own anchors + the landmark
targets. Repeat. numpy-only; reuses the robust per-camera fitter.
"""
from __future__ import annotations

from itertools import combinations

import numpy as np

from .warp import apply_transform, fence_lines_to_pairs, fit_calibration


def _polygon_scale(records) -> float:
    """Diagonal of the ortho-vertex bounding box — sets the clustering tolerance."""
    if not records:
        return 1.0
    o = np.array([r[2] for r in records], dtype=np.float64)
    span = o.max(axis=0) - o.min(axis=0)
    return float(np.hypot(span[0], span[1])) or 1.0


def _cluster_corners(records, eps: float):
    """Greedily group fence vertices by ortho proximity (snapped corners coincide).

    `records` = [(cam, cam_px np(2,), ortho np(2,)), ...].
    Returns [{"snap": np(2,), "by_cam": {cam: [cam_px, ...]}}].
    """
    clusters: list = []
    for cam, cam_px, ortho in records:
        best, best_d = None, eps
        for cl in clusters:
            d = float(np.hypot(*(cl["_sum"] / cl["_n"] - ortho)))
            if d < best_d:
                best, best_d = cl, d
        if best is None:
            best = {"_sum": np.zeros(2), "_n": 0, "by_cam": {}}
            clusters.append(best)
        best["_sum"] += ortho
        best["_n"] += 1
        best["by_cam"].setdefault(cam, []).append(cam_px)
    for cl in clusters:
        cl["snap"] = cl["_sum"] / cl["_n"]
    return clusters


def _cam_pred(model, pxs) -> np.ndarray:
    """Where a camera projects its sighting(s) of a landmark: mean ortho px."""
    return apply_transform(model, np.array(pxs, dtype=np.float64)).mean(axis=0)


def joint_calibrate(cams, inputs, image_sizes, tie_points=None, *,
                    w_fence: float = 2.0, iters: int = 4, robust: bool = True):
    """Jointly re-fit `cams` coupled through shared fence corners and tie points.

    `inputs[cam]`  = {lines, center_pairs, ground_pairs, fence_lines, h_center}
    `image_sizes[cam]` = (w, h)
    `tie_points` = [[{"camera", "pt":[x,y]}, ...], ...] cross-camera ground sightings.

    Returns (results, global_diag):
      results[cam] = {"model", "diag", "n_shared"}  (only cams that fit)
      global_diag  = {cross_camera_px, max_cross_camera_px, n_shared_corners, n_pairs}
    """
    tie_points = tie_points or []

    # Panel footprints are height-0 ground anchors that couple cameras exactly like
    # fence corners: same [cam_ring, ortho_ring] shape, snapped to shared ortho panel
    # corners. Merge each camera's panel_lines into its fence_lines so ALL the fence
    # coupling machinery (solo fit, own-anchor guard, corner clustering, refit) applies
    # to panels too — the point of "use the panel areas to calibrate all cameras".
    inputs = {
        c: {**v, "fence_lines": (v.get("fence_lines") or []) + (v.get("panel_lines") or [])}
        for c, v in inputs.items()
    }

    def fit_one(cam, ground_pairs, fence_lines):
        i = inputs[cam]
        return fit_calibration(i.get("lines") or [], i.get("center_pairs") or [],
                               ground_pairs, i.get("h_center"), image_sizes[cam],
                               fence_lines=fence_lines, robust=robust,
                               ground_lines=i.get("ground_lines") or [])

    # 1. Which cameras calibrate on their own stored inputs?
    fittable, results = [], {}
    for cam in cams:
        try:
            model, diag = fit_one(cam, inputs[cam].get("ground_pairs") or [],
                                  inputs[cam].get("fence_lines") or [])
        except Exception:  # noqa: BLE001 - a camera that can't fit is left out
            continue
        fittable.append(cam)
        results[cam] = {"model": model, "diag": diag, "n_shared": 0}
    fitset = set(fittable)

    # Keep the solo fit + each camera's OWN absolute anchors (clicked ground + its
    # fence vertices at their drawn ortho), so we can guarantee the joint result
    # never ends up worse than solo for any camera (a tie point must not degrade a
    # camera — see the guard after the descent).
    solo = {c: {"model": results[c]["model"], "diag": results[c]["diag"]} for c in fittable}
    own = {
        c: [[list(p[0]), list(p[1])] for p in (inputs[c].get("ground_pairs") or [])]
        + [[list(pr[0]), list(pr[1])] for pr in fence_lines_to_pairs(inputs[c].get("fence_lines") or [])]
        for c in fittable
    }

    # 2. Fence vertices -> clusters. Per camera, split into FIXED ground anchors
    #    (clicked ground + single-camera fence vertices at their snapped ortho) and
    #    observations of SHARED landmarks (index into `landmarks`).
    fence_records = []
    for cam in fittable:
        for pair in fence_lines_to_pairs(inputs[cam].get("fence_lines") or []):
            fence_records.append((cam, np.array(pair[0], float), np.array(pair[1], float)))
    eps = max(2.0, 0.005 * _polygon_scale(fence_records))
    clusters = _cluster_corners(fence_records, eps)

    landmarks: list = []  # {"by_cam": {cam:[px]}, "anchor": np|None, "w_reg": float}
    cam_fixed = {c: [list(p) for p in (inputs[c].get("ground_pairs") or [])] for c in fittable}
    cam_obs = {c: [] for c in fittable}  # (cam_px np, landmark_idx)

    for cl in clusters:
        if len(cl["by_cam"]) >= 2:  # shared fence corner -> anchored landmark
            idx = len(landmarks)
            landmarks.append({"by_cam": {c: list(pts) for c, pts in cl["by_cam"].items()},
                              "anchor": cl["snap"].copy(), "w_reg": w_fence})
            for c, pts in cl["by_cam"].items():
                for p in pts:
                    cam_obs[c].append((p, idx))
        else:  # single-camera fence vertex -> fixed ground anchor at its snap
            for c, pts in cl["by_cam"].items():
                for p in pts:
                    cam_fixed[c].append([[float(p[0]), float(p[1])],
                                         [float(cl["snap"][0]), float(cl["snap"][1])]])

    # 3. Tie points -> free landmarks (consensus location).
    for tp in tie_points:
        by_cam: dict = {}
        for o in tp:
            cam = o.get("camera")
            if cam in fitset:
                by_cam.setdefault(cam, []).append(np.array(o["pt"], float))
        if len(by_cam) >= 2:
            idx = len(landmarks)
            landmarks.append({"by_cam": by_cam, "anchor": None, "w_reg": 0.0})
            for c, pxs in by_cam.items():
                for px in pxs:
                    cam_obs[c].append((px, idx))

    for c in fittable:
        results[c]["n_shared"] = len({idx for (_, idx) in cam_obs[c]})

    # 4. Quality-weighted consensus: good cameras (low reproj) weigh more, so a
    #    shared point pulls an off camera toward the accurate ones.
    def consensus(lm) -> np.ndarray:
        num, den = np.zeros(2), 0.0
        for c, pxs in lm["by_cam"].items():
            w = 1.0 / (float(results[c]["diag"]["reproj_error"]) + 1.0)
            num += w * _cam_pred(results[c]["model"], pxs)
            den += w
        if lm["anchor"] is not None:
            num += lm["w_reg"] * lm["anchor"]
            den += lm["w_reg"]
        return num / den if den > 0 else (lm["anchor"] if lm["anchor"] is not None else num)

    X = [lm["anchor"].copy() if lm["anchor"] is not None else consensus(lm) for lm in landmarks]

    # 5. Coordinate-descent bundle adjustment.
    for _ in range(max(1, iters) if landmarks else 0):
        for i, lm in enumerate(landmarks):
            X[i] = consensus(lm)
        for cam in fittable:
            gp = list(cam_fixed[cam]) + [
                [[float(px[0]), float(px[1])], [float(X[idx][0]), float(X[idx][1])]]
                for (px, idx) in cam_obs[cam]
            ]
            try:
                model, diag = fit_one(cam, gp, [])
                results[cam]["model"], results[cam]["diag"] = model, diag
            except Exception:  # noqa: BLE001 - keep the last good fit
                pass

    # 5b. Guard: a shared point must never make a camera WORSE on its own anchors.
    # Weak/sparsely-anchored cameras can get dragged by tie targets that conflict
    # with their own (already imperfect) points; if that happens, keep the solo fit.
    def _own_reproj(model, pairs) -> float:
        if not pairs:
            return 0.0
        cam = np.array([p[0] for p in pairs], dtype=np.float64)
        ort = np.array([p[1] for p in pairs], dtype=np.float64)
        return float(np.sqrt(((apply_transform(model, cam) - ort) ** 2).sum(axis=1)).mean())

    for cam in fittable:
        r_joint = _own_reproj(results[cam]["model"], own[cam])
        r_solo = _own_reproj(solo[cam]["model"], own[cam])
        reverted = r_joint > r_solo + 1e-6
        if reverted:  # tie points hurt this camera -> keep solo
            results[cam]["model"] = solo[cam]["model"]
            results[cam]["diag"] = solo[cam]["diag"]
        results[cam]["reverted"] = reverted
        # Report the honest OWN-anchor reproj (the joint diag mixes in tie targets,
        # which inflates it when a camera's own anchors and the ties disagree).
        d = dict(results[cam]["diag"])
        d["reproj_error"] = r_solo if reverted else r_joint
        results[cam]["diag"] = d

    # 6. Global cross-camera agreement: pairwise ortho distance at each landmark.
    dists, n_pairs = [], 0
    for lm in landmarks:
        preds = [_cam_pred(results[c]["model"], pxs) for c, pxs in lm["by_cam"].items()]
        for a, b in combinations(preds, 2):
            dists.append(float(np.hypot(*(a - b))))
            n_pairs += 1
    global_diag = {
        "cross_camera_px": float(np.mean(dists)) if dists else 0.0,
        "max_cross_camera_px": float(np.max(dists)) if dists else 0.0,
        "n_shared_corners": len(landmarks),
        "n_pairs": n_pairs,
    }
    return results, global_diag
