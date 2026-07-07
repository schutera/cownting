"""Orchestration for the offline batch stages: ingest, segment, localize."""
from __future__ import annotations

from pathlib import Path

import cv2
import pandas as pd

from . import db
from .config import Config
from .detect import build_segmenter
from .detect.overlay import render_overlay
from .ingest import index_video
from .scene.regions import assign_regions, load_count_areas


def ingest(config: Config) -> int:
    """Decode every camera's video into the frames table. Returns frames indexed."""
    con = db.connect(config.paths.db_path)
    db.init_db(con)
    total = 0
    for cam in config.cameras:
        frames = index_video(cam, config.ingest, config.paths.artifacts_dir)
        db.insert_frames(con, frames)
        total += len(frames)
        print(f"[ingest] {cam.id}: {len(frames)} frames")
    con.close()
    return total


def segment(config: Config, limit: int | None = None) -> int:
    """Run the segmenter on unprocessed frames; write detections + overlays.

    Region assignment happens later in `localize`.
    """
    con = db.connect(config.paths.db_path)
    db.init_db(con)

    pending = db.unprocessed_frames(con)
    if limit:
        pending = pending.head(limit)
    if pending.empty:
        print("[segment] nothing to do")
        con.close()
        return 0

    segmenter = build_segmenter(config.detect, config.posture)
    overlay_dir = Path(config.paths.artifacts_dir) / "overlays"

    n_det = 0
    for _, fr in pending.iterrows():
        image = cv2.imread(fr["frame_path"])
        if image is None:
            db.mark_processed(con, fr["camera_id"], int(fr["frame_idx"]), None)
            continue
        instances = segmenter.segment(image)

        rows = []
        for inst in instances:
            row = dict(
                camera_id=fr["camera_id"], ts=fr["ts"], time_bin=int(fr["time_bin"]),
                frame_path=fr["frame_path"], score=inst.score,
                bbox_x1=inst.bbox[0], bbox_y1=inst.bbox[1], bbox_x2=inst.bbox[2], bbox_y2=inst.bbox[3],
                area_px=inst.area_px, ground_px_x=inst.ground_px[0], ground_px_y=inst.ground_px[1],
                posture=inst.posture,
            )
            rows.append(row)

        if rows:
            db.insert_detections(con, pd.DataFrame(rows))
            n_det += len(rows)

        ov_path = str(overlay_dir / fr["camera_id"] / f"{int(fr['frame_idx']):08d}.jpg")
        render_overlay(image, instances, ov_path)
        db.mark_processed(con, fr["camera_id"], int(fr["frame_idx"]), ov_path)

    print(f"[segment] {len(pending)} frames -> {n_det} detections")
    con.close()
    return n_det


def localize(config: Config) -> int:
    """Assign every detection to a count area (image-space, per camera)."""
    con = db.connect(config.paths.db_path)
    areas = load_count_areas(config.paths.count_areas)

    updated = 0
    for camera_id in areas:
        cam_areas = areas.get(camera_id, [])
        if not cam_areas:
            continue
        dets = con.execute(
            "SELECT detection_id, ground_px_x, ground_px_y FROM detections WHERE camera_id = ?",
            [camera_id],
        ).df()
        if dets.empty:
            continue
        region_ids = assign_regions(
            dets[["ground_px_x", "ground_px_y"]].to_numpy(), cam_areas, camera_id,
        )
        dets["region_id"] = pd.array(region_ids, dtype=object)
        db.update_region(con, dets[["detection_id", "region_id"]])
        updated += len(dets)

    # Shelter (panel) assignment — image-space & per-camera, so it is INDEPENDENT of
    # world_x/fence: compute for EVERY detection of a camera that has drawn footprints.
    from .scene.panels import assign_panels, camera_panels, load_panels

    panels = load_panels(config.paths.panels)
    if panels is not None:
        for camera_id in panels.get("cameras", {}):
            if not camera_panels(panels, camera_id):
                continue
            sdets = con.execute(
                "SELECT detection_id, ground_px_x, ground_px_y FROM detections WHERE camera_id = ?",
                [camera_id],
            ).df()
            if sdets.empty:
                continue
            res = assign_panels(
                sdets[["ground_px_x", "ground_px_y"]].to_numpy(),
                camera_id, panels, config.shade.margin_px,
            )
            sdets["under_panel"] = pd.array(res["under_panel"], dtype=object)
            sdets["near_infra"] = pd.array(res["boundary"], dtype=object)
            sdets["panel_id"] = pd.array(res["panel_id"], dtype=object)
            db.update_shelter(con, sdets)

    print(f"[localize] updated {updated} detections")
    con.close()
    return updated
