"""Three-stage per-camera calibration: line-fisheye + center-plane warp + float drop.

Maps cow ground-contact camera pixels -> orthophoto pixels. Three decoupled,
individually-testable stages (see docs/plan iridescent-shimmying-hollerith.md):

1. Fisheye undistort from world-straight lines (one-parameter division model).
2. Center-plane polynomial warp (absorbs terrain slope + residual distortion).
3. Float drop from height-0 samples (pillar bases + direct ground points).

Everything is numpy-only (no scipy). Every stored model is JSON-serializable:
python lists / floats only, never numpy arrays or scalars. `np.linalg.lstsq` is
always called with `rcond=None`.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np

from .homography import load_all, project_points


# --------------------------------------------------------------------------- utils
def _as_pts(pts) -> np.ndarray:
    """Coerce an iterable of [x, y] to an (N, 2) float64 array."""
    arr = np.asarray(pts, dtype=np.float64)
    return arr.reshape(-1, 2)


def _jsonable(obj):
    """Recursively convert numpy arrays/scalars to plain python for JSON storage."""
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.generic):
        return obj.item()
    return obj


# --------------------------------------------------------------------------- stage 1: fisheye
def undistort(model: dict | None, pts) -> np.ndarray:
    """Apply the one-parameter division undistortion. None -> identity.

    Coords are normalized about center `c`, scaled by `s`; for point p,
    n = (p - c) / s, r2 = |n|^2, n_u = n / (1 + lambda * r2), back to px = c + s * n_u.
    """
    p = _as_pts(pts)
    if model is None:
        return p.copy()
    c = np.asarray(model["center"], dtype=np.float64)
    s = float(model["scale"])
    lam = float(model["lambda"])
    n = (p - c) / s
    r2 = (n ** 2).sum(axis=1, keepdims=True)
    n_u = n / (1.0 + lam * r2)
    return c + s * n_u


def _line_residual(pts: np.ndarray) -> float:
    """Sum of squared perpendicular residuals of pts to their total-least-squares line.

    TLS via PCA/SVD on centered points: the smaller singular vector is the line
    normal; residual = projection of centered points onto that normal.
    """
    if len(pts) < 2:
        return 0.0
    centered = pts - pts.mean(axis=0)
    # SVD: columns of Vt are principal directions; last row = normal (min variance)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    normal = vt[-1]
    d = centered @ normal
    return float((d ** 2).sum())


def _total_line_residual(lines_u: list[np.ndarray]) -> float:
    return sum(_line_residual(ln) for ln in lines_u)


def fit_distortion(lines, image_size) -> dict | None:
    """Fit the one-parameter division model by minimizing line-straightness residual.

    `lines`: list of lines, each a list of [x, y] camera px (>= 5 pts to be used).
    `image_size`: (w, h). Center is fixed at the image center (robust); scale
    s = 0.5 * hypot(w, h). Returns {"center":[cx,cy], "scale":s, "lambda":lam}
    or None if fewer than 1 usable line.
    """
    usable = [_as_pts(ln) for ln in lines if len(ln) >= 5]
    if len(usable) < 1:
        return None

    w, h = float(image_size[0]), float(image_size[1])
    c = np.array([w / 2.0, h / 2.0], dtype=np.float64)
    s = 0.5 * float(np.hypot(w, h))

    def residual_for(lam: float) -> float:
        model = {"center": [c[0], c[1]], "scale": s, "lambda": lam}
        lines_u = [undistort(model, ln) for ln in usable]
        return _total_line_residual(lines_u)

    # coarse grid over lambda
    grid = np.linspace(-1.5, 1.5, 200)
    vals = np.array([residual_for(float(x)) for x in grid])
    i = int(np.argmin(vals))
    best_lam = float(grid[i])

    # local golden-section refine within the neighbouring grid cells
    step = grid[1] - grid[0]
    lo = best_lam - step
    hi = best_lam + step
    gr = (np.sqrt(5.0) - 1.0) / 2.0
    a, b = lo, hi
    x1 = b - gr * (b - a)
    x2 = a + gr * (b - a)
    f1 = residual_for(x1)
    f2 = residual_for(x2)
    for _ in range(60):
        if f1 < f2:
            b, x2, f2 = x2, x1, f1
            x1 = b - gr * (b - a)
            f1 = residual_for(x1)
        else:
            a, x1, f1 = x1, x2, f2
            x2 = a + gr * (b - a)
            f2 = residual_for(x2)
        if abs(b - a) < 1e-8:
            break
    lam = float((a + b) / 2.0)
    if residual_for(best_lam) < residual_for(lam):
        lam = best_lam

    return {"center": [float(c[0]), float(c[1])], "scale": float(s), "lambda": float(lam)}


def _mean_line_residual_px(model: dict | None, lines) -> float:
    """Mean per-point perpendicular residual (px) after undistorting, across all lines."""
    usable = [_as_pts(ln) for ln in lines if len(ln) >= 5]
    total_sq = 0.0
    total_n = 0
    for ln in usable:
        pu = undistort(model, ln)
        if len(pu) < 2:
            continue
        centered = pu - pu.mean(axis=0)
        _, _, vt = np.linalg.svd(centered, full_matrices=False)
        normal = vt[-1]
        d = centered @ normal
        total_sq += float((d ** 2).sum())
        total_n += len(pu)
    if total_n == 0:
        return 0.0
    return float(np.sqrt(total_sq / total_n))


# --------------------------------------------------------------------------- stage 2: polynomial
def _poly_design(pts: np.ndarray, degree: int) -> np.ndarray:
    """Monomial design matrix in 2 (already normalized) coords.

    deg1 -> [1, x, y]                         (3 terms, affine)
    deg2 -> deg1 + [x^2, xy, y^2]             (6 terms)
    deg3 -> deg2 + [x^3, x^2 y, x y^2, y^3]   (10 terms)
    """
    x = pts[:, 0]
    y = pts[:, 1]
    cols = [np.ones_like(x), x, y]
    if degree >= 2:
        cols += [x * x, x * y, y * y]
    if degree >= 3:
        cols += [x * x * x, x * x * y, x * y * y, y * y * y]
    return np.stack(cols, axis=1)


def _n_terms(degree: int) -> int:
    return 3 if degree <= 1 else (6 if degree == 2 else 10)


def _choose_degree(n: int) -> int:
    """Pick the richest polynomial the point count supports: deg3>=10, deg2>=6, else affine."""
    if n >= 10:
        return 3
    if n >= 6:
        return 2
    return 1


def _normalize(pts: np.ndarray):
    """Center + scale points for lstsq conditioning. Returns (normalized, mean, scale)."""
    mean = pts.mean(axis=0)
    centered = pts - mean
    scale = centered.std(axis=0)
    scale = np.where(scale < 1e-9, 1.0, scale)
    return centered / scale, mean, scale


def fit_polynomial(cam_pts, ortho_pts) -> dict:
    """Fit a per-axis polynomial mapping cam_pts -> ortho_pts.

    Degree chosen by `_choose_degree` on the point count; raises ValueError if
    there are fewer points than polynomial terms. Inputs are normalized (mean +
    scale stored in the model) for conditioning.
    """
    src = _as_pts(cam_pts)
    dst = _as_pts(ortho_pts)
    if len(src) != len(dst):
        raise ValueError("cam_pts and ortho_pts must have the same length")
    degree = _choose_degree(len(src))
    nterms = _n_terms(degree)
    if len(src) < nterms:
        raise ValueError(f"need >= {nterms} pairs for a degree-{degree} polynomial, got {len(src)}")

    norm_src, mean, scale = _normalize(src)
    A = _poly_design(norm_src, degree)
    coeffs = []
    for axis in range(2):
        c, *_ = np.linalg.lstsq(A, dst[:, axis], rcond=None)
        coeffs.append(c.tolist())

    return {
        "type": "poly",
        "degree": int(degree),
        "coeffs": coeffs,
        "norm": {"mean": mean.tolist(), "scale": scale.tolist()},
    }


def apply_polynomial(model: dict, pts) -> np.ndarray:
    """Evaluate a fitted polynomial model at pts -> (N, 2)."""
    p = _as_pts(pts)
    mean = np.asarray(model["norm"]["mean"], dtype=np.float64)
    scale = np.asarray(model["norm"]["scale"], dtype=np.float64)
    degree = int(model["degree"])
    norm = (p - mean) / scale
    A = _poly_design(norm, degree)
    coeffs = np.asarray(model["coeffs"], dtype=np.float64)  # (2, nterms)
    out = A @ coeffs.T  # (N, 2)
    return out


# --------------------------------------------------------------------------- robust fitting
def _huber_weights(resid: np.ndarray, delta: float) -> np.ndarray:
    """Per-point Huber weights: 1 within `delta` px, then delta/|r| (down-weights)."""
    r = np.maximum(resid, 1e-9)
    w = np.ones_like(r)
    far = r > delta
    w[far] = delta / r[far]
    return w


def _tukey_weights(resid: np.ndarray, c: float) -> np.ndarray:
    """Tukey bisquare weights: redescending — points past `c` px get weight 0 (rejected)."""
    r = resid / c
    w = (1.0 - r ** 2) ** 2
    w[np.abs(r) >= 1.0] = 0.0
    return w


def _robust_scale(resid: np.ndarray, floor: float = 2.0) -> float:
    """MAD-based robust scale (px), floored so clean data keeps weights ~1 (≈OLS)."""
    return max(floor, 1.4826 * float(np.median(resid)))


def _lstsq_weighted(A: np.ndarray, y: np.ndarray, w: np.ndarray) -> np.ndarray:
    """Weighted least squares via row-scaling by sqrt(w)."""
    sw = np.sqrt(w)
    c, *_ = np.linalg.lstsq(A * sw[:, None], y * sw, rcond=None)
    return c


def _irls_coeffs(A: np.ndarray, dst: np.ndarray, huber_iters: int = 3,
                 tukey_iters: int = 5) -> np.ndarray:
    """Robust coeffs (2, nterms) mapping design A -> dst (N,2).

    Huber first (convex, gives a robust start), then Tukey bisquare (redescending,
    fully rejects gross misclicks). Scale is MAD-based with a floor, so a clean
    fit stays ~OLS. Weights are shared across x/y (a bad click is bad in both).
    """
    n = len(dst)
    w = np.ones(n)
    coeffs = np.zeros((2, A.shape[1]))
    for it in range(huber_iters + tukey_iters):
        coeffs = np.stack([_lstsq_weighted(A, dst[:, ax], w) for ax in range(2)], axis=0)
        resid = np.sqrt(((A @ coeffs.T - dst) ** 2).sum(axis=1))
        s = _robust_scale(resid)
        w = _huber_weights(resid, 1.345 * s) if it < huber_iters else _tukey_weights(resid, 4.685 * s)
    return coeffs


def fit_polynomial_robust(cam_pts, ortho_pts, degree: int | None = None) -> dict:
    """Robust (IRLS Huber→bisquare) version of `fit_polynomial`: same model shape.

    Misclicks are rejected; on clean data it converges to the plain LS fit.
    """
    src = _as_pts(cam_pts)
    dst = _as_pts(ortho_pts)
    if len(src) != len(dst):
        raise ValueError("cam_pts and ortho_pts must have the same length")
    if degree is None:
        degree = _choose_degree(len(src))
    nterms = _n_terms(degree)
    if len(src) < nterms:
        raise ValueError(f"need >= {nterms} pairs for a degree-{degree} polynomial, got {len(src)}")

    norm_src, mean, scale = _normalize(src)
    coeffs = _irls_coeffs(_poly_design(norm_src, degree), dst)
    return {
        "type": "poly",
        "degree": int(degree),
        "coeffs": coeffs.tolist(),
        "norm": {"mean": mean.tolist(), "scale": scale.tolist()},
    }


def _anchor_spread(pts) -> tuple[float, float]:
    """Singular values of the centered anchors: (major, minor) extent. minor≈0 ⇒ collinear."""
    p = _as_pts(pts)
    if len(p) < 2:
        return 0.0, 0.0
    s = np.linalg.svd(p - p.mean(axis=0), compute_uv=False)
    return float(s[0]), float(s[1] if len(s) > 1 else 0.0)


def _assert_not_collinear(ground_ortho: np.ndarray, from_fence_only: bool) -> float:
    """Guard against a rank-deficient ground set (the 'fence-only degenerate' case).

    Returns the spread ratio (minor/major). Raises if the ground anchors are
    effectively collinear — a straight fence alone can't pin the ground plane in
    the direction across it; you need a second orientation or center/ground points.
    """
    major, minor = _anchor_spread(ground_ortho)
    ratio = minor / major if major > 0 else 0.0
    if ratio < 1e-4:
        hint = (
            "add a second fence segment in a different orientation, or some center/ground points"
            if from_fence_only
            else "spread the ground anchors so they aren't on one line"
        )
        raise ValueError(f"ground anchors are collinear (spread ratio {ratio:.2e}) — {hint}")
    return ratio


# --------------------------------------------------------------------------- stage 3: float drop
def _fit_drop(cam_u: np.ndarray, deltas: np.ndarray) -> dict:
    """Fit a low-order polynomial mapping undistorted cam px -> 2D offset (delta).

    Degree 1 (affine, 3 terms) if 3 <= n < 6 else degree 2. The design matrix uses
    the same monomials as `_poly_design` but truncated to the linear terms for deg1.
    """
    n = len(cam_u)
    if n < 3:
        raise ValueError("need >= 3 height-0 samples to fit the float drop")
    degree = 1 if n < 6 else 2
    norm_src, mean, scale = _normalize(cam_u)
    if degree == 1:
        x = norm_src[:, 0]
        y = norm_src[:, 1]
        A = np.stack([np.ones_like(x), x, y], axis=1)  # 3 terms
    else:
        A = _poly_design(norm_src, 2)  # 6 terms
    coeffs = []
    for axis in range(2):
        c, *_ = np.linalg.lstsq(A, deltas[:, axis], rcond=None)
        coeffs.append(c.tolist())
    return {
        "type": "drop",
        "degree": int(degree),
        "coeffs": coeffs,
        "norm": {"mean": mean.tolist(), "scale": scale.tolist()},
    }


def _fit_drop_robust(cam_u: np.ndarray, deltas: np.ndarray) -> dict:
    """Robust (IRLS Huber→bisquare) version of `_fit_drop`; ~plain LS on clean data."""
    n = len(cam_u)
    if n < 3:
        raise ValueError("need >= 3 height-0 samples to fit the float drop")
    degree = 1 if n < 6 else 2
    norm_src, mean, scale = _normalize(cam_u)
    if degree == 1:
        x, y = norm_src[:, 0], norm_src[:, 1]
        A = np.stack([np.ones_like(x), x, y], axis=1)
    else:
        A = _poly_design(norm_src, 2)
    coeffs = _irls_coeffs(A, deltas)
    return {
        "type": "drop",
        "degree": int(degree),
        "coeffs": coeffs.tolist(),
        "norm": {"mean": mean.tolist(), "scale": scale.tolist()},
    }


def _apply_drop(model: dict, pts) -> np.ndarray:
    p = _as_pts(pts)
    mean = np.asarray(model["norm"]["mean"], dtype=np.float64)
    scale = np.asarray(model["norm"]["scale"], dtype=np.float64)
    norm = (p - mean) / scale
    if int(model["degree"]) == 1:
        x = norm[:, 0]
        y = norm[:, 1]
        A = np.stack([np.ones_like(x), x, y], axis=1)
    else:
        A = _poly_design(norm, 2)
    coeffs = np.asarray(model["coeffs"], dtype=np.float64)
    return A @ coeffs.T


# --------------------------------------------------------------------------- full calibration
def _extent_of(ortho_targets: np.ndarray, pad_frac: float = 0.20) -> list[float]:
    """Axis-aligned bbox of ortho targets, padded by `pad_frac` on each side."""
    xmin = float(ortho_targets[:, 0].min())
    ymin = float(ortho_targets[:, 1].min())
    xmax = float(ortho_targets[:, 0].max())
    ymax = float(ortho_targets[:, 1].max())
    dx = (xmax - xmin) * pad_frac
    dy = (ymax - ymin) * pad_frac
    return [xmin - dx, ymin - dy, xmax + dx, ymax + dy]


def _split_pairs(pairs):
    """Split [[[camx,camy],[ox,oy]], ...] into (cam Nx2, ortho Nx2)."""
    if not pairs:
        return np.empty((0, 2)), np.empty((0, 2))
    cam = np.array([[p[0][0], p[0][1]] for p in pairs], dtype=np.float64)
    ortho = np.array([[p[1][0], p[1][1]] for p in pairs], dtype=np.float64)
    return cam, ortho


def fence_lines_to_pairs(fence_lines, kind: str = "fence"):
    """Expand fence-line correspondences into ground point-pairs.

    Each correspondence is `[cam_polyline, ortho_polyline]` tracing the same
    physical fence segment; vertex i on the camera side matches vertex i on the
    ortho side (same order). The fence stands on the ground (height 0), so every
    matched vertex is a height-0 ground anchor. Returns
    `[[[camx,camy],[ox,oy]], ...]`. A correspondence with < 2 vertices is
    skipped; mismatched vertex counts raise ValueError. `kind` only labels the
    error message (panel footprints reuse this same expansion).
    """
    pairs: list = []
    for k, corr in enumerate(fence_lines or []):
        cam = _as_pts(corr[0])
        ortho = _as_pts(corr[1])
        if len(cam) != len(ortho):
            raise ValueError(
                f"{kind} correspondence #{k + 1}: camera and ortho polylines must have "
                f"the same vertex count (got {len(cam)} vs {len(ortho)})"
            )
        if len(cam) < 2:
            continue
        for i in range(len(cam)):
            pairs.append(
                [[float(cam[i, 0]), float(cam[i, 1])],
                 [float(ortho[i, 0]), float(ortho[i, 1])]]
            )
    return pairs


# --------------------------------------------------------------------------- ground lines
def _fit_line_tls(pts) -> tuple:
    """Total-least-squares infinite line through pts -> (a, b, c) with a^2+b^2=1
    and a*x + b*y + c = 0 on the line ((a, b) is the unit normal)."""
    p = _as_pts(pts)
    ctr = p.mean(axis=0)
    _, _, vt = np.linalg.svd(p - ctr, full_matrices=False)
    a, b = float(vt[-1, 0]), float(vt[-1, 1])
    return a, b, float(-(a * ctr[0] + b * ctr[1]))


def _resample_polyline(pts, n: int) -> np.ndarray:
    """n points spaced evenly by arc length along the polyline pts."""
    p = _as_pts(pts)
    if len(p) < 2:
        return np.repeat(p[:1], n, axis=0)
    cum = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(p, axis=0), axis=1))])
    total = float(cum[-1])
    if total < 1e-9:
        return np.repeat(p[:1], n, axis=0)
    out = np.empty((n, 2))
    for k, t in enumerate(np.linspace(0.0, total, n)):
        i = min(max(int(np.searchsorted(cum, t) - 1), 0), len(p) - 2)
        seg = cum[i + 1] - cum[i]
        f = 0.0 if seg < 1e-9 else (t - cum[i]) / seg
        out[k] = p[i] + f * (p[i + 1] - p[i])
    return out


def _project_to_line(pts, a: float, b: float, c: float) -> np.ndarray:
    """Perpendicular projection of pts onto the line a*x+b*y+c=0 (a^2+b^2=1)."""
    q = _as_pts(pts)
    n = np.array([a, b], dtype=np.float64)
    return q - ((q @ n + c)[:, None]) * n[None, :]


def _fit_with_ground_lines(lines, center_pairs, ground_pairs, h_center, image_size,
                           fence_lines, robust, ground_lines, iters: int = 6, samples: int = 10,
                           panel_lines=None):
    """Fit with length-agnostic point-on-line ground constraints.

    Each ground line is [cam_polyline, ortho_polyline] tracing the SAME straight
    ground feature; endpoints/length need NOT correspond. We sample the camera
    line and (alternating with a refit) project those samples' current warp onto
    the ortho line — penalizing only the perpendicular distance to the ortho line,
    never the position along it. Reuses the point fitter + degeneracy guard.

    `panel_lines` are threaded through unchanged (folded into the ground set by the
    inner `fit_calibration`, exactly like `fence_lines`).
    """
    cam_samples, ortho_lines = [], []
    for gl in ground_lines or []:
        cam_poly, ortho_poly = _as_pts(gl[0]), _as_pts(gl[1])
        if len(cam_poly) >= 2 and len(ortho_poly) >= 2:
            cam_samples.append(_resample_polyline(cam_poly, samples))
            ortho_lines.append(_fit_line_tls(ortho_poly))
    if not cam_samples:  # nothing usable -> ordinary fit
        return fit_calibration(lines, center_pairs, ground_pairs, h_center, image_size,
                               fence_lines=fence_lines, robust=robust, panel_lines=panel_lines)

    def project_all(model):
        pairs = []
        for cs, (a, b, c) in zip(cam_samples, ortho_lines):
            for cp, tp in zip(cs, _project_to_line(apply_transform(model, cs), a, b, c)):
                pairs.append([cp.tolist(), tp.tolist()])
        return pairs

    # Seed line targets by projecting via a points-only fit if one exists, else a
    # rough arc-length correspondence (lines-only camera).
    try:
        seed, _ = fit_calibration(lines, center_pairs, list(ground_pairs), h_center,
                                  image_size, fence_lines=fence_lines, robust=robust,
                                  panel_lines=panel_lines)
        line_pairs = project_all(seed)
    except Exception:  # noqa: BLE001 - no point anchors to seed from
        line_pairs = []
        for gl in ground_lines:
            cp, op = _as_pts(gl[0]), _as_pts(gl[1])
            if len(cp) >= 2 and len(op) >= 2:
                for a_, b_ in zip(_resample_polyline(cp, samples), _resample_polyline(op, samples)):
                    line_pairs.append([a_.tolist(), b_.tolist()])

    model = diag = None
    for _ in range(max(1, iters)):
        model, diag = fit_calibration(lines, center_pairs, list(ground_pairs) + line_pairs,
                                      h_center, image_size, fence_lines=fence_lines, robust=robust,
                                      panel_lines=panel_lines)
        line_pairs = project_all(model)

    res = []
    for cs, (a, b, c) in zip(cam_samples, ortho_lines):
        w = apply_transform(model, cs)
        res.append(float(np.abs(w[:, 0] * a + w[:, 1] * b + c).mean()))
    diag = dict(diag)
    pe = dict(diag.get("per_point_error") or {})
    pe["ground_lines"] = res
    diag["per_point_error"] = pe
    return model, diag


def fit_calibration(lines, center_pairs, ground_pairs, h_center, image_size,
                    fence_lines=None, robust: bool = True, ground_lines=None,
                    panel_lines=None):
    """Fit the full 3-stage calibration for one camera.

    Returns (model, diagnostics). See module docstring / prompt for the exact
    shapes. Raises ValueError on too-few / degenerate inputs.

    `fence_lines` (optional) are camera↔ortho fence-segment correspondences; each
    matched vertex is a height-0 ground anchor and is folded into the ground set
    for levelling, the float drop, and the ortho extent (see fence_lines_to_pairs).

    `panel_lines` (optional) are camera↔ortho solar-panel footprint correspondences,
    treated EXACTLY like `fence_lines`: each matched vertex is a height-0 ground
    anchor (the footprint corners sit on the ground) folded into the ground set,
    extent, and drop; scored one mean residual per correspondence.

    `ground_lines` (optional) are camera↔ortho tracings of the same straight ground
    feature whose endpoints/length do NOT correspond. They're folded in as
    length-agnostic point-on-line constraints (see _fit_with_ground_lines).

    `robust` uses IRLS (Huber) polynomial/drop fits so a few misclicks can't skew
    the map; on clean data it matches the plain least-squares fit. A collinear
    ground set (the 'fence-only degenerate' case) is rejected with guidance.
    """
    if ground_lines:
        return _fit_with_ground_lines(lines, center_pairs, ground_pairs, h_center,
                                      image_size, fence_lines, robust, ground_lines,
                                      panel_lines=panel_lines)
    _poly = fit_polynomial_robust if robust else fit_polynomial
    _drop = _fit_drop_robust if robust else _fit_drop
    distortion = fit_distortion(lines, image_size) if lines else None

    fence_pairs = fence_lines_to_pairs(fence_lines)
    # Panel footprint corners are also height-0 ground anchors — expand them the
    # same way and treat them identically to fence anchors.
    panel_pairs = fence_lines_to_pairs(panel_lines, kind="panel")
    # Fence/panel vertices are height-0 ground anchors: fold them into the ground
    # set used for the fit / extent, but keep the user's clicked ground pairs first
    # so their per-point residuals stay index-aligned for the UI badges.
    ground_all = list(ground_pairs) + fence_pairs + panel_pairs

    center_cam, center_ortho = _split_pairs(center_pairs)
    ground_cam, ground_ortho = _split_pairs(ground_all)

    per_point_error = {"center": [], "ground": [], "fence": [], "panel": []}
    n_c, n_g = len(center_pairs), len(ground_all)

    # Adaptive requirement: ground levels the plane (>= 3 always), and centers +
    # ground together must reach 6 — i.e. ground >= max(3, 6 - centers). Give as
    # many centers as you have; ground makes up the difference. Polynomial degree
    # falls back to affine when points are sparse (see _choose_degree).
    if n_g < 3:
        raise ValueError(f"need >= 3 ground/pillar points to level the ground (have {n_g})")
    if n_c + n_g < 6:
        need = 6 - (n_c + n_g)
        raise ValueError(
            f"need center + ground >= 6 (have {n_c} + {n_g} = {n_c + n_g}); "
            f"add {need} more ground point(s) — ground must be >= max(3, 6 - centers)"
        )

    # Never rely on a degenerate (collinear) ground set — e.g. a single straight
    # fence. This is the geometric form of "never fence-only": a line of anchors
    # can't pin the plane across itself.
    ground_spread = _assert_not_collinear(
        ground_ortho,
        from_fence_only=(len(ground_pairs) == 0 and (len(fence_pairs) + len(panel_pairs)) > 0),
    )

    if n_c >= 3:
        # center-plane warp + float drop from height-0 samples
        center_cam_u = undistort(distortion, center_cam)
        F_center = _poly(center_cam_u, center_ortho)
        ground_cam_u = undistort(distortion, ground_cam)
        deltas = ground_ortho - apply_polynomial(F_center, ground_cam_u)
        drop = _drop(ground_cam_u, deltas)
        extent = _extent_of(np.vstack([center_ortho, ground_ortho]))
        model = {
            "type": "center_pillar",
            "distortion": distortion,
            "center": F_center,
            "drop": drop,
            "h_center": None if h_center is None else float(h_center),
            "ortho_extent": extent,
        }
    else:
        # too few centers to define a center plane -> fit the ground map directly
        # from the height-0 samples (n_g >= 6 here, since n_c < 3 and n_c+n_g >= 6)
        ground_cam_u = undistort(distortion, ground_cam)
        F_ground = _poly(ground_cam_u, ground_ortho)
        extent = _extent_of(ground_ortho)
        model = {
            "type": "ground_poly",
            "distortion": distortion,
            "ground": F_ground,
            "ortho_extent": extent,
        }

    # -------------------------------------------- diagnostics (meaningful residuals)
    # Center points are scored against the center-plane warp ONLY (adding the drop
    # to a center-height point is semantically wrong and would inflate it). Ground
    # points are scored against the full ground map. The headline reproj error is
    # the ground-map accuracy (over ALL ground anchors, clicked + fence) — the
    # thing the heatmap actually depends on.
    if n_c > 0 and model["type"] == "center_pillar":
        proj_c = apply_polynomial(model["center"], undistort(distortion, center_cam))
        dc = np.sqrt(((proj_c - center_ortho) ** 2).sum(axis=1))
        per_point_error["center"] = [float(v) for v in dc]

    ground_err = np.array([0.0])
    if n_g > 0:
        dg_all = np.sqrt(((apply_transform(model, ground_cam) - ground_ortho) ** 2).sum(axis=1))
        # Clicked ground pairs come first in `ground_all`; keep only those in the
        # per-point list so the UI badges stay aligned to the pairs the user placed.
        per_point_error["ground"] = [float(v) for v in dg_all[: len(ground_pairs)]]
        ground_err = dg_all

    # Fence residual is summarized per correspondence (mean px over its vertices),
    # so each traced segment gets one badge in the collector.
    for corr in fence_lines or []:
        cam = _as_pts(corr[0])
        ortho = _as_pts(corr[1])
        if len(cam) < 2 or len(cam) != len(ortho):
            continue
        d = np.sqrt(((apply_transform(model, cam) - ortho) ** 2).sum(axis=1))
        per_point_error["fence"].append(float(d.mean()))

    # Panel residual: same per-correspondence mean, one badge per panel footprint.
    for corr in panel_lines or []:
        cam = _as_pts(corr[0])
        ortho = _as_pts(corr[1])
        if len(cam) < 2 or len(cam) != len(ortho):
            continue
        d = np.sqrt(((apply_transform(model, cam) - ortho) ** 2).sum(axis=1))
        per_point_error["panel"].append(float(d.mean()))

    diagnostics = {
        "line_residual": _mean_line_residual_px(distortion, lines) if lines else 0.0,
        "reproj_error": float(ground_err.mean()),
        "max_residual": float(ground_err.max()),
        "ground_spread": float(ground_spread),  # minor/major anchor extent; low ⇒ near-collinear
        "per_point_error": per_point_error,
    }
    return _jsonable(model), _jsonable(diagnostics)


# --------------------------------------------------------------------------- apply / dispatch
def apply_transform(model: dict, pts) -> np.ndarray:
    """Apply a resolved calibration model to camera px -> ortho px (N, 2)."""
    mtype = model["type"]
    if mtype == "homography":
        return project_points(model["H"], pts)
    if mtype == "poly":
        return apply_polynomial(model, pts)
    if mtype == "ground_poly":
        u = undistort(model.get("distortion"), pts)
        return apply_polynomial(model["ground"], u)
    if mtype == "center_pillar":
        u = undistort(model.get("distortion"), pts)
        out = apply_polynomial(model["center"], u)
        if model.get("drop") is not None:
            out = out + _apply_drop(model["drop"], u)
        return out
    raise ValueError(f"unknown model type: {mtype!r}")


def resolve_model(entry: dict) -> dict:
    """Bridge a calibration.json entry to a model dict `apply_transform` understands."""
    if "model" in entry and entry["model"] is not None:
        return entry["model"]
    if "H" in entry:
        return {"type": "homography", "H": entry["H"]}
    raise ValueError("calibration entry has neither 'model' nor legacy 'H'")


def clip_to_extent(pts, extent, pad_frac: float = 0.0) -> np.ndarray:
    """Set rows whose points fall outside `extent` to NaN. `extent` is [xmin,ymin,xmax,ymax].

    `extent` is assumed already padded; `pad_frac` extends it further if given.
    """
    out = _as_pts(pts).astype(np.float64).copy()
    if extent is None:
        return out
    xmin, ymin, xmax, ymax = [float(v) for v in extent]
    if pad_frac:
        dx = (xmax - xmin) * pad_frac
        dy = (ymax - ymin) * pad_frac
        xmin, ymin, xmax, ymax = xmin - dx, ymin - dy, xmax + dx, ymax + dy
    inside = (
        (out[:, 0] >= xmin) & (out[:, 0] <= xmax)
        & (out[:, 1] >= ymin) & (out[:, 1] <= ymax)
    )
    out[~inside] = np.nan
    return out


# --------------------------------------------------------------------------- persistence
def save_calibration(
    path: str,
    camera_id: str,
    model: dict,
    diagnostics: dict,
    orthophoto: str | None,
    lines=None,
    center_pairs=None,
    ground_pairs=None,
    h_center=None,
    fence_lines=None,
    ground_lines=None,
    panel_lines=None,
) -> None:
    """Write one camera's calibration entry into the shared calibration.json map.

    MERGE-safe: the input-field args (``lines``, ``center_pairs``, ``ground_pairs``,
    ``fence_lines``, ``ground_lines``, ``panel_lines``, ``h_center``) each default to
    a ``None`` sentinel meaning "preserve the value already stored for this camera".
    An EXPLICITLY-passed value — *including* an empty list ``[]`` — REPLACES the stored
    value. This lets a caller that only refreshes the model/diagnostics omit the input
    fields without silently wiping them, while a caller that deliberately clears a field
    (e.g. the solo panel endpoint passing ``center_pairs=[]``, ``ground_pairs=[]``,
    ``h_center=None``) still works — but note ``h_center`` uses ``None`` as both its
    natural "no height" value and the preserve sentinel, so ``None`` preserves it.

    ``model``/``method``/the diagnostic fields/``orthophoto``/``saved_at`` always update.
    The ``n_*`` counts are recomputed from the RESOLVED (merged) values.
    """
    data = load_all(path)
    prev = data.get(camera_id, {}) or {}

    # For each mergeable field: use the passed value if not None, else the value
    # already stored for this camera, else the field's natural default.
    def _merge(passed, key, default):
        if passed is not None:
            return passed
        stored = prev.get(key)
        return default if stored is None else stored

    lines = _merge(lines, "lines", [])
    center_pairs = _merge(center_pairs, "center_pairs", [])
    ground_pairs = _merge(ground_pairs, "ground_pairs", [])
    fence_lines = _merge(fence_lines, "fence_lines", [])
    ground_lines = _merge(ground_lines, "ground_lines", [])
    panel_lines = _merge(panel_lines, "panel_lines", [])
    h_center = _merge(h_center, "h_center", None)

    entry = {
        "method": model["type"],
        "model": model,
        "reproj_error": diagnostics["reproj_error"],
        "max_residual": diagnostics["max_residual"],
        "line_residual": diagnostics["line_residual"],
        "per_point_error": diagnostics["per_point_error"],
        "h_center": None if h_center is None else float(h_center),
        "n_center": len(center_pairs),
        "n_ground": len(ground_pairs),
        "n_lines": len(lines),
        "n_fence": len(fence_lines),
        "n_ground_lines": len(ground_lines),
        "n_panel": len(panel_lines),
        "lines": lines,
        "center_pairs": center_pairs,
        "ground_pairs": ground_pairs,
        "fence_lines": fence_lines,
        "ground_lines": ground_lines,
        "panel_lines": panel_lines,
        "orthophoto": orthophoto,
        "saved_at": datetime.now().isoformat(timespec="seconds"),
    }
    data[camera_id] = _jsonable(entry)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
