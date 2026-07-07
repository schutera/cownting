"""DuckDB store: schema, inserts, and query helpers for the dashboard.

The `detections` table carries every column the full project will ever need.
Stage 2-4 columns (world_x/y, track_id, global_id, motion, in_shade, ...) are
written NULL now and filled later, so downstream stages are additive.
"""
from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd

FRAME_COLS = [
    "camera_id", "frame_idx", "ts", "time_bin", "frame_path", "overlay_path", "processed",
]

DET_COLS = [
    "camera_id", "ts", "time_bin", "frame_path", "score",
    "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2", "area_px",
    "ground_px_x", "ground_px_y", "posture",
    # --- reserved for later stages (nullable) ---
    "track_id", "global_id",
    "motion", "in_shade", "near_infra", "cluster_size",
    "under_panel", "panel_id", "region_id",
]


def connect(db_path: str, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(db_path, read_only=read_only)


def init_db(con: duckdb.DuckDBPyConnection) -> None:
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_det START 1;")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS frames (
            camera_id   VARCHAR,
            frame_idx   BIGINT,
            ts          TIMESTAMP,
            time_bin    BIGINT,
            frame_path  VARCHAR,
            overlay_path VARCHAR,
            processed   BOOLEAN DEFAULT FALSE
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            detection_id BIGINT DEFAULT nextval('seq_det'),
            camera_id   VARCHAR,
            ts          TIMESTAMP,
            time_bin    BIGINT,
            frame_path  VARCHAR,
            score       DOUBLE,
            bbox_x1     DOUBLE, bbox_y1 DOUBLE, bbox_x2 DOUBLE, bbox_y2 DOUBLE,
            area_px     DOUBLE,
            ground_px_x DOUBLE, ground_px_y DOUBLE,
            posture     VARCHAR,
            track_id    BIGINT, global_id BIGINT,
            motion      VARCHAR,
            in_shade    BOOLEAN, near_infra BOOLEAN, cluster_size INTEGER,
            under_panel BOOLEAN, panel_id VARCHAR,
            region_id   VARCHAR
        );
        """
    )
    # Forward-compat: add shelter columns to DBs created before this feature.
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS under_panel BOOLEAN")
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS panel_id VARCHAR")
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS region_id VARCHAR")


# --------------------------------------------------------------------------- writes
def insert_frames(con, df: pd.DataFrame) -> None:
    df = df.copy()
    for c in FRAME_COLS:
        if c not in df:
            df[c] = None
    con.register("df_frames", df[FRAME_COLS])
    con.execute(f"INSERT INTO frames ({', '.join(FRAME_COLS)}) SELECT {', '.join(FRAME_COLS)} FROM df_frames")
    con.unregister("df_frames")


def insert_detections(con, df: pd.DataFrame) -> None:
    if df.empty:
        return
    df = df.copy()
    for c in DET_COLS:
        if c not in df:
            df[c] = None
    con.register("df_det", df[DET_COLS])
    con.execute(f"INSERT INTO detections ({', '.join(DET_COLS)}) SELECT {', '.join(DET_COLS)} FROM df_det")
    con.unregister("df_det")


def mark_processed(con, camera_id: str, frame_idx: int, overlay_path: str | None) -> None:
    con.execute(
        "UPDATE frames SET processed = TRUE, overlay_path = ? WHERE camera_id = ? AND frame_idx = ?",
        [overlay_path, camera_id, frame_idx],
    )


def update_region(con, df: pd.DataFrame) -> None:
    """df: detection_id, region_id."""
    if df.empty:
        return
    con.register("df_r", df[["detection_id", "region_id"]])
    con.execute(
        """
        UPDATE detections AS d
        SET region_id = r.region_id
        FROM df_r AS r
        WHERE d.detection_id = r.detection_id
        """
    )
    con.unregister("df_r")


def update_shelter(con, df: pd.DataFrame) -> None:
    """df: detection_id, under_panel, panel_id (panel areas are polygons, so there
    is no band boundary flag — near_infra stays NULL)."""
    if df.empty:
        return
    con.register("df_s", df[["detection_id", "under_panel", "panel_id"]])
    con.execute(
        """
        UPDATE detections AS d
        SET under_panel = s.under_panel, panel_id = s.panel_id
        FROM df_s AS s
        WHERE d.detection_id = s.detection_id
        """
    )
    con.unregister("df_s")


# --------------------------------------------------------------------------- reads
def unprocessed_frames(con, camera_id: str | None = None) -> pd.DataFrame:
    sql = "SELECT camera_id, frame_idx, ts, time_bin, frame_path FROM frames WHERE NOT processed"
    params = []
    if camera_id:
        sql += " AND camera_id = ?"
        params.append(camera_id)
    sql += " ORDER BY camera_id, frame_idx"
    return con.execute(sql, params).df()


def all_frames(con, camera_id: str | None = None) -> pd.DataFrame:
    sql = "SELECT camera_id, frame_idx, frame_path FROM frames"
    params = []
    if camera_id:
        sql += " WHERE camera_id = ?"
        params.append(camera_id)
    sql += " ORDER BY camera_id, frame_idx"
    return con.execute(sql, params).df()


def cameras(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT camera_id FROM frames ORDER BY 1").fetchall()]


def kpi_summary(con) -> dict:
    row = con.execute(
        """
        SELECT
            (SELECT count(*) FROM frames WHERE processed)                 AS frames,
            -- valid = any processed frame
            (SELECT count(*) FROM frames WHERE processed)                 AS valid_frames,
            (SELECT count(*) FROM detections)                             AS detections,
            (SELECT count(*) FROM detections WHERE posture = 'standing')  AS standing,
            (SELECT count(*) FROM detections WHERE posture = 'lying')     AS lying,
            (SELECT count(*) FROM detections WHERE under_panel)            AS sheltering
        """
    ).fetchone()
    frames, valid_frames, dets, standing, lying, sheltering = row
    return {
        "frames": int(frames or 0),
        "valid_frames": int(valid_frames or 0),
        "detections": int(dets or 0),
        "standing": int(standing or 0),
        "lying": int(lying or 0),
        "sheltering": int(sheltering or 0),
        "cows_per_frame": round((dets / valid_frames), 2) if valid_frames else 0.0,
        "pct_lying": round(100 * lying / dets, 1) if dets else 0.0,
        "pct_sheltering": round(100 * sheltering / dets, 1) if dets else 0.0,
    }


def day_series(con) -> pd.DataFrame:
    """Per-frame counts summed across cameras, for the time-of-day bar strips:
    total in view + standing/lying + sheltering. Shares the frame axis with the
    scrubber's activity strip (LEFT JOIN so zero-cow frames keep their slot).

    `sheltering` (under_panel TRUE) and `open` (under_panel FALSE) are the two
    halves of the under-panel share; they exclude cows on cameras with no panel
    areas (under_panel NULL), exactly as standing/lying exclude unknown posture."""
    return con.execute(
        """
        SELECT f.frame_idx AS frame_idx,
               count(d.detection_id)                                     AS total,
               count(d.detection_id) FILTER (WHERE d.posture='standing')  AS standing,
               count(d.detection_id) FILTER (WHERE d.posture='lying')     AS lying,
               count(d.detection_id) FILTER (WHERE d.under_panel)         AS sheltering,
               count(d.detection_id) FILTER (WHERE d.under_panel = false) AS "open"
        FROM frames f
        LEFT JOIN detections d
          ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path
        GROUP BY f.frame_idx ORDER BY f.frame_idx
        """
    ).df()


def area_summary(con) -> pd.DataFrame:
    """Whole-day totals per count area, split by posture. Feeds the static
    per-area KPI list (cows spotted + standing/lying) on the right rail."""
    return con.execute(
        """
        SELECT region_id,
               count(*)                                       AS total,
               count(*) FILTER (WHERE posture = 'standing')   AS standing,
               count(*) FILTER (WHERE posture = 'lying')      AS lying,
               count(*) FILTER (WHERE under_panel)            AS sheltering
        FROM detections
        WHERE region_id IS NOT NULL
        GROUP BY region_id
        ORDER BY total DESC
        """
    ).df()


def area_counts_whole_day(con) -> pd.DataFrame:
    """Whole-day occupancy per count area for the map's "whole day" toggle.

    `peak` is the max cows present in the area *at the same instant* (max over
    frames of the per-frame count) — the natural whole-day analog of the single-
    frame badge, and unlike a cumulative sum it stays a small, readable integer.
    standing/lying/unknown are whole-day cumulative counts, used only for the
    posture ring's proportions; `sheltering` is the peak simultaneous under-panel
    count. Empty frame => no rows (not a blank latest frame).
    """
    return con.execute(
        """
        WITH per_frame AS (
            SELECT d.region_id AS region_id, f.frame_idx AS frame_idx,
                   count(*)                             AS cnt,
                   count(*) FILTER (WHERE d.under_panel) AS shel
            FROM detections d
            JOIN frames f ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path
            WHERE d.region_id IS NOT NULL
            GROUP BY d.region_id, f.frame_idx
        ),
        peak AS (
            SELECT region_id, max(cnt) AS peak, max(shel) AS sheltering
            FROM per_frame GROUP BY region_id
        ),
        posture AS (
            SELECT region_id,
                   count(*) FILTER (WHERE coalesce(posture, 'unknown') = 'standing') AS standing,
                   count(*) FILTER (WHERE coalesce(posture, 'unknown') = 'lying')     AS lying,
                   count(*) FILTER (WHERE coalesce(posture, 'unknown')
                                          NOT IN ('standing', 'lying'))               AS unknown
            FROM detections WHERE region_id IS NOT NULL GROUP BY region_id
        )
        SELECT p.region_id, p.peak, p.sheltering, o.standing, o.lying, o.unknown
        FROM peak p JOIN posture o USING (region_id)
        ORDER BY p.peak DESC
        """
    ).df()


def counts_over_time(con, camera_id: str, trunc: str = "hour") -> pd.DataFrame:
    # Frame-based (LEFT JOIN) so zero-cow frames count; all processed frames.
    return con.execute(
        f"""
        SELECT date_trunc('{trunc}', f.ts)       AS t,
               count(DISTINCT f.frame_idx)       AS frames,
               count(d.detection_id)             AS detections,
               count(d.detection_id) * 1.0 / nullif(count(DISTINCT f.frame_idx), 0) AS cows_per_frame
        FROM frames f
        LEFT JOIN detections d ON d.frame_path = f.frame_path
        WHERE f.camera_id = ? AND f.processed
        GROUP BY 1 ORDER BY 1
        """,
        [camera_id],
    ).df()


def shelter_over_time(con, camera_id: str | None, trunc: str = "hour") -> pd.DataFrame:
    # Detection-based: sheltering = count of under_panel per bucket. camera_id None -> all cameras.
    sql = f"""
        SELECT date_trunc('{trunc}', ts)               AS t,
               count(*) FILTER (WHERE under_panel)      AS sheltering,
               count(*)                                 AS detections
        FROM detections
    """
    params = []
    if camera_id is not None:
        sql += " WHERE camera_id = ?"
        params.append(camera_id)
    sql += " GROUP BY 1 ORDER BY 1"
    return con.execute(sql, params).df()


def area_counts_over_time(con, camera: str | None = None, trunc: str = "hour") -> pd.DataFrame:
    # Detection-based: cows per count-area per bucket. camera None -> all cameras.
    sql = """
        SELECT date_trunc(?, ts) AS t, region_id, count(*) AS cows
        FROM detections
        WHERE region_id IS NOT NULL
    """
    params = [trunc]
    if camera is not None:
        sql += " AND camera_id = ?"
        params.append(camera)
    sql += " GROUP BY 1, 2 ORDER BY 1, 2"
    return con.execute(sql, params).df()


def posture_over_time(con, camera_id: str, trunc: str = "hour") -> pd.DataFrame:
    return con.execute(
        f"""
        SELECT date_trunc('{trunc}', ts) AS t,
               coalesce(posture, 'unknown') AS posture,
               count(*) AS n
        FROM detections WHERE camera_id = ?
        GROUP BY 1, 2 ORDER BY 1
        """,
        [camera_id],
    ).df()


def detections_df(con, camera_id: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM detections"
    params = []
    if camera_id:
        sql += " WHERE camera_id = ?"
        params.append(camera_id)
    return con.execute(sql, params).df()


def frames_df(con, camera_id: str) -> pd.DataFrame:
    return con.execute(
        "SELECT frame_idx, ts, frame_path, overlay_path FROM frames "
        "WHERE camera_id = ? AND processed ORDER BY frame_idx",
        [camera_id],
    ).df()


def reference_frame(con, camera_id: str) -> str | None:
    """A frame for calibration clicking (middle of the processed range)."""
    df = con.execute(
        "SELECT frame_path FROM frames WHERE camera_id = ? AND processed ORDER BY frame_idx",
        [camera_id],
    ).df()
    if df.empty:
        return None
    return df.iloc[len(df) // 2]["frame_path"]
