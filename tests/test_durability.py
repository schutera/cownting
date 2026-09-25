"""Two invariants nothing else pins, both of which fail silently.

1. THE `instance_key` RECIPE IS FROZEN.

   Every annotation and every mask edit in `labels.duckdb` is filed under an
   `instance_key` — a hash of (dataset, camera, frame basename, quantised bbox,
   ordinal). Annotator hours are the one thing in this system a re-ingest cannot
   rebuild, and the key is the only thread connecting them to a cow.

   `test_labels_schema.py` pins the key RELATIVELY — stable across path
   spellings, stable under sub-pixel jitter, identical between the Python and
   SQL producers. All of that stays true if someone edits `KEY_VERSION` or
   `_SEP`, because BOTH producers read the same constants and move in lockstep.
   Verified: mutating them leaves that module fully green while every stored
   annotation now points at a key nothing produces.

   So: one fixed digest, asserted through both producers. Changing the recipe is
   a legitimate thing to want; doing it without noticing is not. When this fails,
   the author must either revert or ship a migration — which is the point.

2. `is_transient_lock_error` IS THE WHOLE CONCURRENCY STRATEGY.

   DuckDB allows one writer per file, and this app opens a short-lived
   connection per request. When two coincide, DuckDB refuses the second with a
   message that differs per platform and per operation. `db.connect` retries for
   ~9 s on the strength of this ONE predicate; `labeling.ensure_labels_db`
   retries its DDL on it; `labels_backup` classifies contention with it.

   It was named in no test at all. `test_db_concurrency.py` runs 30 threads and
   asserts no errors — but it never provokes a collision, so it passes with the
   retry logic entirely disabled (proven: stubbing the predicate to `False`
   leaves that module green). A DuckDB release that rewords "Unique file handle
   conflict" would turn every dashboard query back into a 500, and nothing would
   go red.

No pytest. Run either way:
    py -3.11 -m tests.test_durability
    py -3.11 tests/test_durability.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import threading

import duckdb

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, labels_db  # noqa: E402

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


# --------------------------------------------------------------------------- 1
#
# The v1 recipe, frozen. Produced by the shipped code and checked in on
# 2026-09-25, at which point the production label store held ~3,000 annotations
# filed under keys from this recipe. If you change KEY_VERSION, _SEP, the
# quantisation, or the field order, this digest changes — and so must every
# stored key, via `labels_db.rekey_*`.
GOLDEN = [
    # (dataset_id, camera_id, frame_basename, bbox, ordinal, expected key)
    ("2026-07-03", "camera_01", "00000450.jpg",
     [10.0, 20.0, 110.0, 220.0], 0, "ce9dfb582cc963e0ae99de54ae497a39"),
]


def test_the_instance_key_recipe_is_frozen():
    for ds, cam, frame, bbox, ordinal, want in GOLDEN:
        got = labels_db.instance_key(ds, cam, frame, bbox, ordinal)
        check(f"python: {ds}/{cam}/{frame}#{ordinal} -> {want}", got == want,
              f"got {got} — the key recipe CHANGED; every stored annotation now "
              f"points at a key nothing produces")


def test_the_sql_producer_agrees_with_the_frozen_recipe():
    """Both producers read the same KEY_VERSION, so parity alone cannot catch a
    version bump — but a divergence between them is its own catastrophe, and
    asserting both against the SAME fixed digest catches either."""
    con = duckdb.connect(":memory:")
    try:
        db.init_db(con)
        for ds, cam, frame, bbox, ordinal, want in GOLDEN:
            con.execute("DELETE FROM detections")
            # `instance_key_sql` derives the ordinal from a window over the real
            # detections shape, so a hand-made two-column table cannot exercise
            # it — the key must come off the table the queue actually scans.
            con.execute(
                "INSERT INTO detections (dataset_id, camera_id, ts, frame_path, score, "
                "  bbox_x1, bbox_y1, bbox_x2, bbox_y2, area_px, ground_px_x, ground_px_y) "
                "VALUES (?, ?, CAST('2026-07-03 09:00:00' AS TIMESTAMP), ?, 0.9, "
                "        ?, ?, ?, ?, 10000, 60, 220)",
                [ds, cam, f"data/artifacts/{ds}/frames/{cam}/{frame}",
                 bbox[0], bbox[1], bbox[2], bbox[3]])
            row = con.execute(
                f"SELECT {labels_db.instance_ordinal_sql('d')} AS ordinal, "
                f"       {labels_db.instance_key_sql('d')} AS k FROM detections d"
            ).fetchone()
            check(f"sql ordinal for the single row is {ordinal}", row[0] == ordinal,
                  str(row[0]))
            check(f"sql: {ds}/{cam}/{frame}#{ordinal} -> {want}", row[1] == want,
                  f"got {row[1]}")
    finally:
        con.close()


def test_the_key_version_constant_is_the_one_the_digest_was_taken_under():
    """A guard on the guard: if the digests above were ever regenerated to match
    a changed recipe rather than the recipe reverted, this says so out loud."""
    check("KEY_VERSION is still 'v1'", labels_db.KEY_VERSION == "v1",
          "" if labels_db.KEY_VERSION == "v1" else
          f"now {labels_db.KEY_VERSION!r} — if this bump is intended, the GOLDEN "
          f"digests must be regenerated AND every stored key migrated")


# --------------------------------------------------------------------------- 2

# The real message texts, per platform and per operation. Taken from the
# predicate's own docstring and from errors observed in production.
TRANSIENT = [
    ('IO Error: Unique file handle conflict: Database "data/cownting.duckdb" is '
     'already attached by another connection', "both platforms, connect()"),
    ('IO Error: Could not set lock on file "/app/data/labels.duckdb": '
     'Conflicting lock is held', "POSIX, connect()"),
    ("IO Error: Cannot open file \"data\\cownting.duckdb\": The process cannot "
     "access the file because it is being used by another process",
     "Windows sharing violation"),
    ('TransactionContext Error: Catalog write-write conflict on create with "v_current_answers"',
     "two writers replaying the same CREATE OR REPLACE"),
]

# Must NOT be retried: retrying these wastes 9 s and then reports the wrong
# cause, and the first two are the errors an operator most needs to see plainly.
PERMANENT = [
    ('IO Error: Cannot open file "data/cownting.duckdb": No such file or directory',
     "a missing file"),
    ("IO Error: Cannot open file: No space left on device", "a full disk"),
    ("Binder Error: Table \"detections\" does not have a column with name \"mask_poly\"",
     "a schema mistake"),
    ("Constraint Error: Duplicate key \"alice\" violates primary key constraint",
     "a genuine constraint violation"),
]


def test_the_transient_lock_predicate_recognises_every_real_wording():
    for msg, why in TRANSIENT:
        check(f"retried: {why}", db.is_transient_lock_error(Exception(msg)), msg[:90])


def test_it_does_not_swallow_a_permanent_failure():
    """The retry loop is ~9 s. Classifying a missing file or a full disk as
    transient turns a clear error into a slow one with a misleading message."""
    for msg, why in PERMANENT:
        check(f"NOT retried: {why}", not db.is_transient_lock_error(Exception(msg)),
              msg[:90])


def test_connect_actually_retries_rather_than_raising():
    """`test_db_concurrency` runs 30 threads and asserts no errors — but it never
    provokes a collision on a modern DuckDB, so it passes with the retry logic
    stubbed out entirely. This forces the retry path: a fake that refuses with a
    real lock message a few times and then succeeds."""
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "t.duckdb")
        duckdb.connect(path).close()
        real, state = duckdb.connect, {"n": 0}

        def flaky(*args, **kwargs):
            state["n"] += 1
            if state["n"] <= 3:
                raise duckdb.IOException(
                    'IO Error: Unique file handle conflict: Database "t.duckdb" '
                    "is already attached by another connection")
            return real(*args, **kwargs)

        duckdb.connect = flaky
        try:
            con = db.connect(path)
            raised = ""
        except Exception as e:  # noqa: BLE001
            con, raised = None, f"{type(e).__name__}: {e}"
        finally:
            duckdb.connect = real
        check("connect() rides out a transient conflict", not raised, raised)
        check("...having actually retried", state["n"] == 4, f"{state['n']} attempts")
        if con is not None:
            con.close()

        # ...and a PERMANENT error must come straight back, not after 9 seconds.
        def broken(*args, **kwargs):
            raise duckdb.IOException("IO Error: No space left on device")

        duckdb.connect = broken
        try:
            db.connect(path)
            raised2 = ""
        except Exception as e:  # noqa: BLE001
            raised2 = str(e)
        finally:
            duckdb.connect = real
        check("...and a permanent error is raised immediately",
              "No space left" in raised2, raised2[:90])
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_a_real_concurrent_open_succeeds():
    """The end-to-end shape `test_db_concurrency` was reaching for, but with a
    WRITER held open throughout so the readers have something to collide with."""
    d = tempfile.mkdtemp()
    try:
        path = os.path.join(d, "t.duckdb")
        writer = db.connect(path)
        db.init_db(writer)
        errors: list[str] = []

        def reader():
            try:
                c = db.connect(path)
                c.execute("SELECT count(*) FROM frames").fetchone()
                c.close()
            except Exception as e:  # noqa: BLE001
                errors.append(f"{type(e).__name__}: {e}")

        threads = [threading.Thread(target=reader) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        writer.close()
        check("20 readers against a held writer all get through",
              not errors, f"{len(errors)} failed; first: {errors[:1]}")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main():
    test_the_instance_key_recipe_is_frozen()
    test_the_sql_producer_agrees_with_the_frozen_recipe()
    test_the_key_version_constant_is_the_one_the_digest_was_taken_under()
    test_the_transient_lock_predicate_recognises_every_real_wording()
    test_it_does_not_swallow_a_permanent_failure()
    test_connect_actually_retries_rather_than_raising()
    test_a_real_concurrent_open_succeeds()
    print(f"\n{'ALL OK' if not _FAILED else f'{_FAILED} FAILED'}")
    sys.exit(1 if _FAILED else 0)


if __name__ == "__main__":
    main()
