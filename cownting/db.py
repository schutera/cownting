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
    "camera_id", "frame_idx", "ts", "time_bin", "frame_path", "overlay_path", "processed", "frame_quality",
]

DET_COLS = [
    "camera_id", "ts", "time_bin", "frame_path", "score",
    "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2", "area_px",
    "ground_px_x", "ground_px_y", "posture",
    # --- reserved for later stages (nullable) ---
    "world_x", "world_y", "track_id", "global_id",
    "motion", "in_shade", "near_infra", "cluster_size",
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
            processed   BOOLEAN DEFAULT FALSE,
            frame_quality VARCHAR
        );
        """
    )
    # migrate older DBs created before the quality gate existed
    con.execute("ALTER TABLE frames ADD COLUMN IF NOT EXISTS frame_quality VARCHAR;")
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
            world_x     DOUBLE, world_y DOUBLE,
            track_id    BIGINT, global_id BIGINT,
            motion      VARCHAR,
            in_shade    BOOLEAN, near_infra BOOLEAN, cluster_size INTEGER
        );
        """
    )


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


def update_frame_quality(con, df: pd.DataFrame) -> None:
    """df: camera_id, frame_idx, frame_quality."""
    if df.empty:
        return
    con.register("df_q", df[["camera_id", "frame_idx", "frame_quality"]])
    con.execute(
        """
        UPDATE frames AS f SET frame_quality = q.frame_quality
        FROM df_q AS q WHERE f.camera_id = q.camera_id AND f.frame_idx = q.frame_idx
        """
    )
    con.unregister("df_q")


def mark_processed(con, camera_id: str, frame_idx: int, overlay_path: str | None) -> None:
    con.execute(
        "UPDATE frames SET processed = TRUE, overlay_path = ? WHERE camera_id = ? AND frame_idx = ?",
        [overlay_path, camera_id, frame_idx],
    )


def update_world(con, df: pd.DataFrame) -> None:
    """df: detection_id, world_x, world_y."""
    if df.empty:
        return
    con.register("df_w", df[["detection_id", "world_x", "world_y"]])
    con.execute(
        """
        UPDATE detections AS d
        SET world_x = w.world_x, world_y = w.world_y
        FROM df_w AS w
        WHERE d.detection_id = w.detection_id
        """
    )
    con.unregister("df_w")


# --------------------------------------------------------------------------- reads
def unprocessed_frames(con, camera_id: str | None = None, skip_blind: bool = False) -> pd.DataFrame:
    sql = "SELECT camera_id, frame_idx, ts, time_bin, frame_path FROM frames WHERE NOT processed"
    params = []
    if skip_blind:  # only skip truly-unusable optics; hazy frames are still worth detecting on
        sql += " AND (frame_quality IS NULL OR frame_quality NOT IN ('blind', 'dark'))"
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


def quality_breakdown(con, camera_id: str | None = None) -> pd.DataFrame:
    sql = "SELECT coalesce(frame_quality, 'unassessed') AS quality, count(*) AS n FROM frames"
    params = []
    if camera_id:
        sql += " WHERE camera_id = ?"
        params.append(camera_id)
    sql += " GROUP BY 1 ORDER BY 2 DESC"
    return con.execute(sql, params).df()


def cameras(con) -> list[str]:
    return [r[0] for r in con.execute("SELECT DISTINCT camera_id FROM frames ORDER BY 1").fetchall()]


def kpi_summary(con) -> dict:
    row = con.execute(
        """
        SELECT
            (SELECT count(*) FROM frames WHERE processed)                        AS frames,
            (SELECT count(*) FROM frames WHERE frame_quality IN ('blind','dark')) AS blind,
            -- valid = observable optics OR we actually detected a cow there
            (SELECT count(*) FROM frames f WHERE f.processed AND (
                 coalesce(f.frame_quality, 'ok') NOT IN ('blind', 'dark')
                 OR EXISTS (SELECT 1 FROM detections d WHERE d.frame_path = f.frame_path)
             ))                                                                  AS valid_frames,
            (SELECT count(*) FROM detections)                                   AS detections,
            (SELECT count(*) FROM detections WHERE posture = 'lying')           AS lying,
            (SELECT count(*) FROM detections WHERE world_x IS NOT NULL)         AS localized
        """
    ).fetchone()
    frames, blind, valid_frames, dets, lying, localized = row
    return {
        "frames": int(frames or 0),
        "blind_frames": int(blind or 0),
        "valid_frames": int(valid_frames or 0),
        "detections": int(dets or 0),
        "cows_per_frame": round((dets / valid_frames), 2) if valid_frames else 0.0,
        "pct_lying": round(100 * lying / dets, 1) if dets else 0.0,
        "pct_localized": round(100 * localized / dets, 1) if dets else 0.0,
    }


def counts_over_time(con, camera_id: str, trunc: str = "hour") -> pd.DataFrame:
    # Frame-based (LEFT JOIN) so zero-cow frames count; valid frames only.
    return con.execute(
        f"""
        SELECT date_trunc('{trunc}', f.ts)       AS t,
               count(DISTINCT f.frame_idx)       AS frames,
               count(d.detection_id)             AS detections,
               count(d.detection_id) * 1.0 / nullif(count(DISTINCT f.frame_idx), 0) AS cows_per_frame
        FROM frames f
        LEFT JOIN detections d ON d.frame_path = f.frame_path
        WHERE f.camera_id = ? AND f.processed AND (
            coalesce(f.frame_quality, 'ok') NOT IN ('blind', 'dark') OR d.detection_id IS NOT NULL)
        GROUP BY 1 ORDER BY 1
        """,
        [camera_id],
    ).df()


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
    """A clear frame for calibration clicking (middle of the usable, non-blind range)."""
    df = con.execute(
        "SELECT frame_path FROM frames WHERE camera_id = ? AND processed "
        "AND (frame_quality = 'ok' OR frame_quality IS NULL) ORDER BY frame_idx",
        [camera_id],
    ).df()
    if df.empty:
        return None
    return df.iloc[len(df) // 2]["frame_path"]
