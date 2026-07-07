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
        # Read-write, NOT read_only: DuckDB rejects opening a second connection to
        # the same file with a different mode in one process ("Can't open a
        # connection ... with a different configuration"). The save path
        # (run_localize) needs a writer, so a read_only reader open at the same
        # moment (e.g. the dashboard polling during a save) would make that write
        # connection fail and 500 the POST. Everyone shares one mode.
        return db.connect(config.paths.db_path)

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
                "posture_enabled": config.posture.enabled}

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

    @app.get("/api/panel-areas")
    def get_panel_areas():
        """Shelter regions (same polygon shape as count areas). A cow inside one
        is 'under a panel'."""
        return regions.load_count_areas(config.paths.panel_areas)

    @app.post("/api/panel-areas")
    def set_panel_areas(req: AreasReq):
        regions.save_count_areas(config.paths.panel_areas, req.areas)
        run_localize(config)
        return {"ok": True}

    @app.get("/api/area-counts")
    def area_counts(frame: int | None = None):
        """Cow counts per region, split by posture, for the occupancy map.

        With `frame`: the cows present in each area at exactly `frame_idx == frame`
        (does NOT accumulate over a window). Without `frame` (the map's "whole day"
        toggle): the whole-day PEAK simultaneous occupancy per area — NOT the latest
        frame, which at dusk is empty and used to blank the map.

        Returns `counts` (total per region) plus `postures`
        (`{region_id: {standing, lying, unknown}}`) for the per-area composition
        ring, and `sheltering` (`{region_id: n}`) = that area's cows under a panel,
        for the unit-block indicator. Posture is the reused proxy; NULL -> unknown.
        """
        c = con()
        if frame is None:
            df = db.area_counts_whole_day(c)
            c.close()
            counts = {r.region_id: int(r.peak) for r in df.itertuples()}
            sheltering = {r.region_id: int(r.sheltering) for r in df.itertuples()}
            postures = {
                r.region_id: {
                    "standing": int(r.standing),
                    "lying": int(r.lying),
                    "unknown": int(r.unknown),
                }
                for r in df.itertuples()
            }
            return {
                "counts": counts,
                "postures": postures,
                "sheltering": sheltering,
                "frame": None,
            }
        rows = (
            []
            if frame is None
            else c.execute(
                "SELECT d.region_id, coalesce(d.posture, 'unknown') AS posture, "
                "       count(*) AS n, count(*) FILTER (WHERE d.under_panel) AS shel "
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
        sheltering: dict[str, int] = {}
        for rid, posture, n, shel in rows:
            n = int(n)
            counts[rid] = counts.get(rid, 0) + n
            sheltering[rid] = sheltering.get(rid, 0) + int(shel or 0)
            slot = postures.setdefault(rid, {"standing": 0, "lying": 0, "unknown": 0})
            slot[posture if posture in slot else "unknown"] += n
        return {"counts": counts, "postures": postures, "sheltering": sheltering, "frame": frame}

    @app.get("/api/day-series")
    def day_series():
        """Per-frame metric arrays (summed across cameras) for the time-of-day bar
        strips: frames + total / standing / lying / sheltering / open. Same frame
        axis as the scrubber's activity strip."""
        c = con()
        df = db.day_series(c)
        c.close()
        keys = ["total", "standing", "lying", "sheltering", "open"]
        if df.empty:
            return {"frames": [], **{k: [] for k in keys}}
        return {
            "frames": [int(x) for x in df["frame_idx"].tolist()],
            **{k: [int(x) for x in df[k].tolist()] for k in keys},
        }

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

    # ------------------------------------------------------------------ shelter
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
