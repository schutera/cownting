"""Numpy-only unit tests for the 3-stage warp calibration.

No pytest, no scipy. Run either way:
    .venv/bin/python -m tests.test_warp
    .venv/bin/python tests/test_warp.py

Prints each check and a final PASS; sys.exit(1) on the first failure.
"""
from __future__ import annotations

import os
import sys

import numpy as np

# Allow `python tests/test_warp.py` (no package context) to find the package.
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting.calib.warp import (  # noqa: E402
    apply_polynomial,
    apply_transform,
    clip_to_extent,
    fence_lines_to_pairs,
    fit_calibration,
    fit_distortion,
    fit_polynomial,
    resolve_model,
)
from cownting.calib.homography import compute_homography, project_points  # noqa: E402

RNG = np.random.default_rng(42)
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


def _distort_forward(pts, center, scale, lam):
    """Inverse of `undistort`: apply barrel distortion with parameter `lam`.

    undistort maps n_d -> n_u = n_d / (1 + lam*|n_d|^2). Here we go the other way:
    given straight (undistorted) points, produce distorted image points so that
    fit_distortion should recover ~lam and straighten them.
    """
    p = np.asarray(pts, dtype=np.float64).reshape(-1, 2)
    c = np.asarray(center, dtype=np.float64)
    n_u = (p - c) / scale
    ru = np.hypot(n_u[:, 0], n_u[:, 1])
    # Solve n_u = n_d/(1+lam n_d^2) for the distorted radius r_d (per point).
    # lam*ru*rd^2 - rd + ru = 0  ->  rd = (1 - sqrt(1 - 4*lam*ru^2)) / (2*lam*ru)
    rd = np.empty_like(ru)
    for i in range(len(ru)):
        r = float(ru[i])
        if abs(lam) < 1e-12 or r < 1e-12:
            rd[i] = r
        else:
            disc = 1.0 - 4.0 * lam * r * r
            disc = max(disc, 0.0)
            rd[i] = (1.0 - np.sqrt(disc)) / (2.0 * lam * r)
    scale_r = np.where(ru < 1e-12, 1.0, rd / ru)
    n_d = n_u * scale_r[:, None]
    return c + scale * n_d


# --------------------------------------------------------------------------- 1: distortion
def test_distortion():
    w, h = 1920.0, 1080.0
    center = np.array([w / 2, h / 2])
    scale = 0.5 * np.hypot(w, h)
    lam_true = 0.4

    lines_straight = []
    # 3 straight lines in different orientations, spread across the frame.
    for (x0, y0, x1, y1) in [(200, 200, 1700, 300), (300, 900, 1600, 850), (960, 100, 1000, 1000)]:
        t = np.linspace(0.0, 1.0, 9)
        xs = x0 + t * (x1 - x0)
        ys = y0 + t * (y1 - y0)
        lines_straight.append(np.stack([xs, ys], axis=1))

    lines_distorted = [_distort_forward(ln, center, scale, lam_true) for ln in lines_straight]

    # Pre-fit residual: how bowed the distorted lines are (identity model).
    from cownting.calib.warp import _mean_line_residual_px

    pre = _mean_line_residual_px(None, lines_distorted)
    model = fit_distortion([ln.tolist() for ln in lines_distorted], (w, h))
    post = _mean_line_residual_px(model, lines_distorted)

    check("distortion: model fitted", model is not None)
    check("distortion: straightened (post << pre)", post < 0.1 * pre + 1e-6,
          f"pre={pre:.3f}px post={post:.4f}px")
    check("distortion: lambda near truth", abs(model["lambda"] - lam_true) < 0.05,
          f"fit={model['lambda']:.4f} true={lam_true}")


# --------------------------------------------------------------------------- 2: polynomial
def test_polynomial():
    # Known quadratic map cam -> ortho.
    def truth(p):
        x, y = p[:, 0], p[:, 1]
        ox = 3.0 + 1.2 * x - 0.4 * y + 0.001 * x * x + 0.0005 * x * y
        oy = -5.0 + 0.3 * x + 0.9 * y - 0.0007 * y * y + 0.0003 * x * y
        return np.stack([ox, oy], axis=1)

    cam = RNG.uniform(0, 1000, size=(40, 2))
    ortho = truth(cam)
    model = fit_polynomial(cam, ortho)
    pred = apply_polynomial(model, cam)
    resid = np.sqrt(((pred - ortho) ** 2).sum(axis=1)).mean()
    check("polynomial: degree-3 chosen (n=40)", model["degree"] == 3)
    check("polynomial: tiny in-sample residual", resid < 1e-3, f"{resid:.2e}px")

    held = RNG.uniform(100, 900, size=(10, 2))
    pred_h = apply_polynomial(model, held)
    resid_h = np.sqrt(((pred_h - truth(held)) ** 2).sum(axis=1)).mean()
    check("polynomial: held-out prediction matches", resid_h < 1e-2, f"{resid_h:.2e}px")


# --------------------------------------------------------------------------- synthetic camera
def _lookat(eye, target, up=np.array([0.0, 0.0, 1.0])):
    """World->camera rotation R and translation t for a camera at `eye` looking at `target`.

    Camera axes: x = right, y = -true_up (image y points down), z = forward.
    """
    eye = np.asarray(eye, dtype=np.float64)
    target = np.asarray(target, dtype=np.float64)
    fwd = target - eye
    fwd /= np.linalg.norm(fwd)
    right = np.cross(fwd, up)
    right /= np.linalg.norm(right)
    trueup = np.cross(right, fwd)
    R = np.stack([right, -trueup, fwd], axis=0)
    t = -R @ eye
    return R, t


def _make_camera():
    """Pinhole camera mounted high, looking obliquely down at the field.

    Mounted ~11 m up so the field fills the frame without points near the horizon
    (where the image->ground map becomes singular and no low-order poly can fit).
    """
    f = 1400.0
    cx, cy = 960.0, 540.0
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], dtype=np.float64)
    R, t = _lookat(eye=[2.0, -3.0, 11.0], target=[2.0, 9.0, 0.0])
    return K, R, t


def _project(K, R, t, world):
    """Project world (N,3) metres -> image px (N,2)."""
    world = np.asarray(world, dtype=np.float64)
    cam = (R @ world.T).T + t
    uv = (K @ cam.T).T
    return uv[:, :2] / uv[:, 2:3]


def _ground_z(X, Y, a, b):
    return a * X + b * Y


# --------------------------------------------------------------------------- 3: center_pillar
def test_center_pillar():
    K, R, t = _make_camera()
    a, b = 0.04, -0.025          # tilted ground plane z = aX + bY
    h_center = 2.5               # panel-center height above the local ground
    ortho_scale = 20.0           # ortho px per metre (a few px ~= 10-20 cm on ground)

    # Grid of field positions (kept clear of the horizon so the map stays smooth).
    gx = np.linspace(-5, 9, 6)
    gy = np.linspace(3, 13, 6)
    XX, YY = np.meshgrid(gx, gy)
    X = XX.ravel()
    Y = YY.ravel()
    Zg = _ground_z(X, Y, a, b)

    # Panel centers float at h_center above the ground plane.
    centers_world = np.stack([X, Y, Zg + h_center], axis=1)
    ground_world = np.stack([X, Y, Zg], axis=1)

    centers_cam = _project(K, R, t, centers_world)
    ground_cam = _project(K, R, t, ground_world)
    ortho = np.stack([X * ortho_scale, Y * ortho_scale], axis=1)

    idx = np.arange(len(X))
    # Center pairs: use all grid points (36 -> plenty for the degree-3 warp).
    center_pairs = [[centers_cam[i].tolist(), ortho[i].tolist()] for i in idx]
    # Ground/pillar samples: a spread subset held for training the drop.
    train_g = idx[::4]           # ~9 spread samples
    ground_pairs = [[ground_cam[i].tolist(), ortho[i].tolist()] for i in train_g]

    # No fisheye here (pinhole); fit_calibration only needs lines for the
    # distortion stage, so pass none and rely on center + ground pairs.
    model, diag = fit_calibration([], center_pairs, ground_pairs, h_center,
                                  (1920, 1080))
    check("center_pillar: type", model["type"] == "center_pillar")
    check("center_pillar: has drop", model["drop"] is not None)
    check("center_pillar: h_center stored", abs(model["h_center"] - h_center) < 1e-9)

    # Held-out GROUND points (not used to fit the drop) — the real accuracy test.
    held = np.array([i for i in idx if i not in set(train_g.tolist())])
    held = held[::2]             # a spread subset
    pred = apply_transform(model, ground_cam[held])
    err = np.sqrt(((pred - ortho[held]) ** 2).sum(axis=1))
    mean_err = float(err.mean())
    check("center_pillar: held-out ground recovered", mean_err < 5.0,
          f"mean {mean_err:.3f}px, max {err.max():.3f}px")
    check("center_pillar: diagnostics present",
          "reproj_error" in diag and "per_point_error" in diag
          and len(diag["per_point_error"]["ground"]) == len(train_g),
          f"reproj={diag['reproj_error']:.3f}px")

    # The center warp alone (no drop) maps a CENTER pixel to its ortho footprint.
    from cownting.calib.warp import apply_polynomial as _ap
    pc = _ap(model["center"], centers_cam[held])
    ce = np.sqrt(((pc - ortho[held]) ** 2).sum(axis=1)).mean()
    check("center_pillar: center warp maps center pixels", ce < 5.0, f"mean {ce:.3f}px")


# --------------------------------------------------------------------------- 4: ground_poly
def test_ground_poly():
    K, R, t = _make_camera()
    a, b = 0.04, 0.02
    ortho_scale = 20.0
    gx = np.linspace(-5, 9, 5)
    gy = np.linspace(3, 13, 5)
    XX, YY = np.meshgrid(gx, gy)
    X = XX.ravel()
    Y = YY.ravel()
    Zg = _ground_z(X, Y, a, b)
    ground_world = np.stack([X, Y, Zg], axis=1)
    ground_cam = _project(K, R, t, ground_world)
    ortho = np.stack([X * ortho_scale, Y * ortho_scale], axis=1)

    idx = np.arange(len(X))
    train = idx[::2]             # ~13 samples
    ground_pairs = [[ground_cam[i].tolist(), ortho[i].tolist()] for i in train]

    model, diag = fit_calibration([], [], ground_pairs, None, (1920, 1080))
    check("ground_poly: type", model["type"] == "ground_poly")

    held = np.array([i for i in idx if i not in set(train.tolist())])
    pred = apply_transform(model, ground_cam[held])
    err = np.sqrt(((pred - ortho[held]) ** 2).sum(axis=1)).mean()
    check("ground_poly: held-out recovered", err < 5.0, f"mean {err:.3f}px")


# --------------------------------------------------------------------------- 4b: adaptive counts
def test_adaptive():
    """Fewer than 6 centers is fine: ground makes up the difference (>= max(3, 6-centers))."""
    K, R, t = _make_camera()
    a, b = 0.03, -0.02
    h_center = 2.5
    ortho_scale = 20.0
    gx = np.linspace(-4, 8, 5)
    gy = np.linspace(4, 12, 5)
    XX, YY = np.meshgrid(gx, gy)
    X = XX.ravel()
    Y = YY.ravel()
    Zg = _ground_z(X, Y, a, b)
    centers_cam = _project(K, R, t, np.stack([X, Y, Zg + h_center], axis=1))
    ground_cam = _project(K, R, t, np.stack([X, Y, Zg], axis=1))
    ortho = np.stack([X * ortho_scale, Y * ortho_scale], axis=1)
    idx = np.arange(len(X))

    def pairs(cam, sel):
        return [[cam[i].tolist(), ortho[i].tolist()] for i in sel]

    def raises(fn):
        try:
            fn()
            return False
        except ValueError:
            return True

    # 5 centers (< 6) + 3 ground -> center_pillar with an affine (degree-1) center warp.
    model, _ = fit_calibration([], pairs(centers_cam, idx[:5]),
                               pairs(ground_cam, idx[::8][:3]), h_center, (1920, 1080))
    check("adaptive: 5 center + 3 ground fits", model["type"] == "center_pillar")
    check("adaptive: sparse center warp is affine (deg1)",
          model["center"]["degree"] == 1, f"deg={model['center']['degree']}")
    out = apply_transform(model, ground_cam[idx[10:14]])
    check("adaptive: output finite", bool(np.isfinite(out).all()))

    # 0 centers + 6 ground -> ground_poly fallback.
    m2, _ = fit_calibration([], [], pairs(ground_cam, idx[::4][:6]), None, (1920, 1080))
    check("adaptive: 0 center + 6 ground -> ground_poly", m2["type"] == "ground_poly")

    # Validation: ground floor and the center+ground>=6 total.
    check("adaptive: < 3 ground rejected",
          raises(lambda: fit_calibration([], pairs(centers_cam, idx[:5]),
                                         pairs(ground_cam, idx[:2]), h_center, (1920, 1080))))
    check("adaptive: center+ground < 6 rejected",
          raises(lambda: fit_calibration([], pairs(centers_cam, idx[:2]),
                                         pairs(ground_cam, idx[2:5]), h_center, (1920, 1080))))


# --------------------------------------------------------------------------- 4c: fence lines
def test_fence_lines():
    """Fence-segment correspondences expand to ground anchors and can carry the fit."""
    # --- fence_lines_to_pairs mechanics ---
    corr = [[[0, 0], [10, 0], [20, 0]], [[100, 5], [110, 5], [120, 5]]]  # 3 vtx each
    pairs = fence_lines_to_pairs([corr])
    check("fence: expands to one pair per vertex", len(pairs) == 3)
    check("fence: vertex i -> pair i", pairs[1] == [[10.0, 0.0], [110.0, 5.0]])

    def raises(fn):
        try:
            fn()
            return False
        except ValueError:
            return True

    check("fence: mismatched vertex counts raise",
          raises(lambda: fence_lines_to_pairs([[[[0, 0]], [[1, 1], [2, 2]]]])))
    check("fence: <2 vertices skipped", fence_lines_to_pairs([[[[0, 0]], [[1, 1]]]]) == [])

    # --- fit leaning on fence lines as the ground anchors (no clicked ground pairs) ---
    K, R, t = _make_camera()
    a, b = 0.03, -0.02
    h_center = 2.5
    ortho_scale = 20.0
    gx = np.linspace(-4, 8, 5)
    gy = np.linspace(4, 12, 5)
    XX, YY = np.meshgrid(gx, gy)
    X = XX.ravel()
    Y = YY.ravel()
    Zg = _ground_z(X, Y, a, b)
    centers_cam = _project(K, R, t, np.stack([X, Y, Zg + h_center], axis=1))
    ortho = np.stack([X * ortho_scale, Y * ortho_scale], axis=1)
    center_pairs = [[centers_cam[i].tolist(), ortho[i].tolist()] for i in range(len(X))]

    def fence_corr(world_xy):
        """A ground fence: world (x,y) -> [cam_polyline, ortho_polyline] at height 0."""
        w = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in world_xy])
        cam = _project(K, R, t, w)
        orth = np.array([[x * ortho_scale, y * ortho_scale] for (x, y) in world_xy])
        return [cam.tolist(), orth.tolist()]

    # Two fences in different orientations -> anchors spread in 2D (not collinear).
    fence_lines = [
        fence_corr([(-4, 5), (-1, 6), (2, 7), (6, 8)]),
        fence_corr([(7, 4), (7, 8), (6, 11)]),
    ]

    model, diag = fit_calibration([], center_pairs, [], h_center, (1920, 1080),
                                  fence_lines=fence_lines)
    check("fence: fits with fence as the only ground", model["type"] == "center_pillar")
    check("fence: one residual per correspondence",
          len(diag["per_point_error"]["fence"]) == 2)
    check("fence: clicked-ground residuals stay empty",
          diag["per_point_error"]["ground"] == [])

    # Held-out GROUND points (not on either fence) recover through the fence-anchored map.
    held_xy = [(0, 6), (3, 9), (5, 5), (-2, 10)]
    held = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in held_xy])
    held_cam = _project(K, R, t, held)
    held_ortho = np.array([[x * ortho_scale, y * ortho_scale] for (x, y) in held_xy])
    err = np.sqrt(((apply_transform(model, held_cam) - held_ortho) ** 2).sum(axis=1)).mean()
    check("fence: held-out ground recovered", err < 5.0, f"mean {err:.3f}px")


# --------------------------------------------------------------------------- 4d: fence persistence
def test_save_persists_fence():
    """Phase 0 guard: fence_lines + n_fence + fence residuals survive save -> load."""
    import os
    import tempfile

    from cownting.calib.warp import save_calibration  # noqa: E402
    from cownting.calib.homography import load_all  # noqa: E402

    K, R, t = _make_camera()
    a, b, h_center, sc = 0.03, -0.02, 2.5, 20.0
    gx = np.linspace(-4, 8, 5)
    gy = np.linspace(4, 12, 5)
    XX, YY = np.meshgrid(gx, gy)
    X = XX.ravel()
    Y = YY.ravel()
    Zg = _ground_z(X, Y, a, b)
    centers_cam = _project(K, R, t, np.stack([X, Y, Zg + h_center], axis=1))
    ortho = np.stack([X * sc, Y * sc], axis=1)
    center_pairs = [[centers_cam[i].tolist(), ortho[i].tolist()] for i in range(len(X))]

    def fence_corr(world_xy):
        w = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in world_xy])
        cam = _project(K, R, t, w)
        orth = np.array([[x * sc, y * sc] for (x, y) in world_xy])
        return [cam.tolist(), orth.tolist()]

    fence_lines = [fence_corr([(-4, 5), (-1, 6), (2, 7), (6, 8)]),
                   fence_corr([(7, 4), (7, 8), (6, 11)])]
    model, diag = fit_calibration([], center_pairs, [], h_center, (1920, 1080),
                                  fence_lines=fence_lines)

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "calibration.json")
        save_calibration(path, "camera_01", model, diag, None,
                         [], center_pairs, [], h_center, fence_lines=fence_lines)
        entry = load_all(path)["camera_01"]

    check("persist: n_fence stored", entry.get("n_fence") == 2, f"{entry.get('n_fence')}")
    fl = entry.get("fence_lines")
    intact = (
        isinstance(fl, list) and len(fl) == 2
        and all(np.allclose(np.array(fl[i][0]), np.array(fence_lines[i][0]))
                and np.allclose(np.array(fl[i][1]), np.array(fence_lines[i][1]))
                for i in range(2))
    )
    check("persist: fence_lines survive JSON round-trip", intact)
    check("persist: fence residuals stored",
          len(entry.get("per_point_error", {}).get("fence", [])) == 2)


# --------------------------------------------------------------------------- 4d': merge-safe save
def test_save_merge_safe():
    """save_calibration is MERGE-safe: an OMITTED (None) input field preserves the
    stored value; an EXPLICITLY-passed one (including []) replaces it.

    Regression guard: the joint endpoint once omitted ground_lines and wiped every
    camera's stored ground_lines because the old code defaulted a missing arg to [].
    """
    import os
    import tempfile

    from cownting.calib.warp import save_calibration  # noqa: E402
    from cownting.calib.homography import load_all  # noqa: E402

    # A minimal valid ground_poly model + diagnostics (fit from a spread ground set).
    K, R, t = _make_camera()
    a, b, sc = 0.03, -0.02, 20.0
    gx = np.linspace(-4, 8, 4)
    gy = np.linspace(4, 12, 4)
    XX, YY = np.meshgrid(gx, gy)
    X, Y = XX.ravel(), YY.ravel()
    gcam = _project(K, R, t, np.stack([X, Y, _ground_z(X, Y, a, b)], axis=1))
    gortho = np.stack([X * sc, Y * sc], axis=1)
    ground_pairs = [[gcam[i].tolist(), gortho[i].tolist()] for i in range(len(X))]
    model, diag = fit_calibration([], [], ground_pairs, None, (1920, 1080))

    # Two straight ground-line correspondences (shape only; not re-fit on save).
    ground_lines = [
        [[[10.0, 10.0], [20.0, 12.0]], [[100.0, 100.0], [200.0, 120.0]]],
        [[[30.0, 40.0], [35.0, 60.0]], [[300.0, 400.0], [350.0, 600.0]]],
    ]

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "calibration.json")

        # 1) First save WITH ground_lines.
        save_calibration(path, "camera_01", model, diag, None,
                         [], [], ground_pairs, None, ground_lines=ground_lines)
        e1 = load_all(path)["camera_01"]
        check("merge: initial ground_lines stored",
              e1.get("n_ground_lines") == 2 and len(e1.get("ground_lines") or []) == 2)

        # 2) Save again OMITTING ground_lines (as the buggy joint call did) — the
        #    stored ground_lines must be PRESERVED, not wiped.
        save_calibration(path, "camera_01", model, diag, None,
                         [], [], ground_pairs, None)
        e2 = load_all(path)["camera_01"]
        preserved = (
            e2.get("n_ground_lines") == 2
            and len(e2.get("ground_lines") or []) == 2
            and np.allclose(np.array(e2["ground_lines"][0][0]),
                            np.array(ground_lines[0][0]))
            and np.allclose(np.array(e2["ground_lines"][1][1]),
                            np.array(ground_lines[1][1]))
        )
        check("merge: omitted ground_lines preserved (not wiped)", preserved,
              f"n_ground_lines={e2.get('n_ground_lines')}")

        # 3) Passing ground_lines=[] EXPLICITLY must CLEAR them (empty replaces).
        save_calibration(path, "camera_01", model, diag, None,
                         [], [], ground_pairs, None, ground_lines=[])
        e3 = load_all(path)["camera_01"]
        check("merge: explicit ground_lines=[] clears them",
              e3.get("n_ground_lines") == 0 and (e3.get("ground_lines") or []) == [],
              f"n_ground_lines={e3.get('n_ground_lines')}")


# --------------------------------------------------------------------------- 4e: robust + guard
def test_robust_and_guard():
    """IRLS down-weights a misclick; a collinear ground set is rejected."""
    K, R, t = _make_camera()
    a, b, sc = 0.03, -0.02, 20.0
    gx = np.linspace(-4, 8, 8)
    gy = np.linspace(4, 12, 8)
    XX, YY = np.meshgrid(gx, gy)
    X = XX.ravel()
    Y = YY.ravel()
    Zg = _ground_z(X, Y, a, b)
    gcam = _project(K, R, t, np.stack([X, Y, Zg], axis=1))
    gortho = np.stack([X * sc, Y * sc], axis=1)
    idx = np.arange(len(X))
    train = idx[::2]
    held = np.array([i for i in idx if i not in set(train.tolist())])

    def pairs(sel, corrupt=None):
        ps = []
        for j, i in enumerate(sel):
            o = gortho[i].copy()
            if corrupt is not None and j == corrupt:
                o = o + np.array([130.0, -90.0])  # a gross misclick (~7 m off)
            ps.append([gcam[i].tolist(), o.tolist()])
        return ps

    dirty = pairs(train, corrupt=12)
    m_r, _ = fit_calibration([], [], dirty, None, (1920, 1080), robust=True)
    m_o, _ = fit_calibration([], [], dirty, None, (1920, 1080), robust=False)
    err_r = np.sqrt(((apply_transform(m_r, gcam[held]) - gortho[held]) ** 2).sum(axis=1)).mean()
    err_o = np.sqrt(((apply_transform(m_o, gcam[held]) - gortho[held]) ** 2).sum(axis=1)).mean()
    check("robust: beats OLS on held-out with a misclick present", err_r < err_o,
          f"robust {err_r:.2f}px vs OLS {err_o:.2f}px")
    check("robust: held-out stays accurate despite the outlier", err_r < 5.0, f"{err_r:.2f}px")

    def raises(fn):
        try:
            fn()
            return False
        except ValueError:
            return True

    # Collinear ground (a single straight fence line) -> rejected, not silently fit.
    tl = np.linspace(0.0, 1.0, 8)
    lx = -3 + tl * 10
    ly = 4 + tl * 6
    lcam = _project(K, R, t, np.stack([lx, ly, _ground_z(lx, ly, a, b)], axis=1))
    lortho = np.stack([lx * sc, ly * sc], axis=1)
    line_pairs = [[lcam[i].tolist(), lortho[i].tolist()] for i in range(len(lx))]
    check("guard: collinear ground rejected",
          raises(lambda: fit_calibration([], [], line_pairs, None, (1920, 1080))))


# --------------------------------------------------------------------------- 4f: joint multi-camera
def test_joint_calibration():
    """Two cameras sharing an L-shaped fence end up agreeing in the orthophoto."""
    from cownting.calib.joint import joint_calibrate

    f, cx, cy = 1400.0, 960.0, 540.0
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], dtype=np.float64)
    a, b, h, sc = 0.03, -0.02, 2.5, 20.0
    R1, t1 = _lookat(np.array([2.0, -3.0, 11.0]), np.array([3.0, 9.0, 0.0]))
    R2, t2 = _lookat(np.array([9.0, -2.0, 11.0]), np.array([5.0, 9.0, 0.0]))

    gx = np.linspace(-3, 8, 5)
    gy = np.linspace(4, 12, 5)
    XX, YY = np.meshgrid(gx, gy)
    X, Y = XX.ravel(), YY.ravel()
    ortho_c = np.stack([X * sc, Y * sc], axis=1)
    centers_w = np.stack([X, Y, _ground_z(X, Y, a, b) + h], axis=1)

    def center_pairs(R, t):
        cc = _project(K, R, t, centers_w)
        return [[cc[i].tolist(), ortho_c[i].tolist()] for i in range(len(X))]

    # L-shaped ground fence (spans 2D so the ground set isn't collinear).
    fence_xy = [(-2, 5), (2, 5), (5, 6), (5, 9), (3, 11)]
    fw = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in fence_xy])
    fence_ortho = np.array([[x * sc, y * sc] for (x, y) in fence_xy])

    def fence_link(R, t):
        return [_project(K, R, t, fw).tolist(), fence_ortho.tolist()]

    inputs = {
        "cam1": {"lines": [], "center_pairs": center_pairs(R1, t1), "ground_pairs": [],
                 "fence_lines": [fence_link(R1, t1)], "h_center": h},
        "cam2": {"lines": [], "center_pairs": center_pairs(R2, t2), "ground_pairs": [],
                 "fence_lines": [fence_link(R2, t2)], "h_center": h},
    }
    sizes = {"cam1": (1920, 1080), "cam2": (1920, 1080)}
    results, g = joint_calibrate(["cam1", "cam2"], inputs, sizes)

    check("joint: both cameras fit", set(results.keys()) == {"cam1", "cam2"})
    check("joint: shared corners found (both trace all 5)",
          g["n_shared_corners"] == 5 and g["n_pairs"] == 5,
          f"corners={g['n_shared_corners']} pairs={g['n_pairs']}")
    check("joint: cross-camera agreement tight", g["cross_camera_px"] < 3.0,
          f"mean {g['cross_camera_px']:.3f}px, max {g['max_cross_camera_px']:.3f}px")
    check("joint: per-camera n_shared counted", results["cam1"]["n_shared"] == 5)

    # Held-out ground (off the fence) recovers through the jointly-fit cam1 map.
    hx = [(1, 6), (4, 9), (2, 10)]
    hw = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in hx])
    ho = np.array([[x * sc, y * sc] for (x, y) in hx])
    err = np.sqrt(((apply_transform(results["cam1"]["model"], _project(K, R1, t1, hw)) - ho) ** 2)
                  .sum(axis=1)).mean()
    check("joint: cam1 held-out ground recovered", err < 5.0, f"{err:.3f}px")


# --------------------------------------------------------------------------- 4g: joint tie points
def test_joint_tiepoints():
    """Cross-camera tie points (no shared fence) couple two cameras in the ortho."""
    from cownting.calib.joint import joint_calibrate

    f, cx, cy = 1400.0, 960.0, 540.0
    K = np.array([[f, 0, cx], [0, f, cy], [0, 0, 1]], dtype=np.float64)
    a, b, h, sc = 0.03, -0.02, 2.5, 20.0
    R1, t1 = _lookat(np.array([2.0, -3.0, 11.0]), np.array([3.0, 9.0, 0.0]))
    R2, t2 = _lookat(np.array([9.0, -2.0, 11.0]), np.array([5.0, 9.0, 0.0]))

    gx = np.linspace(-3, 8, 5)
    gy = np.linspace(4, 12, 5)
    XX, YY = np.meshgrid(gx, gy)
    X, Y = XX.ravel(), YY.ravel()
    ortho_c = np.stack([X * sc, Y * sc], axis=1)
    centers_w = np.stack([X, Y, _ground_z(X, Y, a, b) + h], axis=1)
    ground_xy = [(-2, 6), (6, 6), (2, 11)]  # each camera's own (spread) ground anchors
    gw = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in ground_xy])

    def cam_inputs(R, t):
        cc = _project(K, R, t, centers_w)
        gc = _project(K, R, t, gw)
        return {
            "lines": [],
            "center_pairs": [[cc[i].tolist(), ortho_c[i].tolist()] for i in range(len(X))],
            "ground_pairs": [[gc[i].tolist(), [gw[i, 0] * sc, gw[i, 1] * sc]] for i in range(len(gw))],
            "fence_lines": [],
            "h_center": h,
        }

    inputs = {"cam1": cam_inputs(R1, t1), "cam2": cam_inputs(R2, t2)}
    sizes = {"cam1": (1920, 1080), "cam2": (1920, 1080)}

    # Three shared GROUND features, each sighted in both cameras (no fence, no snap).
    tie_xy = [(1, 7), (4, 9), (0, 10)]
    tie_w = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in tie_xy])
    p1, p2 = _project(K, R1, t1, tie_w), _project(K, R2, t2, tie_w)
    tie_points = [
        [{"camera": "cam1", "pt": p1[i].tolist()}, {"camera": "cam2", "pt": p2[i].tolist()}]
        for i in range(len(tie_w))
    ]

    results, g = joint_calibrate(["cam1", "cam2"], inputs, sizes, tie_points=tie_points)
    check("tie: both cameras fit", set(results.keys()) == {"cam1", "cam2"})
    check("tie: tie points become shared landmarks",
          g["n_shared_corners"] == 3 and g["n_pairs"] == 3,
          f"landmarks={g['n_shared_corners']} pairs={g['n_pairs']}")
    check("tie: each camera sees all 3", results["cam1"]["n_shared"] == 3)
    check("tie: cross-camera agreement tight", g["cross_camera_px"] < 3.0,
          f"mean {g['cross_camera_px']:.3f}px")

    held = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in [(2, 8), (5, 6), (1, 10)]])
    ho = np.array([[p[0] * sc, p[1] * sc] for p in held[:, :2]])
    err = np.sqrt(((apply_transform(results["cam1"]["model"], _project(K, R1, t1, held)) - ho) ** 2)
                  .sum(axis=1)).mean()
    check("tie: cam1 held-out ground recovered", err < 5.0, f"{err:.3f}px")


# --------------------------------------------------------------------------- 4h: ground lines
def test_ground_lines():
    """Point-on-line constraints: ortho line traced over a DIFFERENT extent than the
    camera line still recovers the ground map (length/position ignored)."""
    K, R, t = _make_camera()
    a, b, h, sc = 0.03, -0.02, 2.5, 20.0
    gx = np.linspace(-4, 8, 5)
    gy = np.linspace(4, 12, 5)
    XX, YY = np.meshgrid(gx, gy)
    X, Y = XX.ravel(), YY.ravel()
    centers_w = np.stack([X, Y, _ground_z(X, Y, a, b) + h], axis=1)
    ortho_c = np.stack([X * sc, Y * sc], axis=1)
    cc = _project(K, R, t, centers_w)
    center_pairs = [[cc[i].tolist(), ortho_c[i].tolist()] for i in range(len(X))]

    def gline(p0, p1, cam_ts, ortho_ts):
        # Same world line p0->p1; camera samples over cam_ts, ortho over a DIFFERENT
        # t-range (different extent, position and point count) — endpoints don't match.
        def at(ts):
            return np.array([[p0[0] + s * (p1[0] - p0[0]), p0[1] + s * (p1[1] - p0[1])] for s in ts])
        wc = at(cam_ts)
        cam = _project(K, R, t, np.column_stack([wc, _ground_z(wc[:, 0], wc[:, 1], a, b)]))
        return [cam.tolist(), (at(ortho_ts) * sc).tolist()]

    # Ground lines spanning the field in 2 orientations (coverage, like points need
    # spread) — each ortho side traced over a different extent than its camera side.
    glines = [
        gline((-3, 6), (7, 7), np.linspace(0, 1, 6), np.linspace(0.2, 0.9, 3)),
        gline((-3, 10), (7, 11), np.linspace(0, 1, 6), np.linspace(0.1, 0.8, 4)),
        gline((0, 4), (1, 12), np.linspace(0, 1, 6), np.linspace(0.3, 0.85, 3)),
        gline((5, 4), (6, 12), np.linspace(0, 1, 6), np.linspace(0.15, 0.9, 4)),
    ]

    # A few spread ground points too (the realistic case: points + lines).
    gp_xy = [(-3, 5), (7, 5), (7, 11), (-3, 11), (2, 8)]
    gpc = _project(K, R, t, np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in gp_xy]))
    ground_pairs = [[gpc[i].tolist(), [gp_xy[i][0] * sc, gp_xy[i][1] * sc]] for i in range(len(gp_xy))]

    model, diag = fit_calibration([], center_pairs, ground_pairs, h, (1920, 1080), ground_lines=glines)
    check("gline: fits from points + ground lines", model["type"] == "center_pillar")
    lres = diag["per_point_error"].get("ground_lines", [])
    # The warped camera samples land ON the ortho lines despite the ortho being traced
    # over a different extent — this is the length/position-agnostic proof.
    check("gline: lines satisfied despite mismatched extents (low perp residual)",
          len(lres) == len(glines) and max(lres) < 5.0, f"max {max(lres) if lres else 0:.2f}px")

    held = np.array([[x, y, _ground_z(x, y, a, b)] for (x, y) in [(0, 6), (4, 9), (5, 5), (-2, 10)]])
    ho = np.array([[p[0] * sc, p[1] * sc] for p in held[:, :2]])
    err = np.sqrt(((apply_transform(model, _project(K, R, t, held)) - ho) ** 2).sum(axis=1)).mean()
    check("gline: held-out ground recovered", err < 5.0, f"{err:.2f}px")

    def raises(fn):
        try:
            fn()
            return False
        except ValueError:
            return True

    # A single ground line is collinear -> degenerate -> rejected.
    check("gline: single line rejected (collinear)",
          raises(lambda: fit_calibration([], center_pairs, [], h, (1920, 1080), ground_lines=[glines[0]])))


# --------------------------------------------------------------------------- 5: legacy homography
def test_legacy_homography():
    # Build a homography from 4+ correspondences, then check resolve_model + apply_transform.
    cam = np.array([[100, 100], [900, 120], [880, 700], [120, 680], [500, 400]], dtype=np.float64)
    ortho = np.array([[0, 0], [1000, 0], [1000, 1000], [0, 1000], [500, 520]], dtype=np.float64)
    H, _ = compute_homography(cam, ortho)
    entry = {"H": H.tolist()}
    model = resolve_model(entry)
    check("legacy: resolve_model -> homography", model["type"] == "homography")
    a = apply_transform(model, cam)
    b = project_points(H, cam)
    check("legacy: apply_transform matches project_points", np.allclose(a, b),
          f"max diff {np.abs(a - b).max():.2e}")


# --------------------------------------------------------------------------- 6: clip_to_extent
def test_clip_to_extent():
    extent = [0.0, 0.0, 100.0, 100.0]
    pts = np.array([[50, 50], [-10, 20], [50, 200], [99, 1]], dtype=np.float64)
    out = clip_to_extent(pts, extent)
    inside_ok = np.isfinite(out[0]).all() and np.isfinite(out[3]).all()
    outside_nan = (not np.isfinite(out[1]).any()) and (not np.isfinite(out[2]).any())
    check("clip: inside rows kept", inside_ok)
    check("clip: outside rows -> NaN", outside_nan)
    check("clip: None extent is identity",
          np.array_equal(clip_to_extent(pts, None), pts))


# --------------------------------------------------------------------------- driver
def main():
    print("=== test_warp ===")
    test_distortion()
    test_polynomial()
    test_center_pillar()
    test_ground_poly()
    test_adaptive()
    test_fence_lines()
    test_save_persists_fence()
    test_save_merge_safe()
    test_robust_and_guard()
    test_joint_calibration()
    test_joint_tiepoints()
    test_ground_lines()
    test_legacy_homography()
    test_clip_to_extent()
    print("=================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
