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
from .config import Config
from .pipeline import localize as run_localize
from .scene import regions
from .scene.panels import load_panels, save_panels


class PanelsReq(BaseModel):
    # ortho[k] = {"id": str, "centerline": [[x,y],...]} on the orthophoto (site-wide)
    ortho: list[dict] = []
    # cameras[cam] = [{"id": str, "centerline": [[x,y],...], "width": float}, ...] in image px
    cameras: dict[str, list[dict]] = {}


class AreasReq(BaseModel):
    # areas[camera] = [{"id","name","camera_polygon","ortho_polygon"}, ...]
    areas: dict[str, list[dict]] = {}


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
        c.close()
        ortho = None
        if config.paths.orthophoto and Path(config.paths.orthophoto).exists():
            w, h = _img_size(config.paths.orthophoto)
            ortho = {"url": "/api/img/orthophoto", "width": w, "height": h}
        return {"cameras": cams, "kpis": kpis, "orthophoto": ortho,
                "references": refs,
                "posture_enabled": config.posture.enabled,
                "panels": load_panels(config.paths.panels)}

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

    @app.get("/api/areas")
    def get_areas():
        return regions.load_count_areas(config.paths.count_areas)

    @app.post("/api/areas")
    def set_areas(req: AreasReq):
        regions.save_count_areas(config.paths.count_areas, req.areas)
        run_localize(config)
        return {"ok": True}

    @app.get("/api/area-counts")
    def area_counts(frame: int | None = None):
        """Cow counts per region AT A SINGLE FRAME, split by posture. The count is
        the cows present in each area at exactly `frame_idx == frame` — it does NOT
        accumulate over a window. When `frame` is omitted, the latest frame is used.

        Returns `counts` (total per region) plus `postures`
        (`{region_id: {standing, lying, unknown}}`) for the per-area composition
        ring on the map. Posture is the reused mask-elongation proxy; NULL -> unknown.
        """
        c = con()
        if frame is None:
            row = c.execute("SELECT max(frame_idx) FROM frames").fetchone()
            frame = int(row[0]) if row and row[0] is not None else None
        rows = (
            []
            if frame is None
            else c.execute(
                "SELECT d.region_id, coalesce(d.posture, 'unknown') AS posture, count(*) "
                "FROM detections d "
                "JOIN frames f ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path "
                "WHERE d.region_id IS NOT NULL AND f.frame_idx = ? "
                "GROUP BY d.region_id, posture",
                [frame],
            ).fetchall()
        )
        c.close()
        counts: dict[str, int] = {}
        postures: dict[str, dict[str, int]] = {}
        for rid, posture, n in rows:
            n = int(n)
            counts[rid] = counts.get(rid, 0) + n
            slot = postures.setdefault(rid, {"standing": 0, "lying": 0, "unknown": 0})
            slot[posture if posture in slot else "unknown"] += n
        return {"counts": counts, "postures": postures, "frame": frame}

    @app.get("/api/area-summary")
    def area_summary():
        """Whole-day per-area totals + standing/lying split (static KPI list)."""
        c = con()
        df = db.area_summary(c)
        c.close()
        return _records(df)

    @app.get("/api/area-counts/over-time")
    def area_counts_over_time(camera: str | None = None, trunc: str = "hour"):
        c = con()
        df = db.area_counts_over_time(c, camera, trunc)
        c.close()
        return {"series": _records(df)}

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

    # ------------------------------------------------------------------ panels
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
