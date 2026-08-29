"""Weekly label-store backup tests: the due-gate, the bundle, the oversize
ladder, retention, redaction, and the contention/failure split.

Everything is seeded through the REAL store — labels_db.init_labels_db +
submit_annotation — never hand-written CREATE TABLEs: a backup test that invents
its own schema validates the export against a store that does not exist. The
Discord poster is the injected callable labels_backup designs for, so no test
touches the network.

No pytest. Run either way:
    .venv/bin/python -m tests.test_labels_backup
    .venv/bin/python tests/test_labels_backup.py
"""
from __future__ import annotations

import csv
import os
import sys
import tempfile
import zipfile
from pathlib import Path

import duckdb

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cownting import db, labels_backup, labels_db  # noqa: E402
from cownting.config import AuthCfg, BackupCfg, CameraCfg, Config, PathsCfg  # noqa: E402

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


# A syntactically valid webhook with a long, unmistakable token — the redaction
# tests grep for exactly this string in everything printed and stored.
_WEBHOOK = "https://discord.com/api/webhooks/1234567890/SuperSecretToken1234567890"
_TOKEN = "SuperSecretToken1234567890"

# `mask_edits.csv` is a member in its own right, not folded into annotations.csv:
# an instance can carry a geometry verdict and NO answer (a "not a cow" removal
# retires it before any question is asked), so a join would drop exactly the
# rows that have no other readable copy.
_MEMBERS = {"labels.duckdb", "annotations.csv", "mask_edits.csv", "taxonomy.json",
            "MANIFEST.json", "README.md"}

_SHADED = {"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
           "class_name": "Shaded"}
_STANDING = {"class_key": "behaviour.standing", "group_key": "behaviour",
             "class_name": "Standing"}


class _Recorder:
    """Injected poster: records calls, or raises like urllib would — with the
    full webhook URL embedded in the message, which is exactly what _redact()
    must scrub before anything is printed or stored."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str, Path | None]] = []
        self.fail = fail

    def __call__(self, webhook: str, content: str, file_path: Path | None) -> None:
        if self.fail:
            raise RuntimeError(f"HTTP Error 500: {webhook} rejected the upload")
        self.calls.append((webhook, content, file_path))


def _config(d: str, **backup_kwargs) -> Config:
    return Config(
        cameras=[CameraCfg(id="camera_01", video="unused.mp4")],
        auth=AuthCfg(enabled=False),
        backup=BackupCfg(**backup_kwargs),
        paths=PathsCfg(
            db_path=os.path.join(d, "cownting.duckdb"),
            labels_db_path=os.path.join(d, "labels.duckdb"),
            backups_dir=os.path.join(d, "backups"),
            count_areas=os.path.join(d, "areas.json"),
        ),
    )


def _seed(config: Config, *, labeled: int = 1, skipped: int = 0,
          annotator: str = "alice") -> tuple[duckdb.DuckDBPyConnection, list[int]]:
    """Annotations through the real write path. Returns (open connection, ids)."""
    con = db.connect(config.paths.labels_db_path)
    labels_db.init_labels_db(con)
    ids = []
    for i in range(labeled):
        key = labels_db.instance_key("2026-07-03", "camera_01",
                                     f"{i + 1:08d}.jpg", [10, 20, 110, 220], 0)
        ids.append(labels_db.submit_annotation(
            con, instance_key=key, annotator=annotator, outcome="labeled",
            choices=[_SHADED, _STANDING],
            provenance={"dataset_id": "2026-07-03", "camera_id": "camera_01",
                        "frame_basename": f"{i + 1:08d}.jpg",
                        "bbox_x1": 10.0, "bbox_y1": 20.0,
                        "bbox_x2": 110.0, "bbox_y2": 220.0}))
    for i in range(skipped):
        key = labels_db.instance_key("2026-07-03", "camera_01",
                                     f"{100 + i:08d}.jpg", [10, 20, 110, 220], 0)
        ids.append(labels_db.submit_annotation(
            con, instance_key=key, annotator=annotator, outcome="skipped",
            telemetry={"skip_reason": "occluded"}))
    return con, ids


def _zips(config: Config) -> list[Path]:
    out = Path(config.paths.backups_dir) / "labels"
    return sorted(out.glob("labels-*.zip")) if out.exists() else []


def test_gate_and_bundle():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        # The cookie signing key lives two directories above the staging dir; a
        # glob-based bundle would sweep it into a zip posted to a channel.
        Path(d, ".session_secret").write_text("cookie-signing-key")

        con = db.connect(config.paths.labels_db_path)
        labels_db.init_labels_db(con)
        is_due, reason = labels_backup.due(con, config.backup)
        check("an empty store is not due", not is_due, reason)
        check("...and says why", "empty" in reason, reason)
        con.close()
        rec = _Recorder()
        r = labels_backup.run_backup(config, trigger="cli", poster=rec)
        check("run on an empty store is skipped, nothing posted",
              r["status"] == "skipped" and not rec.calls, str(r))

        con, _ = _seed(config, labeled=1)
        con.close()
        r1 = labels_backup.run_backup(config, trigger="cli", poster=rec)
        check("first run with data fires", r1["status"] == "done", str(r1))
        check("...and posts exactly once", len(rec.calls) == 1 and r1["discord"] == "posted",
              f"{len(rec.calls)} posts, discord={r1['discord']}")
        check("the zip exists on disk", r1["zip_path"] and os.path.exists(r1["zip_path"]),
              str(r1["zip_path"]))
        with zipfile.ZipFile(r1["zip_path"]) as z:
            names = set(z.namelist())
        check("zip members are exactly the bundle files",
              names == _MEMBERS, str(sorted(names)))
        check("nothing resembling .session_secret is swept in",
              all("session_secret" not in n for n in names), str(sorted(names)))

        r2 = labels_backup.run_backup(config, trigger="cli", poster=rec)
        check("a second run with nothing new is skipped",
              r2["status"] == "skipped", str(r2))
        check("...because nothing landed since the watermark",
              "nothing new" in (r2["reason"] or ""), str(r2["reason"]))
        check("no second post happened", len(rec.calls) == 1, str(len(rec.calls)))


def test_csv_contains_the_answers():
    """The check that would have caught the export writing a weekly 'backup'
    containing no labels: every labeled annotation must surface in the CSV with
    a class_key, and skips must appear with their skip_reason."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con, _ = _seed(config, labeled=2, skipped=1)
        labeled_ids = {int(r[0]) for r in con.execute(
            "SELECT annotation_id FROM annotations WHERE outcome = 'labeled'").fetchall()}
        skipped_ids = {int(r[0]) for r in con.execute(
            "SELECT annotation_id FROM annotations WHERE outcome = 'skipped'").fetchall()}
        out = os.path.join(d, "annotations.csv")
        n = labels_backup.export_csv(con, out)
        con.close()
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        check("export_csv reports the rows it wrote", n == len(rows),
              f"reported {n}, file has {len(rows)}")
        # 2 labeled x 2 choices + 1 choice-less skip = 5 long-format rows.
        check("long format: one row per (annotation, choice), skips included",
              len(rows) == 5, str(len(rows)))
        for aid in labeled_ids:
            with_class = [r for r in rows
                          if r["annotation_id"] == str(aid) and r["class_key"]]
            check(f"labeled annotation {aid} has a non-null class_key row",
                  len(with_class) >= 1, str(len(with_class)))
        skip_rows = [r for r in rows if r["outcome"] == "skipped"]
        check("the skip appears as a row",
              {int(r["annotation_id"]) for r in skip_rows} == skipped_ids, str(skip_rows))
        check("...carrying its skip_reason",
              all(r["skip_reason"] == "occluded" for r in skip_rows),
              str([r["skip_reason"] for r in skip_rows]))


def test_snapshot_is_a_real_db():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con, _ = _seed(config, labeled=2, skipped=1)
        labels_db.set_meta(con, labels_backup.META_WATERMARK, "2026-08-01T00:00:00")
        snap = os.path.join(d, "snap.duckdb")
        # `con` stays OPEN read-write across the snapshot — the live-writer case
        # shutil.copy gets wrong (torn .wal) and COPY FROM DATABASE must survive.
        labels_backup.snapshot_db(config.paths.labels_db_path, snap)
        check("snapshot succeeds with a live read-write connection attached",
              os.path.exists(snap))
        con.close()
        sc = duckdb.connect(snap)
        n_ann = sc.execute("SELECT count(*) FROM annotations").fetchone()[0]
        n_grp = sc.execute("SELECT count(*) FROM label_groups").fetchone()[0]
        n_cls = sc.execute("SELECT count(*) FROM label_classes").fetchone()[0]
        check("the snapshot opens and is complete (annotations)", n_ann == 3, str(n_ann))
        check("the snapshot carries the whole taxonomy",
              n_grp == 2 and n_cls == 10, f"{n_grp} groups, {n_cls} classes")
        n_ans = sc.execute("SELECT count(*) FROM v_current_answers").fetchone()[0]
        check("views travel too (v_current_answers works)", n_ans == 4, str(n_ans))
        wm = labels_db.get_meta(sc, labels_backup.META_WATERMARK)
        check("the watermark travels inside the snapshot",
              wm == "2026-08-01T00:00:00", str(wm))
        sc.close()


def test_oversize_ladder_and_retention():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d, keep=2)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=2, skipped=1)
        con.close()

        # Run 1: generous budget -> the full zip posts.
        rec1 = _Recorder()
        r1 = labels_backup.run_backup(config, trigger="cli", poster=rec1)
        check("full zip fits the default budget -> posted",
              r1["status"] == "done" and r1["discord"] == "posted", str(r1))
        check("run 1 sanity: the zip is big enough to squeeze", r1["zip_bytes"] > 1000,
              str(r1["zip_bytes"]))

        # Run 2: budget just under the full zip (the .duckdb is the bulky
        # page-aligned part; the CSV bundle stays far smaller) -> CSV-only rung.
        config.backup.max_upload_bytes = int(r1["zip_bytes"]) - 200
        rec2 = _Recorder()
        r2 = labels_backup.run_backup(config, trigger="cli", force=True, poster=rec2)
        check("oversize falls back to CSV-only rather than failing",
              r2["status"] == "done" and r2["discord"] == "posted_csv_only", str(r2))
        check("the posted file is the CSV bundle",
              len(rec2.calls) == 1 and rec2.calls[0][2] is not None
              and rec2.calls[0][2].name.endswith("-csv.zip"),
              str(rec2.calls and rec2.calls[0][2]))
        check("the FULL zip is still retained on disk",
              r2["zip_path"] and os.path.exists(r2["zip_path"]), str(r2["zip_path"]))

        # Run 3: nothing fits -> a summary message, file=None, still done.
        config.backup.max_upload_bytes = 1
        rec3 = _Recorder()
        r3 = labels_backup.run_backup(config, trigger="cli", force=True, poster=rec3)
        check("even CSV-only oversize posts a summary, never fails",
              r3["status"] == "done" and r3["discord"] == "posted_summary", str(r3))
        check("the summary post carries no file",
              len(rec3.calls) == 1 and rec3.calls[0][2] is None, str(rec3.calls))

        modes = [r[0] for r in db.connect(config.paths.labels_db_path).execute(
            "SELECT discord FROM backup_runs WHERE status = 'done' ORDER BY run_id"
        ).fetchall()]
        check("the delivery mode of every rung is recorded in backup_runs",
              modes == ["posted", "posted_csv_only", "posted_summary"], str(modes))

        # Retention: keep=2 prunes by NAME, so the two newest zips survive.
        left = [p.name for p in _zips(config)]
        expect = {Path(r2["zip_path"]).name, Path(r3["zip_path"]).name}
        check("retention keeps exactly N zips, by name",
              set(left) == expect and len(left) == 2, f"left={left}")


def test_failure_holds_the_watermark_and_redacts():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=1)
        con.close()
        r1 = labels_backup.run_backup(config, trigger="cli", poster=_Recorder())
        check("baseline run succeeds", r1["status"] == "done", str(r1))
        con = db.connect(config.paths.labels_db_path)
        w1 = labels_db.get_meta(con, labels_backup.META_WATERMARK)
        check("baseline advanced the watermark", w1 is not None, str(w1))
        # Something new lands, then Discord goes down.
        labels_db.submit_annotation(
            con, instance_key=labels_db.instance_key(
                "2026-07-03", "camera_01", "00000099.jpg", [10, 20, 110, 220], 0),
            annotator="bob", outcome="labeled", choices=[_SHADED])
        con.close()

        r2 = labels_backup.run_backup(config, trigger="cli", force=True,
                                      poster=_Recorder(fail=True))
        check("a Discord outage is a genuine failure", r2["status"] == "failed", str(r2))
        err = r2["error"] or ""
        check("the returned error is redacted (no URL, no token)",
              _WEBHOOK not in err and _TOKEN not in err, err)
        check("...and shows the placeholder instead", "<discord-webhook>" in err, err)

        con = db.connect(config.paths.labels_db_path)
        stored = con.execute(
            "SELECT error FROM backup_runs WHERE status = 'failed' "
            "ORDER BY run_id DESC LIMIT 1").fetchone()
        check("the STORED error (it travels inside the next zip) is redacted too",
              stored is not None and stored[0]
              and _WEBHOOK not in stored[0] and _TOKEN not in stored[0], str(stored))
        w2 = labels_db.get_meta(con, labels_backup.META_WATERMARK)
        check("the failed run held the watermark (nothing silently dropped)",
              w2 == w1, f"{w1} -> {w2}")
        con.close()


def test_contention_is_skipped_not_failed():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=1)
        # Another runner holds a fresh claim.
        con.execute(
            'INSERT INTO backup_runs (run_id, started_at, status, "trigger", holder) '
            "VALUES (nextval('seq_backup_run'), now(), 'running', 'cli', 'elsewhere:999')")
        con.close()
        rec = _Recorder()
        r = labels_backup.run_backup(config, trigger="cli", force=True, poster=rec)
        check("a refused claim comes back skipped, not failed",
              r["status"] == "skipped", str(r))
        check("...with the frozen contention reason", r["reason"] == "store busy",
              str(r["reason"]))
        check("nothing was posted", not rec.calls)
        con = db.connect(config.paths.labels_db_path)
        rows = [r0[0] for r0 in con.execute(
            "SELECT status FROM backup_runs ORDER BY run_id").fetchall()]
        # No failed row means no 6-hour cooldown is armed — the split that keeps
        # a nightly cron from permanently disabling the weekly schedule.
        check("no row was written: only the rival's claim exists",
              rows == ["running"], str(rows))
        con.close()


def test_unset_webhook_is_a_clean_noop():
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        os.environ.pop(labels_backup.WEBHOOK_ENV, None)
        con, _ = _seed(config, labeled=1)
        last_ann = con.execute("SELECT max(submitted_at) FROM annotations").fetchone()[0]
        con.close()
        rec = _Recorder()
        r = labels_backup.run_backup(config, trigger="cli", poster=rec)
        check("unset webhook: the run still succeeds", r["status"] == "done", str(r))
        check("...recorded as discord='skipped'", r["discord"] == "skipped", str(r["discord"]))
        check("...and the poster was never called", not rec.calls)
        con = db.connect(config.paths.labels_db_path)
        wm = labels_db.get_meta(con, labels_backup.META_WATERMARK)
        con.close()
        check("the watermark DOES advance (re-running would change nothing)",
              wm == str(last_ann), f"{wm} vs {last_ann}")

        # A URL that is not a Discord webhook is refused without being posted to.
        os.environ[labels_backup.WEBHOOK_ENV] = "https://example.com/api/webhooks/1/leak"
        rec2 = _Recorder()
        r2 = labels_backup.run_backup(config, trigger="cli", force=True, poster=rec2)
        check("a non-Discord URL is refused", r2["discord"] == "refused", str(r2["discord"]))
        check("...the run still completes and the poster never sees it",
              r2["status"] == "done" and not rec2.calls, str(r2))
        os.environ.pop(labels_backup.WEBHOOK_ENV, None)


def main():
    print("=== test_labels_backup ===")
    test_gate_and_bundle()
    test_csv_contains_the_answers()
    test_snapshot_is_a_real_db()
    test_oversize_ladder_and_retention()
    test_failure_holds_the_watermark_and_redacts()
    test_contention_is_skipped_not_failed()
    test_unset_webhook_is_a_clean_noop()
    print("==========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
