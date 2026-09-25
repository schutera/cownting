"""A long-deployed database and a fresh one must stay interchangeable.

`init_db` is both the schema and the migration: every boot runs it, and every
column added since the first deploy arrives through `ALTER TABLE ... ADD COLUMN
IF NOT EXISTS`. That works — except that ALTER always appends at the TAIL, while
`CREATE TABLE` puts the column wherever the DDL says. So a database created
before a column existed ends up with a DIFFERENT COLUMN ORDER from one created
today, holding exactly the same data.

That difference was load-bearing in one place, and it destroyed data there:

    archive_dataset() created the archive with a fresh init_db (CREATE order),
    copied the live rows in with `INSERT INTO archive.t SELECT * FROM t`
    (POSITIONAL), and then DELETEd them from the live DB. On a database
    predating the pose stage, live order is [... overlay_path, processed,
    pose_overlay_path ...] while the fresh archive expects [... overlay_path,
    pose_overlay_path, processed ...] — so `processed = TRUE` was cast to the
    string 'true' and filed under `pose_overlay_path`, and `processed` was left
    NULL. Nothing raised. The archive is the only surviving copy of the day.

`archive_dataset` now names its columns, so order cannot matter. These tests
replay the schema of each real era in this repo's history, migrate it, and
assert the result is indistinguishable from a fresh database in every way that
any caller depends on.

No pytest. Run either way:
    py -3.11 -m tests.test_schema_migration
    py -3.11 tests/test_schema_migration.py
"""
from __future__ import annotations

import contextlib
import os
import shutil
import sys
import tempfile

import duckdb

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    line = f"[{'ok ' if cond else 'FAIL'}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


@contextlib.contextmanager
def tmpdir():
    """`TemporaryDirectory` raises on Windows when a DuckDB handle is still
    held, which masks whatever the test was actually failing on."""
    d = tempfile.mkdtemp()
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def cols(con, table: str) -> list[str]:
    return [r[0] for r in con.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_name = ? ORDER BY ordinal_position", [table]).fetchall()]


# --------------------------------------------------------------------- the eras
#
# The schema as it stood at each point this repo added a column. Each entry is
# what `CREATE TABLE` wrote back then; `init_db` is expected to carry it forward
# to today. Kept verbatim rather than generated, because a generated "old"
# schema would be derived from the current one and could only ever agree with it.

_DATASETS = """
CREATE TABLE datasets (
    dataset_id VARCHAR PRIMARY KEY, day DATE, label VARCHAR,
    created_at TIMESTAMP DEFAULT now(), ingested_at TIMESTAMP,
    status VARCHAR DEFAULT 'ingested');
"""

ERAS: dict[str, tuple[str, str]] = {
    # (frames DDL, detections DDL)
    "pre-dataset": ("""
        CREATE TABLE frames (camera_id VARCHAR, frame_idx BIGINT, ts TIMESTAMP,
            time_bin BIGINT, frame_path VARCHAR, overlay_path VARCHAR,
            processed BOOLEAN DEFAULT FALSE);
        """, """
        CREATE TABLE detections (detection_id BIGINT DEFAULT nextval('seq_det'),
            camera_id VARCHAR, ts TIMESTAMP, time_bin BIGINT, frame_path VARCHAR,
            score DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_x2 DOUBLE,
            bbox_y2 DOUBLE, area_px DOUBLE, ground_px_x DOUBLE, ground_px_y DOUBLE,
            posture VARCHAR, track_id BIGINT, global_id BIGINT, motion VARCHAR,
            in_shade BOOLEAN, near_infra BOOLEAN, cluster_size INTEGER);
        """),
    # The era that actually corrupted: dataset_id had arrived, the pose overlay
    # had not, so ALTER appended pose_overlay_path AFTER `processed` instead of
    # before it — a VARCHAR and a BOOLEAN swapping places, which casts silently.
    "pre-pose": ("""
        CREATE TABLE frames (dataset_id VARCHAR, camera_id VARCHAR, frame_idx BIGINT,
            ts TIMESTAMP, time_bin BIGINT, frame_path VARCHAR, overlay_path VARCHAR,
            processed BOOLEAN DEFAULT FALSE);
        """, """
        CREATE TABLE detections (detection_id BIGINT DEFAULT nextval('seq_det'),
            dataset_id VARCHAR, camera_id VARCHAR, ts TIMESTAMP, time_bin BIGINT,
            frame_path VARCHAR, score DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE,
            bbox_x2 DOUBLE, bbox_y2 DOUBLE, area_px DOUBLE, ground_px_x DOUBLE,
            ground_px_y DOUBLE, posture VARCHAR, track_id BIGINT, global_id BIGINT,
            motion VARCHAR, in_shade BOOLEAN, near_infra BOOLEAN, cluster_size INTEGER,
            under_panel BOOLEAN, panel_id VARCHAR, region_id VARCHAR);
        """),
    # After the reversible-clip feature shipped but before the outline and the
    # M5 attribute columns — the gap the clipped_* ALTERs exist to close.
    "pre-mask": ("""
        CREATE TABLE frames (dataset_id VARCHAR, camera_id VARCHAR, frame_idx BIGINT,
            ts TIMESTAMP, time_bin BIGINT, frame_path VARCHAR, overlay_path VARCHAR,
            pose_overlay_path VARCHAR, processed BOOLEAN DEFAULT FALSE);
        """, """
        CREATE TABLE detections (detection_id BIGINT DEFAULT nextval('seq_det'),
            dataset_id VARCHAR, camera_id VARCHAR, ts TIMESTAMP, time_bin BIGINT,
            frame_path VARCHAR, score DOUBLE, bbox_x1 DOUBLE, bbox_y1 DOUBLE,
            bbox_x2 DOUBLE, bbox_y2 DOUBLE, area_px DOUBLE, ground_px_x DOUBLE,
            ground_px_y DOUBLE, posture VARCHAR, track_id BIGINT, global_id BIGINT,
            motion VARCHAR, in_shade BOOLEAN, near_infra BOOLEAN, cluster_size INTEGER,
            under_panel BOOLEAN, panel_id VARCHAR, region_id VARCHAR);
        """),
}


def _open_era(path: str, era: str, *, clipped: bool = False):
    """A database as it was in `era`, then migrated by today's init_db.

    `clipped` replays the one ordering hazard init_db cannot fix with an ALTER
    on `detections` alone: the clip staging tables are CTAS copies, so a DB where
    they already existed never picks up a column added afterwards.
    """
    con = duckdb.connect(path)
    frames_ddl, det_ddl = ERAS[era]
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_det START 1;")
    con.execute(_DATASETS)
    con.execute(frames_ddl)
    con.execute(det_ddl)
    if clipped:
        con.execute("CREATE TABLE clipped_frames AS SELECT * FROM frames WHERE 1=0")
        con.execute("CREATE TABLE clipped_detections AS SELECT * FROM detections WHERE 1=0")
    db.init_db(con)
    return con


def _fresh(path: str):
    con = duckdb.connect(path)
    db.init_db(con)
    return con


# ------------------------------------------------------------------- the tests

def test_every_era_migrates_to_the_same_set_of_columns():
    """Order is allowed to differ — nothing may depend on it any more — but a
    migrated database must not be MISSING a column, or every query naming it
    raises on a box that has simply been running a long time."""
    with tmpdir() as d:
        fresh = _fresh(os.path.join(d, "fresh.duckdb"))
        want = {t: set(cols(fresh, t)) for t in
                ("datasets", "frames", "detections", "clipped_frames", "clipped_detections")}
        fresh.close()
        for era in ERAS:
            for clipped in (False, True):
                tag = f"{era}{' +clip-staging' if clipped else ''}"
                con = _open_era(os.path.join(d, f"{era}-{clipped}.duckdb"), era, clipped=clipped)
                for table, expected in want.items():
                    got = set(cols(con, table))
                    check(f"{tag}: {table} has every column a fresh DB has",
                          expected <= got, f"missing {sorted(expected - got)}")
                con.close()


def test_the_column_lists_the_code_writes_through_all_exist():
    """DET_COLS / FRAME_COLS are named explicitly in `clip_camera` and
    `restore_clip`. A column on one of those lists that the staging table never
    received is a BinderException on the first clip of a long-deployed camera."""
    with tmpdir() as d:
        for era in ERAS:
            con = _open_era(os.path.join(d, f"{era}.duckdb"), era, clipped=True)
            for table, wanted in (("frames", db.FRAME_COLS),
                                  ("clipped_frames", db.FRAME_COLS),
                                  ("detections", db.DET_COLS),
                                  ("clipped_detections", db.DET_COLS)):
                have = set(cols(con, table))
                check(f"{era}: {table} carries every column the code names",
                      set(wanted) <= have, f"missing {sorted(set(wanted) - have)}")
            con.close()


def test_init_db_is_idempotent():
    """It runs on every single boot. A second pass must be a no-op, not an
    error and not a schema change — otherwise a restart is a migration."""
    with tmpdir() as d:
        for era in list(ERAS) + ["fresh"]:
            path = os.path.join(d, f"idem-{era}.duckdb")
            con = _fresh(path) if era == "fresh" else _open_era(path, era)
            before = {t: cols(con, t) for t in
                      ("datasets", "frames", "detections",
                       "clipped_frames", "clipped_detections")}
            try:
                db.init_db(con)
                db.init_db(con)
                raised = ""
            except Exception as e:  # noqa: BLE001 — the failure IS the finding
                raised = f"{type(e).__name__}: {e}"
            check(f"{era}: re-running init_db does not raise", not raised, raised)
            after = {t: cols(con, t) for t in before}
            check(f"{era}: re-running init_db does not change the schema",
                  before == after,
                  next((t for t in before if before[t] != after[t]), ""))
            con.close()


def _seed(con, dataset_id: str) -> None:
    con.execute("INSERT INTO datasets (dataset_id, day, label) VALUES (?, DATE '2026-01-02', 'a day')",
                [dataset_id])
    con.execute(
        """INSERT INTO frames (dataset_id, camera_id, frame_idx, ts, time_bin,
                               frame_path, overlay_path, processed, overlay_rev)
           VALUES (?, 'camA', 7, TIMESTAMP '2026-01-02 09:00:00', 32400,
                   'frames/7.jpg', 'overlays/7.jpg', TRUE, 'rev9')""", [dataset_id])
    con.execute(
        """INSERT INTO detections (dataset_id, camera_id, ts, time_bin, frame_path, score,
                                   bbox_x1, bbox_y1, bbox_x2, bbox_y2, area_px,
                                   region_id, mask_poly, mask_parts,
                                   attr_is_cow, attr_cow_p, attr_behaviour,
                                   attr_behaviour_p, attr_model_rev)
           VALUES (?, 'camA', TIMESTAMP '2026-01-02 09:00:00', 32400, 'frames/7.jpg', 0.91,
                   10, 20, 110, 140, 10000, 'yard', '[[1,2],[3,4]]', 2,
                   TRUE, 0.97, 'behaviour.lying', 0.88, 'rev9')""", [dataset_id])


# What the archive must contain afterwards, read back BY NAME.
_EXPECT_FRAME = ("ds1", "camA", 7, "frames/7.jpg", "overlays/7.jpg", None, True, "rev9")
_EXPECT_DET = ("ds1", "camA", 0.91, 10.0, 140.0, "yard", "[[1,2],[3,4]]", 2,
               True, 0.97, "behaviour.lying", 0.88, "rev9")


def test_archiving_from_any_era_preserves_every_value():
    """THE REGRESSION THIS FILE EXISTS FOR.

    `archive_dataset` copies the day out and then DELETEs it, so the archive is
    the only copy. With the old positional `SELECT *`, a pre-pose database
    archived without raising and wrote `processed = TRUE` into
    `pose_overlay_path` as the string 'true', leaving `processed` NULL. A
    pre-dataset database raised ConversionException instead — non-destructive,
    but archiving was simply impossible there.

    Read back by NAME, so the assertion cannot be satisfied by a row that merely
    has the right values in the wrong columns."""
    with tmpdir() as d:
        for era in ERAS:
            live = _open_era(os.path.join(d, f"a-{era}.duckdb"), era)
            _seed(live, "ds1")
            arch_path = os.path.join(d, f"archive-{era}.duckdb")
            try:
                n = db.archive_dataset(live, "ds1", arch_path)
                raised = ""
            except Exception as e:  # noqa: BLE001
                n, raised = -1, f"{type(e).__name__}: {str(e)[:120]}"
            check(f"{era}: archiving succeeds", not raised, raised)
            live.close()
            if raised:
                continue

            check(f"{era}: it reports the one detection it moved", n == 1, str(n))
            a = duckdb.connect(arch_path, read_only=True)
            frame = a.execute(
                """SELECT dataset_id, camera_id, frame_idx, frame_path, overlay_path,
                          pose_overlay_path, processed, overlay_rev FROM frames""").fetchone()
            check(f"{era}: every frame value lands in its own column",
                  frame == _EXPECT_FRAME, f"{frame} != {_EXPECT_FRAME}")
            det = a.execute(
                """SELECT dataset_id, camera_id, score, bbox_x1, bbox_y2, region_id,
                          mask_poly, mask_parts, attr_is_cow, attr_cow_p,
                          attr_behaviour, attr_behaviour_p, attr_model_rev
                   FROM detections""").fetchone()
            check(f"{era}: every detection value lands in its own column",
                  det == _EXPECT_DET, f"{det} != {_EXPECT_DET}")
            a.close()

            live = duckdb.connect(os.path.join(d, f"a-{era}.duckdb"))
            left = live.execute("SELECT count(*) FROM detections WHERE dataset_id = 'ds1'").fetchone()[0]
            check(f"{era}: the live rows are gone once the archive holds them", left == 0, str(left))
            live.close()


# The eras whose migrated column order MUST differ from a fresh database.
# `pre-mask` deliberately is not one: every column it lacks is appended at the
# tail of the CREATE TABLE body too, so ALTER order and CREATE order agree and a
# positional copy from it would have worked. That is exactly why the hazard went
# unnoticed for so long — the most recent era is the one where it does not bite.
_MUST_DIVERGE = ("pre-dataset", "pre-pose")


def test_a_positional_copy_would_still_be_wrong():
    """A guard on the guard.

    `test_archiving_from_any_era_preserves_every_value` only means anything
    while the eras it replays genuinely migrate to a DIFFERENT column order from
    a fresh database — if they all agreed, a positional `SELECT *` would pass it
    too and the regression pin would be silently vacuous. Assert the divergence
    is still there, so a refactor that removes it has to say so out loud."""
    with tmpdir() as d:
        fresh = _fresh(os.path.join(d, "fresh.duckdb"))
        fresh_order = {t: cols(fresh, t) for t in ("frames", "detections")}
        fresh.close()
        for era in _MUST_DIVERGE:
            con = _open_era(os.path.join(d, f"o-{era}.duckdb"), era)
            differs = {t for t in fresh_order if cols(con, t) != fresh_order[t]}
            check(f"{era} still migrates to a different column order than a fresh DB",
                  bool(differs),
                  "it now agrees with a fresh DB, so the archive test proves nothing")
            con.close()


def test_archiving_carries_the_clip_staging_tables():
    """`clip_camera` MOVES out-of-window rows into staging rather than deleting
    them, on the promise that a clip is undoable. Archiving ignored those tables
    entirely — so the archive held an incomplete copy of the day while the live
    DB kept orphan rows whose `datasets` row no longer existed, and
    `clipped_counts` went on advertising an Undo control for a deleted day."""
    with tmpdir() as d:
        live_path = os.path.join(d, "live.duckdb")
        con = _fresh(live_path)
        _seed(con, "ds1")
        # A second frame + detection two hours later, outside the clip window.
        con.execute(
            """INSERT INTO frames (dataset_id, camera_id, frame_idx, ts, frame_path, processed)
               VALUES ('ds1', 'camA', 8, TIMESTAMP '2026-01-02 11:00:00', 'frames/8.jpg', TRUE)""")
        con.execute(
            """INSERT INTO detections (dataset_id, camera_id, ts, frame_path, score, mask_poly)
               VALUES ('ds1', 'camA', TIMESTAMP '2026-01-02 11:00:00', 'frames/8.jpg',
                       0.5, '[[9,9]]')""")
        db.clip_camera(con, "ds1", "camA", "2026-01-02 08:00:00", "2026-01-02 10:00:00")
        check("the clip staged one frame", db.clipped_counts(con, "ds1") == {"camA": 1},
              str(db.clipped_counts(con, "ds1")))

        arch = os.path.join(d, "archive.duckdb")
        db.archive_dataset(con, "ds1", arch)
        check("no staged row is left behind in the live DB",
              con.execute("SELECT count(*) FROM clipped_frames").fetchone()[0] == 0)
        check("...so no phantom Undo control survives the day",
              db.clipped_counts(con, "ds1") == {}, str(db.clipped_counts(con, "ds1")))
        con.close()

        a = duckdb.connect(arch, read_only=True)
        try:
            for table, want in (("frames", 1), ("detections", 1),
                                ("clipped_frames", 1), ("clipped_detections", 1)):
                got = a.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                check(f"the archive holds the day's {table}", got == want, str(got))
            poly = a.execute("SELECT mask_poly FROM clipped_detections").fetchone()
            check("...with the staged outline intact", poly == ("[[9,9]]",), str(poly))
        finally:
            a.close()


def test_clip_and_restore_round_trip_on_a_migrated_database():
    """`clip_camera` moves out-of-window rows into the staging tables and
    `restore_clip` moves them back, both naming DET_COLS/FRAME_COLS. On a DB
    where the staging tables predate the outline and attribute columns, the
    named INSERT is the first thing to notice."""
    with tmpdir() as d:
        for era in ERAS:
            con = _open_era(os.path.join(d, f"c-{era}.duckdb"), era, clipped=True)
            _seed(con, "ds1")
            # A second frame/detection two hours later, outside the window below.
            con.execute(
                """INSERT INTO frames (dataset_id, camera_id, frame_idx, ts, frame_path, processed)
                   VALUES ('ds1', 'camA', 8, TIMESTAMP '2026-01-02 11:00:00', 'frames/8.jpg', TRUE)""")
            con.execute(
                """INSERT INTO detections (dataset_id, camera_id, ts, frame_path, score,
                                           mask_poly, attr_behaviour, attr_model_rev)
                   VALUES ('ds1', 'camA', TIMESTAMP '2026-01-02 11:00:00', 'frames/8.jpg',
                           0.5, '[[9,9]]', 'behaviour.standing', 'rev9')""")
            try:
                res = db.clip_camera(con, "ds1", "camA",
                                     "2026-01-02 08:00:00", "2026-01-02 10:00:00")
                raised = ""
            except Exception as e:  # noqa: BLE001
                res, raised = {}, f"{type(e).__name__}: {str(e)[:120]}"
            check(f"{era}: clipping a camera does not raise", not raised, raised)
            if raised:
                con.close()
                continue
            check(f"{era}: it clips exactly the out-of-window frame",
                  res.get("removed") == 1 and res.get("kept") == 1, str(res))

            db.restore_clip(con, "ds1", "camA")
            back = con.execute(
                """SELECT mask_poly, attr_behaviour, attr_model_rev FROM detections
                   WHERE dataset_id = 'ds1' AND ts = TIMESTAMP '2026-01-02 11:00:00'""").fetchone()
            check(f"{era}: a restored detection keeps its outline and its score",
                  back == ("[[9,9]]", "behaviour.standing", "rev9"), str(back))
            n = con.execute("SELECT count(*) FROM detections WHERE dataset_id = 'ds1'").fetchone()[0]
            check(f"{era}: the restore neither loses nor duplicates rows", n == 2, str(n))
            con.close()


def main():
    test_every_era_migrates_to_the_same_set_of_columns()
    test_the_column_lists_the_code_writes_through_all_exist()
    test_init_db_is_idempotent()
    test_archiving_from_any_era_preserves_every_value()
    test_archiving_carries_the_clip_staging_tables()
    test_a_positional_copy_would_still_be_wrong()
    test_clip_and_restore_round_trip_on_a_migrated_database()
    print(f"\n{'ALL OK' if not _FAILED else f'{_FAILED} FAILED'}")
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
