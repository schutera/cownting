"""Orchestration for the offline batch stages: ingest, segment, localize."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

import cv2
import pandas as pd

from . import db
from .config import Config, resolve_dataset
from .detect import build_pose_estimator, build_segmenter
from .detect.base import Instance
from .detect.overlay import render_overlay, render_pose_overlay
from .ingest import index_video
from .scene.regions import assign_regions, load_count_areas


def ingest(config: Config) -> int:
    """Decode every camera's video into the frames table for one data-package.

    Idempotent: the dataset is resolved from config, its prior rows + per-dataset
    artifacts are purged, then re-ingested — so re-running replaces rather than
    duplicating. Returns frames indexed.
    """
    dataset_id, day, label = resolve_dataset(config)
    con = db.connect(config.paths.db_path)
    db.init_db(con)
    db.upsert_dataset(con, dataset_id, day, label, status="ingested")

    # Replace-on-reingest: drop this dataset's DB rows and its artifact subtree.
    db.purge_dataset(con, dataset_id)
    ds_art = Path(config.paths.artifacts_dir) / dataset_id
    if ds_art.exists():
        shutil.rmtree(ds_art)

    total = 0
    for cam in config.cameras:
        frames = index_video(cam, config.ingest, config.paths.artifacts_dir, dataset_id)
        db.insert_frames(con, frames)
        total += len(frames)
        print(f"[ingest] {cam.id}: {len(frames)} frames")
    con.close()
    print(f"[ingest] dataset {dataset_id!r}: {total} frames")
    return total


def segment(config: Config, limit: int | None = None,
            on_progress: Callable[[int, int], None] | None = None) -> int:
    """Run the segmenter on unprocessed frames; write detections + overlays.

    Region assignment happens later in `localize`. `on_progress(done, total)` is
    called after each frame (if given) so a caller — e.g. the upload worker — can
    drive a progress bar through this, the batch's long pole.
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
    artifacts = Path(config.paths.artifacts_dir)

    total = len(pending)
    touched: set = set()
    n_det = 0
    for done, (_, fr) in enumerate(pending.iterrows(), start=1):
        ds = fr["dataset_id"]
        touched.add(ds)
        image = cv2.imread(fr["frame_path"])
        if image is None:
            db.mark_processed(con, ds, fr["camera_id"], int(fr["frame_idx"]), None)
            continue
        instances = segmenter.segment(image)

        rows = []
        for inst in instances:
            row = dict(
                dataset_id=ds,
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

        # Overlays live under the same per-dataset subtree as frames (None -> flat).
        base = artifacts / ds if ds else artifacts
        ov_path = str(base / "overlays" / fr["camera_id"] / f"{int(fr['frame_idx']):08d}.jpg")
        render_overlay(image, instances, ov_path)
        db.mark_processed(con, ds, fr["camera_id"], int(fr["frame_idx"]), ov_path)

        if on_progress is not None:
            on_progress(done, total)

    for ds in touched:
        if ds is not None:
            db.set_dataset_status(con, ds, "segmented")
    print(f"[segment] {len(pending)} frames -> {n_det} detections")
    con.close()
    return n_det


def localize(config: Config, dataset_id: str | None = None) -> int:
    """Assign every detection to a count area (image-space, per camera).

    Whole-DB by default (count/panel areas are per-camera and day-independent, so
    reassigning every day is correct and idempotent). Pass `dataset_id` to scope
    the reset + reassignment to one package (faster; used by the per-dataset
    process path)."""
    con = db.connect(config.paths.db_path)
    areas = load_count_areas(config.paths.count_areas)

    scope = " AND dataset_id = ?" if dataset_id is not None else ""
    reset_scope = " WHERE dataset_id = ?" if dataset_id is not None else ""
    dsp = [dataset_id] if dataset_id is not None else []

    # Reset assignments first so shrinking/removing an area (or a whole camera's
    # areas) clears stale region_id / shelter flags — recomputed fresh below.
    con.execute(
        f"UPDATE detections SET region_id = NULL, under_panel = NULL, panel_id = NULL{reset_scope}",
        dsp,
    )

    updated = 0
    for camera_id in areas:
        cam_areas = areas.get(camera_id, [])
        if not cam_areas:
            continue
        dets = con.execute(
            f"SELECT detection_id, ground_px_x, ground_px_y FROM detections WHERE camera_id = ?{scope}",
            [camera_id] + dsp,
        ).df()
        if dets.empty:
            continue
        region_ids = assign_regions(
            dets[["ground_px_x", "ground_px_y"]].to_numpy(), cam_areas, camera_id,
        )
        dets["region_id"] = pd.array(region_ids, dtype=object)
        db.update_region(con, dets[["detection_id", "region_id"]])
        updated += len(dets)

    # Shelter assignment — polygon "panel areas": the SAME per-camera, image-space
    # point-in-polygon test as count areas. A cow whose ground point falls inside
    # any of a camera's panel-area polygons counts as under a panel.
    panel_areas = load_count_areas(config.paths.panel_areas)
    for camera_id, cam_pareas in panel_areas.items():
        if not cam_pareas:
            continue
        sdets = con.execute(
            f"SELECT detection_id, ground_px_x, ground_px_y FROM detections WHERE camera_id = ?{scope}",
            [camera_id] + dsp,
        ).df()
        if sdets.empty:
            continue
        pids = assign_regions(
            sdets[["ground_px_x", "ground_px_y"]].to_numpy(), cam_pareas, camera_id,
        )
        sdets["under_panel"] = pd.array([p is not None for p in pids], dtype=object)
        sdets["panel_id"] = pd.array(pids, dtype=object)
        db.update_shelter(con, sdets)

    if dataset_id is not None:
        db.set_dataset_status(con, dataset_id, "localized")
    print(f"[localize] updated {updated} detections")
    con.close()
    return updated


def pose(config: Config, dataset_id: str | None = None, limit: int | None = None) -> int:
    """Standalone pose stage: AP-10K keypoints -> posture, reusing the stored
    detections (bbox + on-disk frame) so it runs WITHOUT re-segmenting.

    Decoupled from `segment` on purpose: masks aren't persisted, so pose crops
    each stored bbox straight from its frame and runs ViTPose on the raw crop. It
    overwrites `posture` with the pose class (standing/lying/grazing/unknown) and
    bakes a per-frame pose overlay served as `kind=pose`. Gated by
    `flags.pose_enabled`; GPU-side like segment, so `localize` stays model-free.
    Idempotent — re-running just recomputes posture + overlays."""
    if not config.flags.pose_enabled:
        print("[pose] flags.pose_enabled is false; nothing to do")
        return 0
    con = db.connect(config.paths.db_path)

    scope = " WHERE dataset_id = ?" if dataset_id is not None else ""
    dsp = [dataset_id] if dataset_id is not None else []
    dets = con.execute(
        f"SELECT detection_id, camera_id, frame_path, "
        f"bbox_x1, bbox_y1, bbox_x2, bbox_y2 FROM detections{scope}",
        dsp,
    ).df()
    if dets.empty:
        print("[pose] no detections to pose")
        con.close()
        return 0

    estimator = build_pose_estimator(config.pose)
    artifacts = Path(config.paths.artifacts_dir)
    frame_paths = list(dict.fromkeys(dets["frame_path"].tolist()))
    if limit:
        frame_paths = frame_paths[:limit]

    updated = n_frames = 0
    for fpath in frame_paths:
        grp = dets[dets["frame_path"] == fpath]
        image = cv2.imread(fpath)
        if image is None:
            continue
        insts = [
            Instance(bbox=(r.bbox_x1, r.bbox_y1, r.bbox_x2, r.bbox_y2),
                     score=1.0, area_px=0.0, ground_px=(0.0, 0.0))
            for r in grp.itertuples()
        ]
        estimator.estimate(image, insts)
        db.update_posture(con, pd.DataFrame({
            "detection_id": grp["detection_id"].to_numpy(),
            "posture": [i.posture for i in insts],
        }))
        # Mirror the frame's path into a sibling pose_overlays/ subtree. Rebuild via
        # path parts rather than a literal "/frames/" so it also works on Windows
        # (backslash) paths — the hardcoded slash silently collided every overlay.
        parts = list(Path(fpath).parts)
        if "frames" in parts:
            parts[parts.index("frames")] = "pose_overlays"
            pose_ov = str(Path(*parts))
        else:
            pose_ov = str(artifacts / "pose_overlays" / f"{Path(fpath).stem}.jpg")
        render_pose_overlay(image, insts, pose_ov, min_kpt_conf=config.pose.min_kpt_conf)
        db.set_pose_overlay(con, fpath, pose_ov)
        updated += len(insts)
        n_frames += 1
        if n_frames % 100 == 0:
            print(f"[pose] {n_frames}/{len(frame_paths)} frames")

    print(f"[pose] {n_frames} frames -> {updated} detections reposed")
    con.close()
    return updated


def process(config: Config, limit: int | None = None) -> dict[str, int]:
    """Run the full offline batch end to end: ingest -> segment -> localize.

    One call to take raw video all the way to dashboard-ready detections, so a
    caller (the CLI `process` command, or a future upload/auto-process worker)
    need not chain the stages by hand. Each stage opens and closes its own
    DuckDB connection, so they run strictly sequentially and never contend for
    the single writer. The pose stage runs only when `flags.pose_enabled`.
    """
    n_frames = ingest(config)
    n_det = segment(config, limit=limit)
    n_pose = pose(config) if config.flags.pose_enabled else 0
    n_loc = localize(config)
    print(f"[process] {n_frames} frames -> {n_det} detections -> "
          f"{n_pose} reposed -> {n_loc} localized")
    return {"frames": n_frames, "detections": n_det, "posed": n_pose, "localized": n_loc}
