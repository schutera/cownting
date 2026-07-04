"""FastAPI backend: thin JSON + image layer over the DuckDB store and pipeline.

Serves the React frontend in production (mounts frontend/dist at /).
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db
from .calib import (
    fence_lines_to_pairs,
    fit_calibration,
    load_all,
    save_calibration,
)
from .calib.fence import load_fence, save_fence
from .calib.joint import joint_calibrate
from .calib.tiepoints import load_tiepoints, save_tiepoints
from .config import Config
from .pipeline import localize as run_localize
from .scene.panels import load_panels, save_panels


class FenceReq(BaseModel):
    polygon: list[list[float]] = []   # ortho-px vertices; <3 clears the fence


class TiePointsReq(BaseModel):
    # each tie point = [{"camera": id, "pt": [x, y]}, ...]  (same ground pt in >=2 cams)
    tiepoints: list[list[dict]] = []


class PanelsReq(BaseModel):
    # ortho[k] = {"id": str, "centerline": [[x,y],...]} on the orthophoto (site-wide)
    ortho: list[dict] = []
    # cameras[cam] = [{"id": str, "centerline": [[x,y],...], "width": float}, ...] in image px
    cameras: dict[str, list[dict]] = {}


class CalibReq(BaseModel):
    camera: str
    image_size: list[int]                       # camera reference image [w, h]
    lines: list[list[list[float]]] = []         # fisheye undistortion lines; each = [x,y] camera px
    # each ground line = [cam_polyline, ortho_polyline] of the same straight ground
    # feature; endpoints need not correspond (length-agnostic point-on-line)
    ground_lines: list[list[list[list[float]]]] = []
    # each panel line = [cam_ring, ortho_ring]; matched footprint corners are ground anchors
    panel_lines: list[list[list[list[float]]]] = []
    h_center: float | None = None


def _records(df) -> list[dict]:
    return json.loads(df.to_json(orient="records", date_format="iso"))


def _img_size(path: str) -> tuple[int, int]:
    from PIL import Image

    with Image.open(path) as im:
        return im.size


def create_app(config: Config) -> FastAPI:
    app = FastAPI(title="Cownting API")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    def con():
        return db.connect(config.paths.db_path, read_only=True)

    # ------------------------------------------------------------------ data
    @app.get("/api/site")
    def site():
        c = con()
        cams = db.cameras(c)
        kpis = db.kpi_summary(c)
        refs = {}
        for cam in cams:
            rf = db.reference_frame(c, cam)
            if rf:
                w, h = _img_size(rf)
                refs[cam] = {"url": f"/api/img/reference/{cam}", "width": w, "height": h}
        # Map coverage: how many of each camera's detections actually localize onto
        # the orthophoto (world_x set) vs get dropped (out of extent / fence / blowup).
        cov_rows = c.execute(
            "SELECT camera_id, count(*), count(world_x) FROM detections GROUP BY camera_id"
        ).fetchall()
        coverage = {cam: {"detections": int(tot), "localized": int(loc)} for cam, tot, loc in cov_rows}
        c.close()
        ortho = None
        if config.paths.orthophoto and Path(config.paths.orthophoto).exists():
            w, h = _img_size(config.paths.orthophoto)
            ortho = {"url": "/api/img/orthophoto", "width": w, "height": h}
        return {"cameras": cams, "kpis": kpis, "orthophoto": ortho,
                "references": refs, "calibration": load_all(config.paths.calibration),
                "posture_enabled": config.posture.enabled,
                "fence": load_fence(config.paths.fence),
                "tiepoints": load_tiepoints(config.paths.tiepoints),
                "panels": load_panels(config.paths.panels),
                "coverage": coverage}

    @app.get("/api/counts")
    def counts(camera: str, trunc: str = "hour"):
        c = con()
        df = db.counts_over_time(c, camera, trunc)
        c.close()
        return _records(df)

    @app.get("/api/posture")
    def posture(camera: str, trunc: str = "hour"):
        c = con()
        df = db.posture_over_time(c, camera, trunc)
        c.close()
        if df.empty:
            return []
        wide = df.pivot_table(index="t", columns="posture", values="n", fill_value=0).reset_index()
        return _records(wide)

    @app.get("/api/frames")
    def frames(camera: str):
        c = con()
        df = db.frames_df(c, camera)
        c.close()
        return _records(df[["frame_idx", "ts"]])

    @app.get("/api/heatmap")
    def heatmap(frame: int | None = None, window: int = 15):
        """Localized detections. With `frame`, restrict to the TRAILING
        `window`-frame band ending at `frame` — i.e. [frame-window, frame],
        the accumulated last ~`window` minutes up to now (frame_idx ~= minutes),
        across all cameras. Without `frame`, the whole-day aggregate."""
        c = con()
        base = (
            "SELECT d.world_x, d.world_y, d.camera_id, f.frame_idx "
            "FROM detections d "
            "JOIN frames f ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path "
            "WHERE d.world_x IS NOT NULL AND d.world_y IS NOT NULL"
        )
        if frame is None:
            df = c.execute(base).df()
        else:
            df = c.execute(
                base + " AND f.frame_idx BETWEEN ? AND ?", [frame - window, frame]
            ).df()
        c.close()
        pts = df[["world_x", "world_y"]].to_numpy().tolist()
        cams = df["camera_id"].tolist()
        frames = [int(x) for x in df["frame_idx"].tolist()]
        ortho = None
        if config.paths.orthophoto and Path(config.paths.orthophoto).exists():
            w, h = _img_size(config.paths.orthophoto)
            ortho = {"width": w, "height": h}
        return {"points": pts, "cams": cams, "frames": frames,
                "orthophoto": ortho, "frame": frame, "window": window}

    @app.get("/api/timeline")
    def timeline():
        """Frame axis for the day scrubber: sorted frame indices + total cow
        detections per frame (summed across cameras) for an activity strip."""
        c = con()
        df = c.execute(
            """SELECT f.frame_idx AS frame_idx, count(d.detection_id) AS n
               FROM frames f
               LEFT JOIN detections d
                 ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path
               GROUP BY f.frame_idx ORDER BY f.frame_idx"""
        ).df()
        c.close()
        if df.empty:
            return {"frames": [], "counts": [], "min_frame": 0, "max_frame": 0}
        frames = [int(x) for x in df["frame_idx"].tolist()]
        counts = [int(x) for x in df["n"].tolist()]
        return {"frames": frames, "counts": counts,
                "min_frame": frames[0], "max_frame": frames[-1]}

    # ------------------------------------------------------------------ images
    @app.get("/api/img/orthophoto")
    def img_ortho():
        p = config.paths.orthophoto
        if not p or not Path(p).exists():
            raise HTTPException(404, "no orthophoto")
        return FileResponse(p)

    @app.get("/api/img/reference/{camera}")
    def img_reference(camera: str):
        """Reference frame for the calibration view."""
        c = con()
        rf = db.reference_frame(c, camera)
        c.close()
        if not rf or not Path(rf).exists():
            raise HTTPException(404, "no reference frame")
        return FileResponse(rf)

    @app.get("/api/img/frame/{camera}/{frame_idx}")
    def img_frame(camera: str, frame_idx: int, kind: str = "overlay"):
        c = con()
        row = c.execute(
            "SELECT frame_path, overlay_path FROM frames WHERE camera_id = ? AND frame_idx = ?",
            [camera, frame_idx],
        ).fetchone()
        c.close()
        if not row:
            raise HTTPException(404, "frame not found")
        path = row[1] if (kind == "overlay" and row[1]) else row[0]
        if not path or not Path(path).exists():
            raise HTTPException(404, "image missing")
        return FileResponse(path)

    # ------------------------------------------------------------------ calibration
    @app.post("/api/calibration")
    def calibrate(req: CalibReq):
        """Per-camera warp: fisheye undistortion + a ground map from ground lines
        and panel footprints (a single ground->ortho polynomial)."""
        valid_lines = [ln for ln in req.lines if len(ln) >= 5]
        if len(valid_lines) < 3:
            raise HTTPException(400, "need >= 3 lines, each with >= 5 points")
        # Panel footprint corners are height-0 ground anchors (expanded like fence lines).
        try:
            n_panel_pts = len(fence_lines_to_pairs(req.panel_lines, kind="panel"))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        # Ground is anchored by panel footprints + ground lines (point-on-line). With
        # ground lines present, the fitter's degeneracy guard has the final say.
        if not req.ground_lines and n_panel_pts < 6:
            raise HTTPException(
                400,
                f"need >= 6 ground anchors (panel footprint vertices) or >= 2 ground lines; "
                f"have {n_panel_pts} panel vertices, {len(req.ground_lines)} ground lines",
            )
        try:
            model, diag = fit_calibration(
                req.lines, [], [], None, req.image_size,
                ground_lines=req.ground_lines, panel_lines=req.panel_lines,
            )
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))
        save_calibration(
            config.paths.calibration, req.camera, model, diag,
            config.paths.orthophoto, req.lines, [], [], None,
            ground_lines=req.ground_lines, panel_lines=req.panel_lines,
        )
        return {
            "camera": req.camera,
            "method": model["type"],
            "reproj_error": diag["reproj_error"],
            "max_residual": diag["max_residual"],
            "line_residual": diag["line_residual"],
            "per_point_error": diag["per_point_error"],
            "n_ground_lines": len(req.ground_lines),
            "n_panel": len(req.panel_lines),
            "n_lines": len(req.lines),
        }

    @app.post("/api/fence")
    def set_fence(req: FenceReq):
        """Save (or clear, if <3 vertices) the site-wide enclosure polygon, then
        re-localize so out-of-fence detections drop from the heatmap."""
        if len(req.polygon) < 3:
            p = Path(config.paths.fence)
            if p.exists():
                p.unlink()
            return {"n_vertices": 0, "updated": run_localize(config)}
        save_fence(config.paths.fence, req.polygon)
        return {"n_vertices": len(req.polygon), "updated": run_localize(config)}

    @app.post("/api/tiepoints")
    def set_tiepoints(req: TiePointsReq):
        """Save the site-wide cross-camera tie points (used by Joint calibrate)."""
        save_tiepoints(config.paths.tiepoints, req.tiepoints)
        return {"n": len(load_tiepoints(config.paths.tiepoints))}

    @app.post("/api/panels")
    def set_panels(req: PanelsReq):
        """Save the solar-panel footprints (site-wide ortho + per-camera image px),
        then re-localize so per-detection shelter flags are recomputed."""
        save_panels(config.paths.panels, {"ortho": req.ortho, "cameras": req.cameras})
        n_cameras = sum(len(v) for v in req.cameras.values())
        return {"n_ortho": len(req.ortho), "n_cameras": n_cameras,
                "updated": run_localize(config)}

    @app.get("/api/shelter")
    def shelter(camera: str = "all", trunc: str = "hour"):
        """Sheltering (under-panel) counts over time. `camera='all'` (or missing)
        aggregates across all cameras."""
        cam = None if camera == "all" else camera
        c = con()
        df = db.shelter_over_time(c, cam, trunc)
        c.close()
        return _records(df)

    @app.post("/api/localize")
    def localize():
        return {"updated": run_localize(config)}

    @app.post("/api/calibration/joint")
    def calibrate_joint():
        """Re-fit ALL cameras together, coupled through shared fence corners, so
        overlapping views agree in the orthophoto. Uses each camera's stored
        inputs; writes the refined models and re-localizes."""
        entries = load_all(config.paths.calibration)
        if not entries:
            raise HTTPException(400, "no per-camera calibrations yet — calibrate cameras first")
        c = con()
        inputs, sizes, cams = {}, {}, []
        for cam, e in entries.items():
            inputs[cam] = {
                "lines": e.get("lines") or [],
                "center_pairs": e.get("center_pairs") or [],
                "ground_pairs": e.get("ground_pairs") or [],
                "ground_lines": e.get("ground_lines") or [],
                "fence_lines": e.get("fence_lines") or [],
                "panel_lines": e.get("panel_lines") or [],
                "h_center": e.get("h_center"),
            }
            rf = db.reference_frame(c, cam)
            if rf and Path(rf).exists():
                sizes[cam] = _img_size(rf)
            else:  # fall back to the size implied by the stored distortion center
                dm = (e.get("model") or {}).get("distortion")
                sizes[cam] = (int(2 * dm["center"][0]), int(2 * dm["center"][1])) if dm else (1920, 1080)
            cams.append(cam)
        c.close()
        tie_points = load_tiepoints(config.paths.tiepoints)
        try:
            results, gdiag = joint_calibrate(cams, inputs, sizes, tie_points=tie_points)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))
        if not results:
            raise HTTPException(400, "no cameras could be jointly calibrated")
        for cam, r in results.items():
            e = entries[cam]
            save_calibration(
                config.paths.calibration, cam, r["model"], r["diag"],
                config.paths.orthophoto,
                e.get("lines") or [], e.get("center_pairs") or [], e.get("ground_pairs") or [],
                e.get("h_center"), fence_lines=e.get("fence_lines") or [],
                ground_lines=e.get("ground_lines") or [],
                panel_lines=e.get("panel_lines") or [],
            )
        per_camera = {
            cam: {
                "reproj_error": r["diag"]["reproj_error"],
                "max_residual": r["diag"]["max_residual"],
                "line_residual": r["diag"]["line_residual"],
                "n_shared": r["n_shared"],
                "method": r["model"]["type"],
            }
            for cam, r in results.items()
        }
        return {"cameras": list(results.keys()), "global": gdiag,
                "per_camera": per_camera, "updated": run_localize(config)}

    # ------------------------------------------------------------------ static frontend (prod)
    dist = Path(__file__).resolve().parent.parent / "frontend" / "dist"
    if dist.exists():
        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            if full_path.startswith("api/"):
                raise HTTPException(404, "not found")
            candidate = dist / full_path
            if full_path and candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(dist / "index.html"))  # client-side routing fallback

    return app
