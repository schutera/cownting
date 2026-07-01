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
from .calib import compute_homography, load_all, save_homography
from .config import Config
from .pipeline import localize as run_localize


class CalibReq(BaseModel):
    camera: str
    cam_points: list[list[float]]     # full-resolution camera pixels
    ortho_points: list[list[float]]   # full-resolution orthophoto pixels


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
                "references": refs, "calibration": load_all(config.paths.calibration)}

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
    def heatmap():
        c = con()
        df = c.execute(
            "SELECT world_x, world_y FROM detections WHERE world_x IS NOT NULL AND world_y IS NOT NULL"
        ).df()
        c.close()
        pts = df[["world_x", "world_y"]].to_numpy().tolist()
        ortho = None
        if config.paths.orthophoto and Path(config.paths.orthophoto).exists():
            w, h = _img_size(config.paths.orthophoto)
            ortho = {"width": w, "height": h}
        return {"points": pts, "orthophoto": ortho}

    # ------------------------------------------------------------------ images
    @app.get("/api/img/orthophoto")
    def img_ortho():
        p = config.paths.orthophoto
        if not p or not Path(p).exists():
            raise HTTPException(404, "no orthophoto")
        return FileResponse(p)

    @app.get("/api/img/reference/{camera}")
    def img_reference(camera: str):
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
    def save_calibration(req: CalibReq):
        if len(req.cam_points) < 4 or len(req.cam_points) != len(req.ortho_points):
            raise HTTPException(400, "need >= 4 matched pairs")
        try:
            H, err = compute_homography(req.cam_points, req.ortho_points)
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(400, str(exc))
        save_homography(config.paths.calibration, req.camera, H, err,
                        config.paths.orthophoto, req.cam_points, req.ortho_points)
        return {"camera": req.camera, "reproj_error": err, "n_points": len(req.cam_points)}

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
