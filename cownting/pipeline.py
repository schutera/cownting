"""Orchestration for the offline batch stages: ingest, segment, localize."""
from __future__ import annotations

from pathlib import Path

import cv2
import pandas as pd

from . import db
from .calib import load_all, project_points
from .config import Config
from .detect import build_segmenter
from .detect.overlay import render_overlay
from .ingest import index_video
from .scene.quality import assess_quality as _assess_frame


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


def assess_quality(config: Config) -> dict:
    """Classify every frame (ok / foggy / dark) and store it in frame_quality.

    Empty frames stay 'ok' — they are valid occupancy=0 measurements.
    """
    con = db.connect(config.paths.db_path)
    db.init_db(con)
    frames = db.all_frames(con)
    if frames.empty:
        con.close()
        print("[quality] no frames — run ingest first")
        return {}

    rows = []
    for _, fr in frames.iterrows():
        img = cv2.imread(fr["frame_path"])
        q = "missing" if img is None else _assess_frame(img, config.quality)[0]
        rows.append(dict(camera_id=fr["camera_id"], frame_idx=int(fr["frame_idx"]), frame_quality=q))
    dfq = pd.DataFrame(rows)
    db.update_frame_quality(con, dfq)
    con.close()
    breakdown = dfq["frame_quality"].value_counts().to_dict()
    print("[quality]", breakdown)
    return breakdown


def segment(config: Config, limit: int | None = None) -> int:
    """Run the segmenter on unprocessed frames; write detections + overlays.

    If calibration already exists, world coords are filled at the same time;
    otherwise run `localize` after calibrating.
    """
    con = db.connect(config.paths.db_path)
    db.init_db(con)

    skip_blind = config.quality.enabled and config.quality.skip_blind_in_segment
    pending = db.unprocessed_frames(con, skip_blind=skip_blind)
    if limit:
        pending = pending.head(limit)
    if pending.empty:
        print("[segment] nothing to do")
        con.close()
        return 0

    segmenter = build_segmenter(config.detect, config.posture)
    calib = load_all(config.paths.calibration)
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
            cam_cal = calib.get(fr["camera_id"])
            if cam_cal:
                wx, wy = project_points(cam_cal["H"], [inst.ground_px])[0]
                row["world_x"], row["world_y"] = float(wx), float(wy)
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
    """(Re)project every detection's ground point through the current calibration."""
    con = db.connect(config.paths.db_path)
    calib = load_all(config.paths.calibration)
    if not calib:
        print("[localize] no calibration found — run the Calibration tab first")
        con.close()
        return 0

    updated = 0
    for camera_id, cam_cal in calib.items():
        dets = con.execute(
            "SELECT detection_id, ground_px_x, ground_px_y FROM detections WHERE camera_id = ?",
            [camera_id],
        ).df()
        if dets.empty:
            continue
        world = project_points(cam_cal["H"], dets[["ground_px_x", "ground_px_y"]].to_numpy())
        dets["world_x"] = world[:, 0]
        dets["world_y"] = world[:, 1]
        db.update_world(con, dets)
        updated += len(dets)
    print(f"[localize] updated {updated} detections")
    con.close()
    return updated
