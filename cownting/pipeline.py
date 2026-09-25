"""Orchestration for the offline batch stages: ingest, segment, localize."""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Callable

import cv2
import pandas as pd

from . import db
from .config import Config, resolve_dataset
from .detect import build_pose_estimator, build_segmenter
from .detect.base import Instance
from .detect.geometry import mask_to_polygon
from .detect.overlay import (render_overlay, render_overlay_from_polygons,
                             render_pose_overlay)
from .ingest import index_video
from .scene import regions


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


def _assert_decodable(video: str) -> None:
    """Confirm a video exists, opens, and yields at least one decodable frame —
    raised BEFORE any destructive purge so a corrupt/non-video replacement can't
    destroy the stream it was meant to replace (a single stream has no archive,
    unlike a whole-day delete)."""
    if not Path(video).exists():
        raise FileNotFoundError(video)
    cap = cv2.VideoCapture(video)
    try:
        readable = bool(cap.isOpened() and cap.grab() and cap.retrieve()[0])
    finally:
        cap.release()
    if not readable:
        raise RuntimeError(f"unreadable or empty video (refusing to replace): {video}")


def ingest_one_camera(config: Config, dataset_id: str, cam) -> int:
    """Ingest ONE camera's video into an EXISTING dataset without disturbing the
    others — unlike `ingest`, which purges the whole dataset and rmtrees its whole
    artifact subtree. Used to add or replace a single camera stream (e.g. swap a
    failed camera for a healthy re-upload).

    Idempotent for that one camera: its prior rows + on-disk frames/overlays are
    dropped first, then re-indexed, so re-adding replaces rather than duplicates.
    The dataset's day/label and every OTHER camera are left untouched. Returns
    frames indexed.
    """
    # Validate the replacement decodes BEFORE touching the existing stream: a
    # corrupt upload must fail loudly with the old data still intact, not wipe it.
    _assert_decodable(cam.video)
    con = db.connect(config.paths.db_path)
    try:
        db.init_db(con)
        # Keep the dataset's existing day/label (coalesce None); just ensure the row
        # exists and mark it back to 'ingested' until the new stream re-localizes.
        db.upsert_dataset(con, dataset_id, status="ingested")
        # Replace just this camera: drop its rows and its artifact subdirs.
        db.purge_dataset(con, dataset_id, camera_id=cam.id)
        ds_art = Path(config.paths.artifacts_dir) / dataset_id
        for sub in ("frames", "overlays", "pose_overlays"):
            shutil.rmtree(ds_art / sub / cam.id, ignore_errors=True)
        frames = index_video(cam, config.ingest, config.paths.artifacts_dir, dataset_id)
        db.insert_frames(con, frames)
    finally:
        con.close()
    print(f"[ingest_one] {cam.id}: {len(frames)} frames into {dataset_id!r}")
    return len(frames)


def reoverlay(config: Config, *, dataset_id: str | None = None,
              camera_id: str | None = None, limit: int | None = None,
              on_progress: Callable[[int, int], None] | None = None,
              should_stop: Callable[[], bool] | None = None,
              force: bool = False) -> dict:
    """Repaint frame overlays from the database, hiding rejected detections.

    WHY THIS EXISTS. The overlay is painted by `segment`, which runs BEFORE the
    crop model has scored anything — so at the moment the masks are burned in,
    nothing knows which of them are cows. The verdict arrives minutes later, and
    changes again every time a new model is trained. An overlay is therefore a
    CACHE of a model's opinion, and this is its invalidation pass: it rebuilds
    the image from `mask_poly` + `attr_is_cow`, with no GPU and no segmenter,
    and stamps `frames.overlay_rev` with the rev it painted for.

    That stamp is what makes the pass idempotent, resumable, and automatic: a
    frame is selected only when its overlay_rev differs from the rev its own
    detections now carry, so re-running is free, an interrupted run continues,
    and a re-score marks exactly the affected frames dirty without anybody
    having to remember to say so.

    A detection is hidden iff `attr_is_cow IS FALSE` — the model looked and said
    no. NULL means never scored, which is most of the corpus most of the time,
    and must render exactly as it does today (`db.mask_drawn_clause`).

    Deliberately NOT gated on `instance_attrs.gate_enabled`. That flag decides
    what counts; this decides what is painted. Nobody opts in to not having a
    false positive tinted as a cow.

    FIDELITY: the stored outline is the largest external contour only, so a cow
    split by an occluding panel comes back as its bigger half (`mask_parts`
    records the original count). Recovering the rest means re-running the
    segmenter — hours, against minutes for this.
    """
    con = db.connect(config.paths.db_path)
    try:
        db.init_db(con)     # `overlay_rev` may predate this deployment
        where = [
            "f.processed = TRUE", "f.overlay_path IS NOT NULL", "f.overlay_path <> ''",
            "f.frame_path IS NOT NULL", "f.frame_path <> ''",
        ]
        params: list = []
        if dataset_id:
            where.append("f.dataset_id = ?")
            params.append(dataset_id)
        if camera_id:
            where.append("f.camera_id = ?")
            params.append(camera_id)
        # The staleness predicate, and the reason the pass needs no bookkeeping:
        # a frame is dirty when the model rev its detections carry is not the rev
        # its overlay was painted for. `max(attr_model_rev)` over the frame's
        # detections is the frame's rev — a frame mid-score can hold two, and
        # picking either leaves it dirty until scoring finishes, which is right.
        stale = ("(SELECT max(d.attr_model_rev) FROM detections d "
                 "WHERE d.frame_path = f.frame_path AND d.camera_id = f.camera_id "
                 "AND d.dataset_id IS NOT DISTINCT FROM f.dataset_id)")
        if not force:
            where.append(f"f.overlay_rev IS DISTINCT FROM {stale}")
        sql = (f"SELECT f.dataset_id, f.camera_id, f.frame_path, f.overlay_path, {stale} AS rev "
               f"FROM frames f WHERE {' AND '.join(where)} "
               f"ORDER BY f.dataset_id, f.camera_id, f.frame_idx")
        if limit:
            sql += f" LIMIT {int(limit)}"
        frames = con.execute(sql, params).fetchall()

        total = len(frames)
        painted = missing = unchanged = hidden = failed = 0
        stopped = False
        for done, (ds, cam, frame_path, overlay_path, rev) in enumerate(frames, start=1):
            # Between frames, like remask: this pass is minutes and an upload is
            # minutes too, but a corpus-wide run should still yield rather than
            # make someone wait. Resuming costs nothing — the staleness predicate
            # picks up exactly where this stopped.
            if should_stop is not None and should_stop():
                stopped = True
                break
            rows = con.execute(
                f"""SELECT mask_poly, ground_px_x, ground_px_y, {db.mask_drawn_clause()}
                    FROM detections
                    WHERE frame_path = ? AND camera_id = ?
                      AND dataset_id IS NOT DISTINCT FROM ?
                    ORDER BY detection_id""",
                [frame_path, cam, ds],
            ).fetchall()
            image = cv2.imread(frame_path)
            if image is None:
                # The raw frame is gone (a clipped camera, a pruned day). Leave
                # the existing overlay alone rather than destroying it, and leave
                # the frame dirty so a later run retries.
                missing += 1
                if on_progress:
                    on_progress(done, total)
                continue
            # Per-frame isolation. Without it one unparseable `mask_poly` ends
            # the whole pass, and because the frame order is deterministic and
            # `overlay_rev` is only stamped on success, every retry dies on the
            # same row — so every frame after it stays on the old model's masks
            # for good. Count it, leave the frame dirty, keep going.
            try:
                items = []
                hid = 0
                for poly, gx, gy, draw in rows:
                    pts = json.loads(poly) if poly else None
                    ground = (gx, gy) if gx is not None and gy is not None else None
                    items.append((pts, ground, bool(draw)))
                    if not draw:
                        hid += 1
                render_overlay_from_polygons(image, items, overlay_path)
            except Exception as e:  # noqa: BLE001 — one bad frame is not a failed pass
                failed += 1
                print(f"[cownting.reoverlay] {frame_path}: {type(e).__name__}: {e}")
                if on_progress:
                    on_progress(done, total)
                continue
            # Only now, with the JPEG proven written, is the frame current.
            con.execute(
                "UPDATE frames SET overlay_rev = ? WHERE frame_path = ? AND camera_id = ? "
                "AND dataset_id IS NOT DISTINCT FROM ?",
                [rev, frame_path, cam, ds],
            )
            hidden += hid
            painted += 1
            if on_progress:
                on_progress(done, total)
        return {"frames": total, "painted": painted, "hidden": hidden,
                "missing_frames": missing, "unchanged": unchanged,
                "failed": failed, "stopped": stopped}
    finally:
        # Same DuckDB single-writer rule as every other pass: a leaked write
        # handle is a lock every later request fails on for the life of the
        # process, not merely a leaked descriptor.
        con.close()


def remask(config: Config, limit: int | None = None,
           on_progress: Callable[[int, int], None] | None = None,
           dataset_id: str | None = None, camera_id: str | None = None,
           min_iou: float = 0.9,
           should_stop: Callable[[], bool] | None = None) -> dict:
    """Backfill `mask_poly` / `mask_parts` on ALREADY-PROCESSED frames.

    `segment` only touches frames where `processed = FALSE`, so every day ingested
    before outlines were persisted has detections with a bbox and no outline —
    which is the entire existing corpus. This re-runs the segmenter over those
    frames and writes THE TWO MASK COLUMNS ONLY.

    **UPDATE-only, matched by IoU, and that shape is not a nicety.** `bbox_*`,
    `frame_path`, `camera_id`, `dataset_id` and the ordinal are all key material
    for `labels_db.instance_key`, and `score`/`area_px`/`ground_px_*`/`ts` are the
    ORDERING columns of the ordinal window. Re-inserting detections — or
    refreshing any of those from the new prediction — would renumber ordinals,
    re-mint every key and orphan every label already collected, which is precisely
    the damage `reconcile_dataset` exists to repair after a re-ingest. A backfill
    must never be able to cause it. So each re-predicted mask is matched to an
    EXISTING row by IoU against the stored bbox, and only the two new columns are
    written.

    A re-prediction is not guaranteed to reproduce the original detections: weights
    drift, and a box that was found then may not be found now. Unmatched rows keep
    `mask_poly` NULL and the run reports the match rate, so a poor run is visible
    rather than silently partial.

    Returns `{"frames": n, "matched": n, "detections": n, "unmatched": n}`.
    """
    con = db.connect(config.paths.db_path)
    try:
        return _remask(con, config, limit, on_progress, dataset_id, camera_id,
                       min_iou, should_stop)
    finally:
        # A leaked write handle is not a leaked file descriptor here: DuckDB
        # allows one read-write process per file, and this runs INSIDE the API
        # server, so a connection left open by a failed pass would lock every
        # later request out of the database for the life of the process. `segment`
        # could get away with the same shape because it runs from the CLI and the
        # process exits; this cannot.
        con.close()


def _remask(con, config: Config, limit: int | None,
            on_progress: Callable[[int, int], None] | None,
            dataset_id: str | None, camera_id: str | None, min_iou: float,
            should_stop: Callable[[], bool] | None = None) -> dict:
    # This command exists FOR databases older than the mask columns, so it has to
    # run the forward-compat migration before it can select on them. Idempotent;
    # every other stage that may meet an old DB does the same.
    db.init_db(con)
    segmenter = build_segmenter(config.detect, config.posture)
    where = [
        "f.processed = TRUE", "f.frame_path IS NOT NULL", "f.frame_path <> ''",
        # Only frames with WORK TO DO: at least one detection that still has no
        # outline. This is what makes the pass RESUMABLE — a run killed halfway
        # picks up where it stopped instead of re-inferring what it already did —
        # and it skips frames whose detections were clipped away or that never had
        # any, which on a real corpus is a large fraction. Without it the first
        # `--limit N` frames can easily be N frames with nothing on them.
        "EXISTS (SELECT 1 FROM detections d WHERE d.frame_path = f.frame_path "
        "AND d.camera_id = f.camera_id "
        "AND d.dataset_id IS NOT DISTINCT FROM f.dataset_id "
        "AND d.mask_poly IS NULL)",
    ]
    params: list = []
    if dataset_id:
        where.append("f.dataset_id = ?")
        params.append(dataset_id)
    if camera_id:
        where.append("f.camera_id = ?")
        params.append(camera_id)
    sql = (f"SELECT f.dataset_id, f.camera_id, f.frame_idx, f.frame_path FROM frames f "
           f"WHERE {' AND '.join(where)} ORDER BY f.dataset_id, f.camera_id, f.frame_idx")
    if limit:
        sql += f" LIMIT {int(limit)}"
    frames = con.execute(sql, params).fetchdf()

    total = len(frames)
    n_frames = n_matched = n_dets = n_missing = 0
    stopped = False
    for done, (_, fr) in enumerate(frames.iterrows(), start=1):
        # CHECKED BETWEEN FRAMES. The caller uses this to hand the machine to a
        # waiting upload: this pass is hours long and an upload is minutes, so a
        # strict queue would make someone wait all afternoon to see their day
        # processed. Stopping here costs nothing — the frame selection only picks
        # frames that still have a detection without an outline, so resuming
        # continues exactly where this left off rather than redoing anything.
        if should_stop is not None and should_stop():
            stopped = True
            break
        rows = con.execute(
            "SELECT detection_id, bbox_x1, bbox_y1, bbox_x2, bbox_y2 FROM detections "
            "WHERE frame_path = ? AND camera_id = ? "
            "AND dataset_id IS NOT DISTINCT FROM ?",
            [fr["frame_path"], fr["camera_id"], fr["dataset_id"]],
        ).fetchall()
        if not rows:
            if on_progress:
                on_progress(done, total)
            continue
        n_dets += len(rows)
        image = cv2.imread(fr["frame_path"])
        if image is None:
            # Routine, not exceptional, and common: a clip stages rows out and a
            # re-ingest rmtrees artifacts, both under a DB that still lists the
            # frame. Counted as skipped, never fatal.
            n_missing += 1
            if on_progress:
                on_progress(done, total)
            continue
        n_frames += 1
        instances = segmenter.segment(image)
        # Checked ONCE per frame, not per instance. `mask_to_polygon` raises on a
        # shape mismatch, which is right for `segment` (one frame dies, nothing is
        # marked processed, the run is resumable) but wrong here: this is a GPU
        # pass over a whole day, and dying halfway would leave a partially
        # backfilled corpus with no record of where it stopped. A misconfigured
        # segmenter is a whole-run problem, so it is reported as one.
        bad = [i for i in instances
               if i.mask is not None and tuple(i.mask.shape[:2]) != tuple(image.shape[:2])]
        if bad:
            raise RuntimeError(
                f"{fr['frame_path']}: mask is {bad[0].mask.shape[:2]} but the frame "
                f"is {image.shape[:2]} — the segmenter is not returning "
                "full-resolution masks (check retina_masks=True). Refusing to "
                "backfill mis-scaled outlines."
            )
        # Greedy best-IoU matching, highest first. A cow's box is well separated
        # from its neighbours at IoU >= 0.9, so the assignment is unambiguous in
        # practice and a full Hungarian solve would buy nothing; a tie that did
        # occur would be resolved the same way by either.
        pairs = []
        for i, inst in enumerate(instances):
            for det_id, x1, y1, x2, y2 in rows:
                iou = _bbox_iou((x1, y1, x2, y2), inst.bbox)
                if iou >= min_iou:
                    pairs.append((iou, i, det_id))
        pairs.sort(reverse=True)
        used_inst: set = set()
        used_det: set = set()
        for iou, i, det_id in pairs:
            if i in used_inst or det_id in used_det:
                continue
            inst = instances[i]
            outline = mask_to_polygon(inst.mask, inst.bbox, expect_shape=image.shape[:2])
            if outline is None:
                continue
            used_inst.add(i)
            used_det.add(det_id)
            con.execute(
                "UPDATE detections SET mask_poly = ?, mask_parts = ? WHERE detection_id = ?",
                [json.dumps(outline[0], separators=(",", ":")), int(outline[1]), det_id],
            )
            n_matched += 1
        if on_progress:
            on_progress(done, total)
    return {"frames": n_frames, "detections": n_dets, "matched": n_matched,
            "unmatched": n_dets - n_matched, "missing_frames": n_missing,
            "stopped": stopped}


def _bbox_iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = (float(v) for v in a)
    bx1, by1, bx2, by2 = (float(v) for v in b)
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    if inter <= 0:
        return 0.0
    ua = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    ub = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = ua + ub - inter
    return inter / denom if denom > 0 else 0.0


def segment(config: Config, limit: int | None = None,
            on_progress: Callable[[int, int], None] | None = None,
            dataset_id: str | None = None, camera_id: str | None = None) -> int:
    """Run the segmenter on unprocessed frames; write detections + overlays.

    Region assignment happens later in `localize`. `on_progress(done, total)` is
    called after each frame (if given) so a caller — e.g. the upload worker — can
    drive a progress bar through this, the batch's long pole. `dataset_id` /
    `camera_id` scope which unprocessed frames are picked up: unset (the batch
    default) processes every pending frame; set (the add-one-camera flow) processes
    only that camera's new frames, so a stray unprocessed frame elsewhere isn't
    swept in.
    """
    con = db.connect(config.paths.db_path)
    db.init_db(con)

    pending = db.unprocessed_frames(con, camera_id=camera_id, dataset_id=dataset_id)
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
            # THE OUTLINE, PERSISTED (M4 phase 0). Until this, the mask was used
            # for area/posture/overlay and then dropped, so the only surviving
            # record of the segmentation was baked pixels in a JPEG — enough to
            # LOOK at, impossible to hit-test, verdict or correct. The Label
            # tool's geometry step is the consumer.
            #
            # `area_px` above is deliberately left alone. It is an ORDERING
            # column of the ordinal window that instance_key is built on, so
            # recomputing it from a simplified polygon would re-rank tied
            # detections, renumber ordinals, re-mint keys and orphan every label
            # already collected. mask_poly/mask_parts are neither partition nor
            # ordering columns, which is exactly why they are safe to add.
            outline = mask_to_polygon(inst.mask, inst.bbox, expect_shape=image.shape[:2])
            if outline is not None:
                row["mask_poly"] = json.dumps(outline[0], separators=(",", ":"))
                row["mask_parts"] = outline[1]
            rows.append(row)

        if rows:
            # DELETE BEFORE INSERT, so a re-segmented frame replaces rather than
            # accumulates. The insert and the `mark_processed` stamp below are
            # two autocommitted statements with an overlay render between them,
            # and `unprocessed_frames` selects on `NOT processed` — so a kill in
            # that window (a redeploy, an OOM, mid-way through the stage that
            # runs for hours) leaves durable detections on a frame still marked
            # unprocessed. The next pass segments it again and appends a SECOND
            # full set of rows with fresh detection_ids, and that frame then
            # reports twice its cows in every count, every area total and every
            # whole-day peak, permanently and with nothing to detect it.
            #
            # Idempotent rather than transactional because the frame is the unit
            # of work and this makes re-doing one free. Scoped by the same triple
            # `mark_processed` uses.
            con.execute(
                "DELETE FROM detections WHERE frame_path = ? AND camera_id = ? "
                "AND dataset_id IS NOT DISTINCT FROM ?",
                [fr["frame_path"], fr["camera_id"], ds],
            )
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


def _localize_one(con, config: Config, ds: str | None) -> int:
    """Reset + reassign count and panel areas for a single partition `ds`.

    Loads ds's OWN area files via regions.dataset_area_path (ds None -> the legacy
    flat config paths). Strictly scoped to `ds` — a real id keys on dataset_id = ?,
    while ds None keys on the pre-dataset partition (dataset_id IS NULL) — so the
    fan-out can localize each dataset without disturbing any other partition.
    Returns the number of detections whose count-area assignment was recomputed."""
    if ds is not None:
        scope, reset_scope, dsp = " AND dataset_id = ?", " WHERE dataset_id = ?", [ds]
    else:
        scope, reset_scope, dsp = " AND dataset_id IS NULL", " WHERE dataset_id IS NULL", []

    # Reset assignments first so shrinking/removing an area (or a whole camera's
    # areas) clears stale region_id / shelter flags — recomputed fresh below. A
    # missing area file loads as {} -> nothing reassigned -> a clean reset, no crash.
    con.execute(
        f"UPDATE detections SET region_id = NULL, under_panel = NULL, panel_id = NULL{reset_scope}",
        dsp,
    )

    areas = regions.load_count_areas(regions.dataset_area_path(config, ds, "count"))
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
        region_ids = regions.assign_regions(
            dets[["ground_px_x", "ground_px_y"]].to_numpy(), cam_areas, camera_id,
        )
        dets["region_id"] = pd.array(region_ids, dtype=object)
        db.update_region(con, dets[["detection_id", "region_id"]])
        updated += len(dets)

    # Shelter assignment — polygon "panel areas": the SAME per-camera, image-space
    # point-in-polygon test as count areas. A cow whose ground point falls inside
    # any of a camera's panel-area polygons counts as under a panel.
    panel_areas = regions.load_count_areas(regions.dataset_area_path(config, ds, "panel"))
    for camera_id, cam_pareas in panel_areas.items():
        if not cam_pareas:
            continue
        sdets = con.execute(
            f"SELECT detection_id, ground_px_x, ground_px_y FROM detections WHERE camera_id = ?{scope}",
            [camera_id] + dsp,
        ).df()
        if sdets.empty:
            continue
        pids = regions.assign_regions(
            sdets[["ground_px_x", "ground_px_y"]].to_numpy(), cam_pareas, camera_id,
        )
        sdets["under_panel"] = pd.array([p is not None for p in pids], dtype=object)
        sdets["panel_id"] = pd.array(pids, dtype=object)
        db.update_shelter(con, sdets)

    if ds is not None:
        db.set_dataset_status(con, ds, "localized")
    return updated


def localize(config: Config, dataset_id: str | None = None) -> int:
    """Assign every detection to a count area (image-space, per camera, per dataset).

    Count/panel areas are stored PER DATASET (data/areas/<dataset_id>/), not
    globally per camera: different uploads reposition the same-named cameras, so a
    single global polygon per camera no longer holds. Each dataset is reset and
    reassigned against ITS OWN area files.

    `dataset_id` given -> localize only that package. `dataset_id` None -> fan out
    over every dataset (each against its own files), PLUS one legacy pass over the
    pre-dataset partition (dataset_id IS NULL) using the flat
    config.paths.count_areas/panel_areas, run only while such detections still
    exist. Returns the total number of detections whose count-area assignment was
    recomputed."""
    con = db.connect(config.paths.db_path)
    try:
        if dataset_id is not None:
            updated = _localize_one(con, config, dataset_id)
        else:
            updated = 0
            for ds in db.datasets(con)["dataset_id"]:
                updated += _localize_one(con, config, ds)
            # Legacy shim: assign pre-dataset detections (dataset_id IS NULL) from
            # the flat area files — only when such rows still exist, so a fully
            # migrated DB skips it and the per-dataset passes above are untouched.
            has_legacy = con.execute(
                "SELECT 1 FROM detections WHERE dataset_id IS NULL LIMIT 1"
            ).fetchone()
            if has_legacy is not None:
                updated += _localize_one(con, config, None)
    finally:
        con.close()
    print(f"[localize] updated {updated} detections")
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
    # A new upload localizes only itself against its own per-dataset area files.
    n_loc = localize(config, dataset_id=resolve_dataset(config)[0])
    print(f"[process] {n_frames} frames -> {n_det} detections -> "
          f"{n_pose} reposed -> {n_loc} localized")
    return {"frames": n_frames, "detections": n_det, "posed": n_pose, "localized": n_loc}
