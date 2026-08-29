"""Label-store contract tests: the stable instance key and the store's semantics.

The load-bearing one is test_python_and_sql_keys_agree — it pins the two key
producers (labels_db.instance_key in Python, labels_db.instance_key_sql in
DuckDB) to each other over adversarial rows: half-pixel coordinates (where
Python's banker's round() and DuckDB's half-away-from-zero disagree), negative
coordinates, a NULL dataset_id, and duplicate boxes that need the ordinal
tiebreak. The queue mints keys in SQL and the submit path verifies them in
Python, so a divergence silently 400s roughly half of all submissions.

No pytest. Run either way:
    .venv/bin/python -m tests.test_labels_schema
    .venv/bin/python tests/test_labels_schema.py
"""
from __future__ import annotations

import os
import re
import sys
import tempfile

import duckdb
import pandas as pd

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, labels_db  # noqa: E402

_FAILED = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _FAILED
    status = "ok " if cond else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)
    if not cond:
        _FAILED += 1
        # A check that only prints is invisible to pytest: the file reports
        # green while assertions inside it fail. `python -m tests` (the
        # pre-boot gate) counts them, but nothing else does.
        if os.environ.get("PYTEST_CURRENT_TEST"):
            raise AssertionError(line)


# One logical frame in every spelling a deployment can produce. The key must not
# care which box ingested it.
_BBOX = [10.0, 20.0, 110.0, 220.0]

# Ready-made choice rows (the shape resolve_choices produces) for the seeded
# taxonomy, so write tests don't re-derive them per call.
_SHADED = {"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
           "class_name": "Shaded"}
_SUN = {"class_key": "sun_exposure.direct_sun", "group_key": "sun_exposure",
        "class_name": "Direct sun"}


def _store(d: str) -> duckdb.DuckDBPyConnection:
    con = db.connect(os.path.join(d, "labels.duckdb"))
    labels_db.init_labels_db(con)
    return con


# The SQL producer over a detections-shaped relation, selecting the ordinal from
# the same window so the Python side can re-derive each row's key exactly.
def _key_scan_sql(extra_cols: str = "") -> str:
    return (
        f"SELECT {extra_cols}d.dataset_id, d.camera_id, d.frame_path, "
        "d.bbox_x1, d.bbox_y1, d.bbox_x2, d.bbox_y2, d.score, "
        f"{labels_db.instance_ordinal_sql('d')} AS ordinal, "
        f"{labels_db.instance_key_sql('d')} AS sql_key "
        "FROM detections d"
    )


def test_key_is_path_and_platform_stable():
    spellings = [
        r"data\artifacts\2026-07-03\frames\camera_01\00000450.jpg",   # Windows dev box
        "data/artifacts/2026-07-03/frames/camera_01/00000450.jpg",    # container
        "/mnt/elsewhere/art/2026-07-03/frames/camera_01/00000450.jpg",  # relocated artifacts_dir
        "00000450.jpg",                                               # already-reduced basename
    ]
    keys = {labels_db.instance_key("2026-07-03", "camera_01", p, _BBOX, 0)
            for p in spellings}
    check("same logical frame keys identically from every path spelling",
          len(keys) == 1, str(keys))
    k = keys.pop()
    check("key is 32 lowercase hex chars",
          re.fullmatch(r"[0-9a-f]{32}", k) is not None, k)


def test_key_quantisation_and_guards():
    base = "00000450.jpg"
    # Sub-pixel jitter: both quantise to (10, 20, 110, 220).
    k_a = labels_db.instance_key("d", "cam", base, [10.2, 20.3, 110.1, 220.4], 0)
    k_b = labels_db.instance_key("d", "cam", base, [10.4, 19.8, 109.9, 220.2], 0)
    check("sub-pixel jitter keys the same", k_a == k_b, f"{k_a} vs {k_b}")
    # A whole-pixel move is a different animal.
    k_c = labels_db.instance_key("d", "cam", base, [11.0, 20.0, 110.0, 220.0], 0)
    check("a whole-pixel move keys differently", k_a != k_c)
    # 0.5 rounds UP (floor(v + 0.5)), never to even like Python's round().
    k_half = labels_db.instance_key("d", "cam", base, [0.5, 20.0, 110.0, 220.0], 0)
    k_one = labels_db.instance_key("d", "cam", base, [1.0, 20.0, 110.0, 220.0], 0)
    k_zero = labels_db.instance_key("d", "cam", base, [0.0, 20.0, 110.0, 220.0], 0)
    check("0.5 rounds up to 1, not to even 0",
          k_half == k_one and k_half != k_zero)
    # An empty frame_path (ingest.save_frames: false) is refused, naming the cause.
    try:
        labels_db.instance_key("d", "cam", "", _BBOX, 0)
        check("empty frame_path is refused", False, "no exception raised")
    except ValueError as e:
        check("empty frame_path is refused", True)
        check("refusal names save_frames as the cause", "save_frames" in str(e), str(e))


def test_python_and_sql_keys_agree():
    """THE load-bearing pin: instance_key() == instance_key_sql() per row, over a
    table built to break any producer that rounds, splits paths, or numbers ties
    differently from the other."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "cownting.duckdb"))
        db.init_db(con)
        cam = "camera_01"
        rows = [
            # NULL dataset_id + Windows backslash path + .5 coords everywhere.
            {"dataset_id": None, "camera_id": cam,
             "frame_path": r"data\artifacts\legacy\frames\camera_01\00000001.jpg",
             "bbox_x1": 0.5, "bbox_y1": 1.5, "bbox_x2": 100.5, "bbox_y2": 200.5,
             "score": 0.9},
            # Negative halves (-3.5 -> -3, -0.5 -> 0) and a whole float (12.0,
            # which must hash as "12", never "12.0").
            {"dataset_id": "2026-07-03", "camera_id": cam,
             "frame_path": "data/artifacts/2026-07-03/frames/camera_01/00000002.jpg",
             "bbox_x1": -3.5, "bbox_y1": -0.5, "bbox_x2": 12.0, "bbox_y2": 47.25,
             "score": 0.8},
            # NULL score: exercises NULLS LAST in the ordinal's ORDER BY.
            {"dataset_id": "2026-07-03", "camera_id": cam,
             "frame_path": "data/artifacts/2026-07-03/frames/camera_01/00000005.jpg",
             "bbox_x1": -2.5, "bbox_y1": 5.5, "bbox_x2": 60.0, "bbox_y2": 80.5,
             "score": None},
            # Two rows sharing one quantised box (10, 10, 50, 50): the ordinal
            # tiebreak must number them 0 and 1 by score DESC.
            {"dataset_id": "2026-07-03", "camera_id": cam,
             "frame_path": "data/artifacts/2026-07-03/frames/camera_01/00000003.jpg",
             "bbox_x1": 10.2, "bbox_y1": 10.2, "bbox_x2": 50.2, "bbox_y2": 50.2,
             "score": 0.9},
            {"dataset_id": "2026-07-03", "camera_id": cam,
             "frame_path": "data/artifacts/2026-07-03/frames/camera_01/00000003.jpg",
             "bbox_x1": 10.4, "bbox_y1": 10.3, "bbox_x2": 50.4, "bbox_y2": 49.7,
             "score": 0.5},
            # Two byte-identical rows: dense_rank must give them the SAME ordinal
            # and therefore the same key.
            {"dataset_id": "2026-07-03", "camera_id": cam,
             "frame_path": "data/artifacts/2026-07-03/frames/camera_01/00000004.jpg",
             "bbox_x1": 7.0, "bbox_y1": 8.0, "bbox_x2": 70.0, "bbox_y2": 90.0,
             "score": 0.7},
            {"dataset_id": "2026-07-03", "camera_id": cam,
             "frame_path": "data/artifacts/2026-07-03/frames/camera_01/00000004.jpg",
             "bbox_x1": 7.0, "bbox_y1": 8.0, "bbox_x2": 70.0, "bbox_y2": 90.0,
             "score": 0.7},
        ]
        db.insert_detections(con, pd.DataFrame(rows))
        got = con.execute(_key_scan_sql()).fetchall()
        check("scan returns every seeded row", len(got) == len(rows), str(len(got)))
        for i, (ds, camera, path, x1, y1, x2, y2, _score, ordinal, sql_key) in enumerate(got):
            py_key = labels_db.instance_key(ds, camera, path, [x1, y1, x2, y2], int(ordinal))
            check(f"row {i}: Python and SQL agree ({os.path.basename(str(path))} ord {ordinal})",
                  py_key == sql_key, f"py={py_key} sql={sql_key}")
            check(f"row {i}: SQL key is 32 lowercase hex",
                  re.fullmatch(r"[0-9a-f]{32}", str(sql_key)) is not None, str(sql_key))
        # 7 rows, but the two byte-identical ones share a key: 6 distinct.
        keys = [r[-1] for r in got]
        check("byte-identical rows share a key; all others distinct",
              len(set(keys)) == 6, str(len(set(keys))))
        con.close()


def test_ordinal_is_dense_and_total():
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "cownting.duckdb"))
        db.init_db(con)
        frame = "data/artifacts/2026-07-03/frames/camera_01/00000003.jpg"
        db.insert_detections(con, pd.DataFrame([
            # Same quantised box, different scores -> ordinals 0 and 1.
            {"dataset_id": "2026-07-03", "camera_id": "camera_01", "frame_path": frame,
             "bbox_x1": 10.2, "bbox_y1": 10.2, "bbox_x2": 50.2, "bbox_y2": 50.2, "score": 0.9},
            {"dataset_id": "2026-07-03", "camera_id": "camera_01", "frame_path": frame,
             "bbox_x1": 10.4, "bbox_y1": 10.3, "bbox_x2": 50.4, "bbox_y2": 49.7, "score": 0.5},
            # Identical on every ordering column, twice -> the SAME ordinal.
            {"dataset_id": "2026-07-03", "camera_id": "camera_01", "frame_path": frame,
             "bbox_x1": 200.0, "bbox_y1": 210.0, "bbox_x2": 260.0, "bbox_y2": 290.0, "score": 0.7},
            {"dataset_id": "2026-07-03", "camera_id": "camera_01", "frame_path": frame,
             "bbox_x1": 200.0, "bbox_y1": 210.0, "bbox_x2": 260.0, "bbox_y2": 290.0, "score": 0.7},
        ]))
        sql = _key_scan_sql()
        first = con.execute(sql).fetchall()
        by_score = {(r[7], int(r[8])) for r in first}
        check("score 0.9 gets ordinal 0, score 0.5 gets ordinal 1 (dense, total)",
              {(0.9, 0), (0.5, 1)}.issubset(by_score), str(sorted(by_score, key=str)))
        twins = [r for r in first if r[7] == 0.7]
        check("rows identical on every ordering column get the SAME ordinal",
              len(twins) == 2 and twins[0][8] == twins[1][8],
              str([t[8] for t in twins]))
        check("...and therefore the same key",
              len(twins) == 2 and twins[0][9] == twins[1][9])
        # Stable across repeated executions: DuckDB's parallel scan may emit tied
        # rows in either order, and dense_rank must not care.
        for run in range(5):
            again = con.execute(sql).fetchall()
            check(f"re-execution {run}: identical (row -> ordinal, key) assignment",
                  sorted(map(str, again)) == sorted(map(str, first)))
        con.close()


def test_key_survives_clip_and_restore():
    from datetime import datetime

    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "cownting.duckdb"))
        db.init_db(con)
        ds, cam = "2026-07-03", "camera_01"
        f1 = f"data/artifacts/{ds}/frames/{cam}/00000001.jpg"
        f2 = f"data/artifacts/{ds}/frames/{cam}/00000002.jpg"
        db.insert_frames(con, pd.DataFrame([
            {"dataset_id": ds, "camera_id": cam, "frame_idx": 1,
             "ts": datetime(2026, 7, 3, 6, 0), "frame_path": f1},
            {"dataset_id": ds, "camera_id": cam, "frame_idx": 2,
             "ts": datetime(2026, 7, 3, 6, 1), "frame_path": f2},
        ]))
        db.insert_detections(con, pd.DataFrame([
            {"dataset_id": ds, "camera_id": cam, "ts": datetime(2026, 7, 3, 6, 0),
             "frame_path": f1, "score": 0.9,
             "bbox_x1": 10.5, "bbox_y1": 20.0, "bbox_x2": 110.5, "bbox_y2": 220.0},
            {"dataset_id": ds, "camera_id": cam, "ts": datetime(2026, 7, 3, 6, 1),
             "frame_path": f2, "score": 0.8,
             "bbox_x1": 30.0, "bbox_y1": 40.0, "bbox_x2": 130.0, "bbox_y2": 240.0},
        ]))
        scan = _key_scan_sql("d.detection_id, ")
        before = con.execute(scan).fetchall()
        ids_before = {int(r[0]) for r in before}
        keys_before = {r[-1] for r in before}

        # Clip to a window that excludes everything: all rows move to staging.
        db.clip_camera(con, ds, cam, "2026-07-03T23:00:00", "2026-07-03T23:30:00")
        check("clip staged every detection",
              con.execute("SELECT count(*) FROM detections").fetchone()[0] == 0)
        restored = db.restore_clip(con, ds, cam)
        check("restore brought both frames back", restored == 2, str(restored))

        after = con.execute(scan).fetchall()
        ids_after = {int(r[0]) for r in after}
        keys_after = {r[-1] for r in after}
        # DET_COLS excludes detection_id, so a restore mints fresh ids — the
        # documented reason detection_id can never be the label key.
        check("detection_id IS re-minted by restore_clip",
              ids_after.isdisjoint(ids_before), f"{ids_before} -> {ids_after}")
        check("instance_key is NOT re-minted by restore_clip",
              keys_after == keys_before)
        con.close()


def _reingest_fixture(d: str):
    """One dataset, one camera, ONE detection — so the re-ingest shift below has
    exactly one candidate and `_best_candidate`'s runner-up guard cannot mask a
    failure of the thing under test."""
    from datetime import datetime

    ds, cam, base = "2026-07-03", "camera_01", "00000001.jpg"
    f1 = f"data/artifacts/{ds}/frames/{cam}/{base}"
    mcon = db.connect(os.path.join(d, "cownting.duckdb"))
    db.init_db(mcon)
    db.upsert_dataset(mcon, ds, __import__("datetime").date(2026, 7, 3),
                      "Jul 03, 2026", status="localized")
    db.insert_frames(mcon, pd.DataFrame([
        {"dataset_id": ds, "camera_id": cam, "frame_idx": 1,
         "ts": datetime(2026, 7, 3, 6, 0), "frame_path": f1}]))
    db.insert_detections(mcon, pd.DataFrame([
        {"dataset_id": ds, "camera_id": cam, "ts": datetime(2026, 7, 3, 6, 0),
         "frame_path": f1, "score": 0.9,
         "bbox_x1": 10.0, "bbox_y1": 20.0, "bbox_x2": 110.0, "bbox_y2": 220.0}]))
    bbox = [10.0, 20.0, 110.0, 220.0]
    prov = {"dataset_id": ds, "camera_id": cam, "frame_basename": base,
            "frame_path": f1, "bbox_x1": bbox[0], "bbox_y1": bbox[1],
            "bbox_x2": bbox[2], "bbox_y2": bbox[3], "ordinal": 0}
    return mcon, _store(d), ds, cam, base, bbox, prov


def _shift_boxes(mcon, dx: float = 3.0) -> str:
    """A re-ingest with a drifted detector: the box moves a few px, so the key
    changes while the animal did not. 3px on a 100x200 box is IoU 0.94 — over
    IOU_ATTACH with no runner-up, i.e. the `aliased` branch."""
    mcon.execute("UPDATE detections SET bbox_x1 = bbox_x1 + ?, bbox_x2 = bbox_x2 + ?",
                 [dx, dx])
    return str(mcon.execute(_key_scan_sql()).fetchall()[0][-1])


def test_a_false_positive_stays_removed_across_a_rekey():
    """REGRESSION. 'Not a cow' lives in `mask_edits` and NOWHERE else — the route
    retires the instance, so the annotator never answers it and no `annotations`
    row is ever written. A repair that both enumerates and rewrites only
    `annotations` therefore never SEES this key: no alias, no reconciliation row,
    no move. `v_false_positives` is left pointing at a key no detection has, the
    queue's anti-join misses, and a detection a human declared not to be an animal
    walks back into everybody's queue."""
    with tempfile.TemporaryDirectory() as d:
        mcon, lcon, ds, cam, base, bbox, prov = _reingest_fixture(d)
        old_key = labels_db.instance_key(ds, cam, base, bbox, 0)
        labels_db.submit_mask_edit(lcon, instance_key=old_key, annotator="alice",
                                   kind="false_positive", seeded_from="model",
                                   provenance=prov)
        check("the removal is visible to the queue's anti-join before the re-ingest",
              lcon.execute("SELECT count(*) FROM v_false_positives WHERE effective_key = ?",
                           [old_key]).fetchone()[0] == 1)
        check("...and it is the ONLY human work on this instance",
              lcon.execute("SELECT count(*) FROM annotations").fetchone()[0] == 0)

        new_key = _shift_boxes(mcon)
        check("the re-ingest really did change the key", new_key != old_key)

        rep = labels_db.reconcile_dataset(lcon, mcon, ds, actor="test")
        check("the reconciler SEES an instance that has no annotations",
              rep["instances"] == 1 and rep["outline_only_instances"] == 1, str(rep))
        check("...and aliases it", rep["states"]["aliased"] == 1, str(rep["states"]))
        check("...and reports the mask_edits row it moved",
              rep["rekeyed"]["mask_edits"] == 1, str(rep["rekeyed"]))
        check("the removal now joins to the LIVE detection",
              lcon.execute("SELECT count(*) FROM v_false_positives WHERE effective_key = ?",
                           [new_key]).fetchone()[0] == 1)
        check("...and no longer to the dead one — it MOVED, it did not fan out",
              lcon.execute("SELECT count(*) FROM v_false_positives WHERE effective_key = ?",
                           [old_key]).fetchone()[0] == 0)
        check("instance_key is untouched: it records what was actually hashed",
              lcon.execute("SELECT count(*) FROM mask_edits WHERE instance_key = ?",
                           [old_key]).fetchone()[0] == 1)
        mcon.close(); lcon.close()


def test_an_outline_correction_follows_its_instance():
    """REGRESSION. The answers and the corrected outline are two rows about ONE
    cow, written in one sitting. A repair that moves only `annotations` tears them
    apart: the answers re-attach, the polygon does not, and the annotator is
    re-served the same animal with the bare model outline while their correction
    is invisible forever."""
    with tempfile.TemporaryDirectory() as d:
        mcon, lcon, ds, cam, base, bbox, prov = _reingest_fixture(d)
        old_key = labels_db.instance_key(ds, cam, base, bbox, 0)
        labels_db.submit_annotation(lcon, instance_key=old_key, annotator="alice",
                                    outcome="labeled", choices=[_SHADED], provenance=prov)
        labels_db.submit_mask_edit(lcon, instance_key=old_key, annotator="alice",
                                   kind="polygon",
                                   polygon=[[12.0, 22.0], [108.0, 30.0], [60.0, 215.0]],
                                   seeded_from="bbox", provenance=prov)
        new_key = _shift_boxes(mcon)
        rep = labels_db.reconcile_dataset(lcon, mcon, ds, actor="test")
        check("both tables were rewritten, not just the answers",
              rep["rekeyed"] == {"annotations": 1, "mask_edits": 1}, str(rep["rekeyed"]))
        eff = lcon.execute("SELECT effective_key FROM v_current_mask_edits "
                           "WHERE instance_key = ?", [old_key]).fetchone()
        check("the correction points at the live detection", eff == (new_key,), str(eff))
        row = lcon.execute("SELECT kind, n_vertices FROM v_current_mask_edits "
                           "WHERE effective_key = ?", [new_key]).fetchone()
        check("...and is still the polygon, intact — a move, not a rewrite",
              row == ("polygon", 3), str(row))
        ans = lcon.execute("SELECT effective_key FROM v_current_answers "
                           "WHERE instance_key = ?", [old_key]).fetchone()
        check("the answer and the outline agree on which cow this is",
              ans == (new_key,) and ans == eff, f"{ans} vs {eff}")
        mcon.close(); lcon.close()


def test_rekey_collides_with_nothing_when_one_annotator_has_rows_in_both_tables():
    """The scariest shape: alice has several versions in BOTH tables under the old
    key, AND has already worked the re-ingested detection under the new one.

    A fix that rewrote `instance_key`, or that superseded-and-reinserted, would
    violate UNIQUE (instance_key, annotator, version) the moment those identities
    met. Rewriting only `effective_key` — which is in no UNIQUE and no PK on
    either table — cannot. This test is what says so, and what stops the next
    person 'simplifying' it into a merge."""
    with tempfile.TemporaryDirectory() as d:
        mcon, lcon, ds, cam, base, bbox, prov = _reingest_fixture(d)
        old_key = labels_db.instance_key(ds, cam, base, bbox, 0)
        for _ in range(2):
            labels_db.submit_annotation(lcon, instance_key=old_key, annotator="alice",
                                        outcome="labeled", choices=[_SHADED],
                                        provenance=prov)
        labels_db.submit_mask_edit(lcon, instance_key=old_key, annotator="alice",
                                   kind="polygon",
                                   polygon=[[12.0, 22.0], [108.0, 30.0], [60.0, 215.0]],
                                   seeded_from="bbox", provenance=prov)
        labels_db.submit_mask_edit(lcon, instance_key=old_key, annotator="alice",
                                   kind="false_positive", seeded_from="edit",
                                   provenance=prov)
        new_key = _shift_boxes(mcon)
        new_prov = {**prov, "bbox_x1": bbox[0] + 3, "bbox_x2": bbox[2] + 3}
        labels_db.submit_annotation(lcon, instance_key=new_key, annotator="alice",
                                    outcome="labeled", choices=[_SHADED],
                                    provenance=new_prov)
        labels_db.submit_mask_edit(lcon, instance_key=new_key, annotator="alice",
                                   kind="ok", seeded_from="model", provenance=new_prov)

        before = {t: lcon.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
                  for t in ("annotations", "mask_edits")}
        rep = labels_db.reconcile_dataset(lcon, mcon, ds, actor="test")
        check("the re-key completes without a constraint violation",
              rep["states"]["aliased"] == 1, str(rep["states"]))
        check("nothing inserted, nothing deleted — only a column moved",
              {t: lcon.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
               for t in ("annotations", "mask_edits")} == before, str(before))
        for t in ("annotations", "mask_edits"):
            dupes = lcon.execute(
                f"SELECT count(*) FROM (SELECT instance_key, annotator, version "
                f"FROM {t} GROUP BY 1, 2, 3 HAVING count(*) > 1)").fetchone()[0]
            check(f"UNIQUE (instance_key, annotator, version) still holds on {t}",
                  dupes == 0, str(dupes))
        check("all of alice's history, both keys and both tables, is on the live key",
              lcon.execute("SELECT count(*) FROM mask_edits WHERE effective_key <> ?",
                           [new_key]).fetchone()[0] == 0
              and lcon.execute("SELECT count(*) FROM annotations WHERE effective_key <> ?",
                               [new_key]).fetchone()[0] == 0)
        check("both instance_keys survive: the audit trail is not collapsed",
              {r[0] for r in lcon.execute(
                  "SELECT DISTINCT instance_key FROM mask_edits").fetchall()}
              == {old_key, new_key})
        again = labels_db.reconcile_dataset(lcon, mcon, ds, actor="test")
        check("re-running moves nothing and does not re-report the same move",
              again["rekeyed"] == {"annotations": 0, "mask_edits": 0},
              str(again["rekeyed"]))
        mcon.close(); lcon.close()


def test_mask_survives_clip_restore_on_a_deployed_shape_db():
    """The persisted outline must survive the same round trip the key does — and
    on the schema an ALREADY-DEPLOYED database actually has.

    That last part is the whole test. `clipped_detections` is created by a
    `CREATE TABLE IF NOT EXISTS ... AS SELECT * FROM detections WHERE 1=0`, which
    is a no-op on every database that already has the table, so a column added to
    `detections` afterwards never reaches it. mask_poly/mask_parts are the FIRST
    columns in this repo's history added after the clip feature shipped, so this
    migration path has never been exercised. A fresh-DB test cannot see it: there
    the CTAS copies the columns and the ALTERs are no-ops.

    Simulated by dropping the columns from `clipped_detections` and re-running
    init_db, which is exactly the shape a deployed DB boots with."""
    from datetime import datetime

    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "cownting.duckdb"))
        db.init_db(con)
        # Rewind to the deployed shape: the staging table as it existed before
        # outlines were persisted.
        con.execute("ALTER TABLE clipped_detections DROP COLUMN mask_poly")
        con.execute("ALTER TABLE clipped_detections DROP COLUMN mask_parts")
        db.init_db(con)  # the forward-compat pass a real boot would run
        cols = {r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'clipped_detections'").fetchall()}
        check("init_db re-adds the mask columns to an old clipped_detections",
              {"mask_poly", "mask_parts"} <= cols, str(sorted(cols)[-4:]))

        ds, cam = "2026-07-03", "camera_01"
        f1 = f"data/artifacts/{ds}/frames/{cam}/00000001.jpg"
        poly = "[[10.0,20.0],[110.0,20.0],[110.0,220.0]]"
        db.insert_frames(con, pd.DataFrame([
            {"dataset_id": ds, "camera_id": cam, "frame_idx": 1,
             "ts": datetime(2026, 7, 3, 6, 0), "frame_path": f1},
        ]))
        db.insert_detections(con, pd.DataFrame([
            {"dataset_id": ds, "camera_id": cam, "ts": datetime(2026, 7, 3, 6, 0),
             "frame_path": f1, "score": 0.9,
             "bbox_x1": 10.5, "bbox_y1": 20.0, "bbox_x2": 110.5, "bbox_y2": 220.0,
             "mask_poly": poly, "mask_parts": 2},
        ]))
        stored = con.execute("SELECT mask_poly, mask_parts FROM detections").fetchone()
        check("insert_detections carries the outline through DET_COLS",
              stored == (poly, 2), str(stored))

        db.clip_camera(con, ds, cam, "2026-07-03T23:00:00", "2026-07-03T23:30:00")
        staged = con.execute(
            "SELECT mask_poly, mask_parts FROM clipped_detections").fetchone()
        check("the outline is staged with the row, not dropped", staged == (poly, 2),
              str(staged))
        db.restore_clip(con, ds, cam)
        back = con.execute("SELECT mask_poly, mask_parts FROM detections").fetchone()
        check("...and comes back intact on restore", back == (poly, 2), str(back))
        con.close()


def test_mask_columns_are_last_so_the_archive_stays_aligned():
    """`archive_dataset` copies live -> archive with a POSITIONAL
    `INSERT INTO archive.t SELECT * FROM t`. The archive is always built fresh, so
    it gets CREATE-TABLE column order, while a long-deployed live DB gets ALTER
    order — and ALTER always appends. The two therefore agree only while new
    columns are appended LAST in the DDL body.

    Put them mid-list and a deleted day is written one slot over into the only
    surviving copy of it — silently, whenever the shifted columns happen to be
    NULL and the types happen to cast. This pins the ordering so a later tidy-up
    cannot quietly reintroduce that."""
    with tempfile.TemporaryDirectory() as d:
        con = db.connect(os.path.join(d, "cownting.duckdb"))
        db.init_db(con)
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'detections' ORDER BY ordinal_position").fetchall()]
        check("mask_poly/mask_parts are the LAST two columns of detections",
              cols[-2:] == ["mask_poly", "mask_parts"], str(cols[-3:]))
        check("...and the last two of DET_COLS, which drives every named insert",
              list(db.DET_COLS)[-2:] == ["mask_poly", "mask_parts"],
              str(list(db.DET_COLS)[-3:]))
        con.close()


def test_schema_is_idempotent_and_seeded():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "labels.duckdb")
        for _ in range(2):
            c = db.connect(path)
            labels_db.init_labels_db(c)
            c.close()
        con = db.connect(path)
        labels_db.init_labels_db(con)  # third boot
        n_groups = con.execute("SELECT count(*) FROM label_groups").fetchone()[0]
        n_classes = con.execute("SELECT count(*) FROM label_classes").fetchone()[0]
        check("three boots do not duplicate the seed (2 groups)", n_groups == 2, str(n_groups))
        check("three boots do not duplicate the seed (10 classes)", n_classes == 10, str(n_classes))
        rev = labels_db.taxonomy_revision(con)
        check("re-boots do not bump the revision (only the first seed did)",
              rev == 1, str(rev))
        groups = con.execute(
            "SELECT group_key, multi_select, required FROM label_groups ORDER BY sort_order"
        ).fetchall()
        check("both seeded groups exist, in order",
              [g[0] for g in groups] == ["sun_exposure", "behaviour"], str(groups))
        check("both groups are single-select and required",
              all(not g[1] and g[2] for g in groups), str(groups))
        for gk in ("sun_exposure", "behaviour"):
            n_escape = con.execute(
                "SELECT count(*) FROM label_classes WHERE group_key = ? AND is_escape", [gk]
            ).fetchone()[0]
            check(f"group {gk} has exactly one escape class", n_escape == 1, str(n_escape))
        thin = con.execute(
            "SELECT count(*) FROM label_classes WHERE description IS NULL "
            "OR length(trim(description)) < 40"
        ).fetchone()[0]
        check("every class carries a real description", thin == 0, f"{thin} thin/missing")

        # Archive a class, reboot: presence-of-key seeding must NOT resurrect it.
        labels_db.update_class(con, "behaviour.lying", active=False, actor="pow")
        rev_after_archive = labels_db.taxonomy_revision(con)
        con.close()
        con = db.connect(path)
        labels_db.init_labels_db(con)
        row = con.execute(
            "SELECT active, archived_at FROM label_classes WHERE class_key = 'behaviour.lying'"
        ).fetchone()
        check("an archived class stays archived across a reboot",
              row is not None and not row[0] and row[1] is not None, str(row))
        check("the reboot did not bump the revision either",
              labels_db.taxonomy_revision(con) == rev_after_archive)
        con.close()


def test_relabel_appends_and_double_submit_is_refused():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        k = labels_db.instance_key("2026-07-03", "camera_01", "00000001.jpg", _BBOX, 0)
        labels_db.submit_annotation(con, instance_key=k, annotator="alice",
                                    outcome="labeled", choices=[_SHADED])
        labels_db.submit_annotation(con, instance_key=k, annotator="alice",
                                    outcome="labeled", choices=[_SUN])
        rows = con.execute(
            "SELECT version, superseded_at IS NULL FROM annotations "
            "WHERE instance_key = ? AND annotator = 'alice' ORDER BY version", [k]
        ).fetchall()
        check("v2 appends and supersedes v1",
              [(int(r[0]), bool(r[1])) for r in rows] == [(1, False), (2, True)], str(rows))

        labels_db.submit_annotation(con, instance_key=k, annotator="bob",
                                    outcome="labeled", choices=[_SHADED])
        answers = {(r[0], r[1]) for r in con.execute(
            "SELECT annotator, class_key FROM v_current_answers WHERE effective_key = ?", [k]
        ).fetchall()}
        check("two annotators coexist on one instance",
              answers == {("alice", "sun_exposure.direct_sun"), ("bob", "sun_exposure.shaded")},
              str(answers))

        # The UNIQUE (instance_key, annotator, version) is what turns a racing
        # double submit into an error instead of a silent duplicate.
        try:
            con.execute(
                "INSERT INTO annotations (instance_key, effective_key, annotator, version) "
                "VALUES (?, ?, 'alice', 2)", [k, k]
            )
            check("UNIQUE blocks a silent double submit", False, "duplicate row accepted")
        except duckdb.ConstraintException:
            check("UNIQUE blocks a silent double submit", True)

        rep = labels_db.agreement(con, "sun_exposure")
        check("agreement() runs and sees the disagreeing pair",
              rep["n_pairs"] == 1 and rep["n_agree"] == 0
              and rep["pairwise_agreement"] == 0.0, str(rep))
        con.close()


def test_archived_class_still_resolves():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        k = labels_db.instance_key("2026-07-03", "camera_01", "00000001.jpg", _BBOX, 0)
        labels_db.submit_annotation(con, instance_key=k, annotator="alice",
                                    outcome="labeled", choices=[_SHADED])
        labels_db.update_class(con, "sun_exposure.shaded", active=False, actor="pow")
        row = con.execute(
            "SELECT class_key, class_name FROM v_current_answers WHERE effective_key = ?", [k]
        ).fetchone()
        # This is the stated reason soft delete was chosen: the snapshotted
        # class_name keeps rendering what the annotator actually saw.
        check("answer still appears after its class is archived",
              row is not None and row[0] == "sun_exposure.shaded", str(row))
        check("...with its snapshotted class_name",
              row is not None and row[1] == "Shaded", str(row))
        con.close()


def test_skip_does_not_count_as_coverage():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        k = labels_db.instance_key("2026-07-03", "camera_01", "00000001.jpg", _BBOX, 0)
        labels_db.submit_annotation(con, instance_key=k, annotator="alice",
                                    outcome="skipped", telemetry={"skip_reason": "occluded"})
        row = con.execute(
            "SELECT n_annotators_labeled, n_annotators_skipped FROM v_instance_coverage "
            "WHERE effective_key = ?", [k]
        ).fetchone()
        # With target 1 the queue serves while n_annotators_labeled < target, so 0
        # labeled means still servable to a second annotator.
        check("one skip leaves n_annotators_labeled = 0 (still servable)",
              row is not None and int(row[0]) == 0, str(row))
        check("...and is counted separately as a skip",
              row is not None and int(row[1]) == 1, str(row))
        con.close()


def test_undo_supersedes_and_keeps_history():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        k = labels_db.instance_key("2026-07-03", "camera_01", "00000001.jpg", _BBOX, 0)
        aid = labels_db.submit_annotation(con, instance_key=k, annotator="alice",
                                          outcome="labeled", choices=[_SHADED])
        undone = labels_db.undo_last(con, "alice", k)
        check("undo returns the superseded annotation_id", undone == aid, f"{undone} vs {aid}")
        row = con.execute(
            "SELECT outcome, superseded_at FROM annotations WHERE annotation_id = ?", [aid]
        ).fetchone()
        check("the row is PRESENT with outcome='undone' and superseded_at set",
              row is not None and row[0] == "undone" and row[1] is not None, str(row))
        left = con.execute(
            "SELECT count(*) FROM v_current_answers WHERE effective_key = ?", [k]
        ).fetchone()[0]
        check("v_current_answers no longer returns it", left == 0, str(left))
        con.close()


def test_multiselect_dispatch():
    with tempfile.TemporaryDirectory() as d:
        con = _store(d)
        labels_db.create_group(con, group_key="tags", name="Tags",
                               description="anything visible in the crop",
                               multi_select=True, required=False, actor="pow")
        labels_db.create_class(con, "tags", name="Dog", class_key="tags.dog",
                               description="a farm dog is in the crop", actor="pow")
        labels_db.create_class(con, "tags", name="Mud", class_key="tags.mud",
                               description="the ground is visibly muddy", actor="pow")
        k = labels_db.instance_key("2026-07-03", "camera_01", "00000001.jpg", _BBOX, 0)
        labels_db.submit_annotation(
            con, instance_key=k, annotator="alice", outcome="labeled",
            choices=[{"class_key": "tags.dog", "group_key": "tags", "class_name": "Dog"},
                     {"class_key": "tags.mud", "group_key": "tags", "class_name": "Mud"}])
        labels_db.submit_annotation(
            con, instance_key=k, annotator="bob", outcome="labeled",
            choices=[{"class_key": "tags.dog", "group_key": "tags", "class_name": "Dog"}])

        rep = labels_db.agreement(con, "tags")
        check("agreement() dispatches a multi-select group to Jaccard",
              rep["metric"] == "jaccard", str(rep.get("metric")))
        check("Jaccard over {dog,mud} vs {dog} is 0.5",
              rep.get("jaccard") is not None and abs(rep["jaccard"] - 0.5) < 1e-9,
              str(rep.get("jaccard")))
        try:
            labels_db.agreement(con, "tags", metric="exact")
            check("forcing the exact-match query on multi-select raises", False, "no error")
        except ValueError:
            check("forcing the exact-match query on multi-select raises", True)
        try:
            labels_db.fleiss_kappa(con, "tags")
            check("fleiss_kappa refuses a multi-select group", False, "no error")
        except ValueError:
            check("fleiss_kappa refuses a multi-select group", True)
        con.close()


def main():
    print("=== test_labels_schema ===")
    test_key_is_path_and_platform_stable()
    test_key_quantisation_and_guards()
    test_python_and_sql_keys_agree()
    test_ordinal_is_dense_and_total()
    test_key_survives_clip_and_restore()
    test_a_false_positive_stays_removed_across_a_rekey()
    test_an_outline_correction_follows_its_instance()
    test_rekey_collides_with_nothing_when_one_annotator_has_rows_in_both_tables()
    test_mask_survives_clip_restore_on_a_deployed_shape_db()
    test_mask_columns_are_last_so_the_archive_stays_aligned()
    test_schema_is_idempotent_and_seeded()
    test_relabel_appends_and_double_submit_is_refused()
    test_archived_class_still_resolves()
    test_skip_does_not_count_as_coverage()
    test_undo_supersedes_and_keeps_history()
    test_multiselect_dispatch()
    print("==========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
