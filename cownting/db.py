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
    "dataset_id",
    "camera_id", "frame_idx", "ts", "time_bin", "frame_path", "overlay_path",
    "pose_overlay_path", "processed",
]

DET_COLS = [
    "dataset_id",
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
    # Dimension: one row per data-package (a day's multi-camera shoot). The fact
    # tables carry a dataset_id FK; day/label live here to avoid update anomalies.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS datasets (
            dataset_id  VARCHAR PRIMARY KEY,
            day         DATE,
            label       VARCHAR,
            created_at  TIMESTAMP DEFAULT now(),
            ingested_at TIMESTAMP,
            status      VARCHAR DEFAULT 'ingested'
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS frames (
            dataset_id  VARCHAR,
            camera_id   VARCHAR,
            frame_idx   BIGINT,
            ts          TIMESTAMP,
            time_bin    BIGINT,
            frame_path  VARCHAR,
            overlay_path VARCHAR,
            pose_overlay_path VARCHAR,
            processed   BOOLEAN DEFAULT FALSE
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS detections (
            detection_id BIGINT DEFAULT nextval('seq_det'),
            dataset_id  VARCHAR,
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
    # Forward-compat: add shelter columns to DBs created before that feature.
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS under_panel BOOLEAN")
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS panel_id VARCHAR")
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS region_id VARCHAR")
    # Forward-compat: add the dataset dimension to DBs created before it. Existing
    # rows keep dataset_id NULL until `cownting migrate` backfills a day-0 package.
    con.execute("ALTER TABLE frames ADD COLUMN IF NOT EXISTS dataset_id VARCHAR")
    con.execute("ALTER TABLE detections ADD COLUMN IF NOT EXISTS dataset_id VARCHAR")
    # Forward-compat: pose overlay for DBs created before the pose stage.
    con.execute("ALTER TABLE frames ADD COLUMN IF NOT EXISTS pose_overlay_path VARCHAR")


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


def mark_processed(con, dataset_id, camera_id: str, frame_idx: int, overlay_path: str | None,
                   pose_overlay_path: str | None = None) -> None:
    # Frame identity is (dataset_id, camera_id, frame_idx). dataset_id may be NULL
    # for a pre-migration DB — `IS NOT DISTINCT FROM` matches NULL to NULL so the
    # old single-day flow keeps working unchanged.
    con.execute(
        "UPDATE frames SET processed = TRUE, overlay_path = ?, pose_overlay_path = ? "
        "WHERE dataset_id IS NOT DISTINCT FROM ? AND camera_id = ? AND frame_idx = ?",
        [overlay_path, pose_overlay_path, dataset_id, camera_id, frame_idx],
    )


# --------------------------------------------------------------------------- datasets (dimension)
def upsert_dataset(con, dataset_id: str, day=None, label: str | None = None,
                   status: str | None = None) -> None:
    con.execute(
        """
        INSERT INTO datasets (dataset_id, day, label, ingested_at, status)
        VALUES (?, ?, ?, now(), coalesce(?, 'ingested'))
        ON CONFLICT (dataset_id) DO UPDATE SET
            day = coalesce(excluded.day, datasets.day),
            label = coalesce(excluded.label, datasets.label),
            ingested_at = now(),
            status = coalesce(excluded.status, datasets.status)
        """,
        [dataset_id, day, label, status],
    )


def set_dataset_status(con, dataset_id: str, status: str) -> None:
    con.execute("UPDATE datasets SET status = ? WHERE dataset_id = ?", [status, dataset_id])


def purge_dataset(con, dataset_id: str, camera_id: str | None = None) -> None:
    """Delete a dataset's rows (all cameras, or one) so a re-ingest replaces rather
    than duplicates. Detections first (no FK, but keep the intent explicit)."""
    where = "dataset_id = ?"
    params = [dataset_id]
    if camera_id is not None:
        where += " AND camera_id = ?"
        params.append(camera_id)
    con.execute(f"DELETE FROM detections WHERE {where}", params)
    con.execute(f"DELETE FROM frames WHERE {where}", params)


def dataset_day(con, dataset_id: str):
    """The capture `day` (date) of a dataset, or None if the id is unknown or the
    day was never set. Used to derive the delete-confirmation date."""
    if not _table_exists(con, "datasets"):
        return None
    row = con.execute("SELECT day FROM datasets WHERE dataset_id = ?", [dataset_id]).fetchone()
    return row[0] if row else None


def archive_dataset(con, dataset_id: str, archive_path: str) -> int:
    """Move a day's rows out of the live DB into the archive DB, then delete them
    here. The day disappears from every frontend view but nothing is destroyed —
    it survives in `archive_path` and could be restored by the reverse move.

    Copies the dimension row + both fact tables (datasets, frames, detections). The
    archive is schema-initialised first, then ATTACHed to this connection so the
    copy + delete run in one transaction. Re-archiving the same id is idempotent
    (its stale archive rows are cleared before the fresh copy). Returns the number
    of detections moved."""
    # Ensure the archive file has the same schema before we ATTACH + copy into it.
    a = connect(archive_path)
    init_db(a)
    a.close()

    # ATTACH takes a literal path; archive_path is trusted config, but escape quotes
    # defensively. The alias is DETACHed even if the copy raises.
    safe = archive_path.replace("'", "''")
    n = con.execute("SELECT count(*) FROM detections WHERE dataset_id = ?", [dataset_id]).fetchone()[0]
    con.execute(f"ATTACH '{safe}' AS archive")
    try:
        # Clear any prior copy of this id so a re-archive replaces rather than dupes.
        for t in ("detections", "frames", "datasets"):
            con.execute(f"DELETE FROM archive.{t} WHERE dataset_id = ?", [dataset_id])
        # Copy live -> archive (same DDL both sides, so SELECT * column order matches).
        for t in ("datasets", "frames", "detections"):
            con.execute(f"INSERT INTO archive.{t} SELECT * FROM {t} WHERE dataset_id = ?", [dataset_id])
        # Drop from the live DB (detections first — keep the intent explicit).
        con.execute("DELETE FROM detections WHERE dataset_id = ?", [dataset_id])
        con.execute("DELETE FROM frames WHERE dataset_id = ?", [dataset_id])
        con.execute("DELETE FROM datasets WHERE dataset_id = ?", [dataset_id])
    finally:
        con.execute("DETACH archive")
    return int(n or 0)


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


def latest_dataset(con) -> str | None:
    """Most-recent dataset_id (by day, then id), or None when no package exists
    yet — the None keeps every read helper in whole-DB mode. Also None on a
    pre-migration DB whose `datasets` table hasn't been created."""
    if not _table_exists(con, "datasets"):
        return None
    row = con.execute(
        "SELECT dataset_id FROM datasets ORDER BY day DESC NULLS LAST, dataset_id DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


DATASET_COLS = ["dataset_id", "day", "label", "status", "created_at", "ingested_at",
                "n_frames", "n_detections", "n_cameras"]


def datasets(con) -> pd.DataFrame:
    """Dimension rows enriched with live fact counts, newest day first — feeds the
    day / data-package picker. Empty (not an error) on a pre-migration DB."""
    if not _table_exists(con, "datasets"):
        return pd.DataFrame(columns=DATASET_COLS)
    return con.execute(
        """
        SELECT s.dataset_id, s.day, s.label, s.status, s.created_at, s.ingested_at,
               (SELECT count(*) FROM frames f WHERE f.dataset_id = s.dataset_id)          AS n_frames,
               (SELECT count(*) FROM detections d WHERE d.dataset_id = s.dataset_id)      AS n_detections,
               (SELECT count(DISTINCT camera_id) FROM frames f WHERE f.dataset_id = s.dataset_id) AS n_cameras
        FROM datasets s
        ORDER BY s.day DESC NULLS LAST, s.dataset_id DESC
        """
    ).df()


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


def update_posture(con, df: pd.DataFrame) -> None:
    """df: detection_id, posture. Used by the standalone pose stage to overwrite
    the elongation-proxy posture with the pose-derived class."""
    if df.empty:
        return
    con.register("df_p", df[["detection_id", "posture"]])
    con.execute(
        """
        UPDATE detections AS d
        SET posture = p.posture
        FROM df_p AS p
        WHERE d.detection_id = p.detection_id
        """
    )
    con.unregister("df_p")


def set_pose_overlay(con, frame_path: str, path: str) -> None:
    """Record a baked pose-overlay image for one frame (served as kind=pose).
    Keyed by frame_path, which is unique per frame across cameras/datasets."""
    con.execute(
        "UPDATE frames SET pose_overlay_path = ? WHERE frame_path = ?",
        [path, frame_path],
    )


# --------------------------------------------------------------------------- reads
def unprocessed_frames(con, camera_id: str | None = None, dataset_id: str | None = None) -> pd.DataFrame:
    sql = ("SELECT dataset_id, camera_id, frame_idx, ts, time_bin, frame_path "
           "FROM frames WHERE NOT processed")
    params = []
    if dataset_id is not None:
        sql += " AND dataset_id = ?"
        params.append(dataset_id)
    if camera_id:
        sql += " AND camera_id = ?"
        params.append(camera_id)
    sql += " ORDER BY camera_id, frame_idx"
    return con.execute(sql, params).df()


def all_frames(con, camera_id: str | None = None, dataset_id: str | None = None) -> pd.DataFrame:
    sql = "SELECT dataset_id, camera_id, frame_idx, frame_path FROM frames"
    clauses, params = [], []
    if dataset_id is not None:
        clauses.append("dataset_id = ?")
        params.append(dataset_id)
    if camera_id:
        clauses.append("camera_id = ?")
        params.append(camera_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY camera_id, frame_idx"
    return con.execute(sql, params).df()


def cameras(con, dataset_id: str | None = None) -> list[str]:
    sql = "SELECT DISTINCT camera_id FROM frames"
    params = []
    if dataset_id is not None:
        sql += " WHERE dataset_id = ?"
        params.append(dataset_id)
    sql += " ORDER BY 1"
    return [r[0] for r in con.execute(sql, params).fetchall()]


def kpi_summary(con, dataset_id: str | None = None) -> dict:
    # `f` filters processed-frame subqueries, `d` filters detection subqueries;
    # each is empty (whole-DB) when dataset_id is None.
    f = " AND dataset_id = ?" if dataset_id is not None else ""
    d = " WHERE dataset_id = ?" if dataset_id is not None else ""
    dp = " AND dataset_id = ?" if dataset_id is not None else ""
    p = [dataset_id] if dataset_id is not None else []
    row = con.execute(
        f"""
        SELECT
            (SELECT count(*) FROM frames WHERE processed{f})                 AS frames,
            -- valid = any processed frame
            (SELECT count(*) FROM frames WHERE processed{f})                 AS valid_frames,
            (SELECT count(*) FROM detections{d})                             AS detections,
            (SELECT count(*) FROM detections WHERE posture = 'standing'{dp})  AS standing,
            (SELECT count(*) FROM detections WHERE posture = 'lying'{dp})     AS lying,
            (SELECT count(*) FROM detections WHERE under_panel{dp})            AS sheltering
        """,
        p + p + p + p + p + p,
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


def _instant_expr(bin_seconds: float, ts_col: str = "f.ts") -> str:
    """SQL for the shared cross-camera *instant* key: a frame's timestamp snapped
    to a `bin_seconds`-wide grid (the capture interval). Cameras are linked by
    real time, not by frame_idx, so clips that started seconds apart still line up
    and a camera that powers on late simply has no rows for the earlier instants.
    `bin_seconds` is numeric (from config), so interpolating it is injection-safe."""
    return f"CAST(round(epoch({ts_col}) / {float(bin_seconds)}) AS BIGINT)"


def day_series(con, dataset_id: str | None = None, bin_seconds: float = 60.0) -> pd.DataFrame:
    """Counts summed across cameras per *instant* (timestamp bucket), for the
    time-of-day bar strips: total in view + standing/lying + sheltering. Shares
    the instant axis with the scrubber (LEFT JOIN so zero-cow instants keep their
    slot). Returns `instant` (the bucket key) and `ts` (its wall-clock label).

    Cross-camera linking is by timestamp (see _instant_expr), NOT frame_idx, so
    cameras with different start times still align. Scoped to one dataset when
    given, whole-DB when None; the frames<->detections join is on
    camera_id+frame_path (frame_path is globally unique).

    `sheltering` (under_panel TRUE) and `open` (everything else) partition the
    herd: a cow is under a panel or it is in the open. Cows on cameras with no
    panel areas (under_panel NULL) count as open, same as cows outside a panel."""
    where = "WHERE f.dataset_id = ?" if dataset_id is not None else ""
    params = [dataset_id] if dataset_id is not None else []
    inst = _instant_expr(bin_seconds)
    return con.execute(
        f"""
        SELECT {inst} AS instant,
               min(f.ts)                                                  AS ts,
               count(d.detection_id)                                     AS total,
               count(d.detection_id) FILTER (WHERE d.posture='standing')  AS standing,
               count(d.detection_id) FILTER (WHERE d.posture='lying')     AS lying,
               count(d.detection_id) FILTER (WHERE d.under_panel)          AS sheltering,
               count(d.detection_id) FILTER (WHERE d.under_panel IS NOT TRUE) AS "open"
        FROM frames f
        LEFT JOIN detections d
          ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path
        {where}
        GROUP BY instant ORDER BY instant
        """,
        params,
    ).df()


def timeline_series(con, dataset_id: str | None = None, bin_seconds: float = 60.0) -> pd.DataFrame:
    """Instant axis for the day scrubber: one row per timestamp bucket with its
    wall-clock `ts` and total cows across cameras. Same buckets as day_series."""
    where = "WHERE f.dataset_id = ?" if dataset_id is not None else ""
    params = [dataset_id] if dataset_id is not None else []
    inst = _instant_expr(bin_seconds)
    return con.execute(
        f"""
        SELECT {inst} AS instant, min(f.ts) AS ts, count(d.detection_id) AS n
        FROM frames f
        LEFT JOIN detections d
          ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path
        {where}
        GROUP BY instant ORDER BY instant
        """,
        params,
    ).df()


def frames_at_instant(con, instant: int, bin_seconds: float = 60.0,
                      dataset_id: str | None = None) -> pd.DataFrame:
    """The representative frame_idx for each camera at one instant (timestamp
    bucket) — how the per-camera seg views resolve which frame to show. A camera
    with no frame in that bucket is absent from the result (it hasn't come online
    yet / has gone offline). Picks the earliest frame_idx if a camera has several."""
    where = " AND dataset_id = ?" if dataset_id is not None else ""
    params = [instant] + ([dataset_id] if dataset_id is not None else [])
    inst = _instant_expr(bin_seconds, "ts")
    return con.execute(
        f"""
        SELECT camera_id, min(frame_idx) AS frame_idx
        FROM frames
        WHERE {inst} = ?{where}
        GROUP BY camera_id ORDER BY camera_id
        """,
        params,
    ).df()


def area_summary(con, dataset_id: str | None = None) -> pd.DataFrame:
    """Whole-day totals per count area, split by posture. Feeds the static
    per-area KPI list (cows spotted + standing/lying) on the right rail."""
    where = " AND dataset_id = ?" if dataset_id is not None else ""
    params = [dataset_id] if dataset_id is not None else []
    return con.execute(
        f"""
        SELECT region_id,
               count(*)                                       AS total,
               count(*) FILTER (WHERE posture = 'standing')   AS standing,
               count(*) FILTER (WHERE posture = 'lying')      AS lying,
               count(*) FILTER (WHERE under_panel)            AS sheltering
        FROM detections
        WHERE region_id IS NOT NULL{where}
        GROUP BY region_id
        ORDER BY total DESC
        """,
        params,
    ).df()


def area_counts_whole_day(con, dataset_id: str | None = None, bin_seconds: float = 60.0) -> pd.DataFrame:
    """Whole-day occupancy per count area for the map's "whole day" toggle.

    `peak` is the max cows present in the area *at the same instant* (max over
    frames of the per-frame count) — the natural whole-day analog of the single-
    frame badge, and unlike a cumulative sum it stays a small, readable integer.
    standing/lying/unknown are whole-day cumulative counts, used only for the
    posture ring's proportions; `sheltering` is the peak simultaneous under-panel
    count. Empty frame => no rows (not a blank latest frame).
    """
    dwhere = " AND d.dataset_id = ?" if dataset_id is not None else ""
    where = " AND dataset_id = ?" if dataset_id is not None else ""
    params = ([dataset_id, dataset_id] if dataset_id is not None else [])
    return con.execute(
        f"""
        WITH per_frame AS (
            SELECT d.region_id AS region_id, {_instant_expr(bin_seconds)} AS instant,
                   count(*)                             AS cnt,
                   count(*) FILTER (WHERE d.under_panel) AS shel
            FROM detections d
            JOIN frames f ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path
            WHERE d.region_id IS NOT NULL{dwhere}
            GROUP BY d.region_id, instant
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
            FROM detections WHERE region_id IS NOT NULL{where} GROUP BY region_id
        )
        SELECT p.region_id, p.peak, p.sheltering, o.standing, o.lying, o.unknown
        FROM peak p JOIN posture o USING (region_id)
        ORDER BY p.peak DESC
        """,
        params,
    ).df()


def counts_over_time(con, camera_id: str, trunc: str = "hour", dataset_id: str | None = None) -> pd.DataFrame:
    # Frame-based (LEFT JOIN) so zero-cow frames count; all processed frames.
    where = " AND f.dataset_id = ?" if dataset_id is not None else ""
    params = [camera_id] + ([dataset_id] if dataset_id is not None else [])
    return con.execute(
        f"""
        SELECT date_trunc('{trunc}', f.ts)       AS t,
               count(DISTINCT f.frame_idx)       AS frames,
               count(d.detection_id)             AS detections,
               count(d.detection_id) * 1.0 / nullif(count(DISTINCT f.frame_idx), 0) AS cows_per_frame
        FROM frames f
        LEFT JOIN detections d ON d.frame_path = f.frame_path
        WHERE f.camera_id = ? AND f.processed{where}
        GROUP BY 1 ORDER BY 1
        """,
        params,
    ).df()


def shelter_over_time(con, camera_id: str | None, trunc: str = "hour", dataset_id: str | None = None) -> pd.DataFrame:
    # Detection-based: sheltering = count of under_panel per bucket. camera_id None -> all cameras.
    sql = f"""
        SELECT date_trunc('{trunc}', ts)               AS t,
               count(*) FILTER (WHERE under_panel)      AS sheltering,
               count(*)                                 AS detections
        FROM detections
    """
    clauses, params = [], []
    if camera_id is not None:
        clauses.append("camera_id = ?")
        params.append(camera_id)
    if dataset_id is not None:
        clauses.append("dataset_id = ?")
        params.append(dataset_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY 1 ORDER BY 1"
    return con.execute(sql, params).df()


def area_counts_over_time(con, camera: str | None = None, trunc: str = "hour", dataset_id: str | None = None) -> pd.DataFrame:
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
    if dataset_id is not None:
        sql += " AND dataset_id = ?"
        params.append(dataset_id)
    sql += " GROUP BY 1, 2 ORDER BY 1, 2"
    return con.execute(sql, params).df()


def posture_over_time(con, camera_id: str, trunc: str = "hour", dataset_id: str | None = None) -> pd.DataFrame:
    where = " AND dataset_id = ?" if dataset_id is not None else ""
    params = [camera_id] + ([dataset_id] if dataset_id is not None else [])
    return con.execute(
        f"""
        SELECT date_trunc('{trunc}', ts) AS t,
               coalesce(posture, 'unknown') AS posture,
               count(*) AS n
        FROM detections WHERE camera_id = ?{where}
        GROUP BY 1, 2 ORDER BY 1
        """,
        params,
    ).df()


def detections_df(con, camera_id: str | None = None, dataset_id: str | None = None) -> pd.DataFrame:
    sql = "SELECT * FROM detections"
    clauses, params = [], []
    if camera_id:
        clauses.append("camera_id = ?")
        params.append(camera_id)
    if dataset_id is not None:
        clauses.append("dataset_id = ?")
        params.append(dataset_id)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    return con.execute(sql, params).df()


def frames_df(con, camera_id: str, dataset_id: str | None = None) -> pd.DataFrame:
    where = " AND dataset_id = ?" if dataset_id is not None else ""
    params = [camera_id] + ([dataset_id] if dataset_id is not None else [])
    return con.execute(
        "SELECT frame_idx, ts, frame_path, overlay_path FROM frames "
        f"WHERE camera_id = ? AND processed{where} ORDER BY frame_idx",
        params,
    ).df()


def export_df(con, dataset_id: str | None = None) -> pd.DataFrame:
    """Flat CSV export: one row per detection, joined to its frame so the CSV
    carries `frame_idx` + `dataset_id` alongside every detection column. Scoped to
    one dataset when given, whole-DB when None. The frame join is against a
    DISTINCT subquery so duplicate frame rows (pre-idempotency ingest) can't fan
    out the detection rows."""
    where = " WHERE d.dataset_id = ?" if dataset_id is not None else ""
    params = [dataset_id] if dataset_id is not None else []
    return con.execute(
        f"""
        SELECT d.detection_id, d.dataset_id, d.camera_id, f.frame_idx, d.ts, d.time_bin,
               d.frame_path, d.score,
               d.bbox_x1, d.bbox_y1, d.bbox_x2, d.bbox_y2,
               d.area_px, d.ground_px_x, d.ground_px_y,
               d.posture, d.region_id, d.under_panel, d.panel_id, d.in_shade,
               d.track_id, d.global_id, d.motion, d.near_infra, d.cluster_size
        FROM detections d
        LEFT JOIN (SELECT DISTINCT dataset_id, camera_id, frame_path, frame_idx FROM frames) f
          ON d.dataset_id IS NOT DISTINCT FROM f.dataset_id
         AND d.camera_id = f.camera_id AND d.frame_path = f.frame_path
        {where}
        ORDER BY d.camera_id, f.frame_idx, d.detection_id
        """,
        params,
    ).df()


def _domain(df: pd.DataFrame, col: str, spec) -> list:
    """Stable ordered category list for a crosstab axis: the registry's fixed
    order when defined; temporal features ascending; otherwise by total desc."""
    if spec is None:
        return []
    if spec.fixed_domain:
        present = set(df[col].dropna().tolist())
        return [v for v in spec.fixed_domain if v in present]
    vals = df[col].dropna()
    if vals.empty:
        return []
    if spec.kind in ("temporal_hour", "temporal_frame"):
        return sorted({int(v) for v in vals.tolist()})
    order = df.groupby(col)["n"].sum().sort_values(ascending=False)
    return [v for v in order.index.tolist()]


def crosstab(con, primary: str, breakdown: str | None = None, *,
             dataset_id: str | None = None, camera_id: str | None = None,
             frame: int | None = None):
    """Long-form GROUP BY of two whitelisted feature expressions.

    Returns (df[primary, breakdown, n], primary_domain, breakdown_domain). Feature
    keys resolve through features.FEATURES — their SQL comes ONLY from that trusted
    registry, never from the argument strings (injection guard). Filters are
    parameterized. frames is joined only when a temporal_frame feature or a `frame`
    filter needs frame_idx."""
    from . import features as feat

    p = feat.resolve(primary)
    b = feat.resolve(breakdown) if breakdown else None
    needs_frames = p.needs_frames or (b is not None and b.needs_frames) or frame is not None

    sel = [f"{p.sql} AS prim", (f"{b.sql} AS brk" if b else "NULL AS brk"),
           "count(d.detection_id) AS n"]
    sql = f"SELECT {', '.join(sel)} FROM detections d"
    if needs_frames:
        sql += " LEFT JOIN frames f ON d.camera_id = f.camera_id AND d.frame_path = f.frame_path"

    clauses, params = ["1=1"], []
    for spec in (p, b):
        if spec is not None and spec.drop_null:
            clauses.append(f"{spec.sql} IS NOT NULL")
    if dataset_id is not None:
        clauses.append("d.dataset_id = ?"); params.append(dataset_id)
    if camera_id is not None:
        clauses.append("d.camera_id = ?"); params.append(camera_id)
    if frame is not None:
        clauses.append("f.frame_idx = ?"); params.append(frame)
    sql += " WHERE " + " AND ".join(clauses)
    sql += " GROUP BY 1" + (", 2" if b else "")

    df = con.execute(sql, params).df().rename(columns={"prim": "primary", "brk": "breakdown"})
    return df, _domain(df, "primary", p), _domain(df, "breakdown", b)


def available_features(con) -> dict:
    """{feature_key: has_any_non_null_data} — gates reserved features (in_shade,
    future head_pose) in the UI until their column is populated. `avail_col` comes
    from the trusted registry, so interpolating it is safe."""
    from . import features as feat

    out = {}
    for k, s in feat.FEATURES.items():
        if not s.avail_col:
            out[k] = True
        else:
            n = con.execute(
                f"SELECT count(*) FROM detections WHERE {s.avail_col} IS NOT NULL"
            ).fetchone()[0]
            out[k] = n > 0
    return out


def reference_frame(con, camera_id: str, dataset_id: str | None = None) -> str | None:
    """A representative frame for a camera (middle of the processed range)."""
    where = " AND dataset_id = ?" if dataset_id is not None else ""
    params = [camera_id] + ([dataset_id] if dataset_id is not None else [])
    df = con.execute(
        f"SELECT frame_path FROM frames WHERE camera_id = ? AND processed{where} ORDER BY frame_idx",
        params,
    ).df()
    if df.empty:
        return None
    return df.iloc[len(df) // 2]["frame_path"]
