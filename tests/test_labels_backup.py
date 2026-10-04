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
import io
import json
import contextlib
import os
import sys
import tempfile
import urllib.error
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
_MEMBERS = {"labels.duckdb", "annotations.csv", "mask_edits.csv", "point_edits.csv",
            "taxonomy.json", "MANIFEST.json", "README.md"}

_SHADED = {"class_key": "sun_exposure.shaded", "group_key": "sun_exposure",
           "class_name": "Shaded"}
_STANDING = {"class_key": "behaviour.standing", "group_key": "behaviour",
             "class_name": "Standing"}


class _Recorder:
    """Injected poster: records calls, or raises like urllib would — with the
    full webhook URL embedded in the message, which is exactly what _redact()
    must scrub before anything is printed or stored."""

    def __init__(self, fail: bool = False, oversize: int | None = None,
                 discord_code: int | None = None):
        self.calls: list[tuple[str, str, Path | None]] = []
        self.fail = fail
        # `oversize`: refuse any post CARRYING A FILE with this HTTP status, the
        # way Discord refuses an attachment our own budget thought was fine.
        # 413, or 400 with body {"code": 40005}. Posts with no file always
        # succeed — that is what makes the summary rung the guaranteed floor.
        self.oversize = oversize
        self.discord_code = discord_code

    def __call__(self, webhook: str, content: str, file_path: Path | None) -> None:
        if self.fail:
            raise RuntimeError(f"HTTP Error 500: {webhook} rejected the upload")
        if self.oversize is not None and file_path is not None:
            body = json.dumps({"code": self.discord_code}).encode() if self.discord_code else b"{}"
            raise urllib.error.HTTPError(
                webhook, self.oversize, "Payload Too Large", {},  # type: ignore[arg-type]
                io.BytesIO(body))
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


def test_a_day_of_only_point_work_is_backed_up():
    """M6 §4.4. The body-points step writes to `point_edits` before any question
    is answered, so a labeler who only placed points has written nothing to
    `annotations` or `mask_edits`. The gate must still see that work, and the
    bundle must carry it in a readable CSV."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con = db.connect(config.paths.labels_db_path)
        labels_db.init_labels_db(con)
        key = labels_db.instance_key("2026-07-03", "camera_01", "00000001.jpg",
                                     [10, 20, 110, 220], 0)
        pts = [[100.0, 60.0], [60.0, 30.0], [15.0, 40.0], [70.0, 215.0], None]
        labels_db.submit_point_edit(
            con, instance_key=key, annotator="alice", points=pts,
            provenance={"dataset_id": "2026-07-03", "camera_id": "camera_01",
                        "frame_basename": "00000001.jpg", "bbox_x1": 10.0,
                        "bbox_y1": 20.0, "bbox_x2": 110.0, "bbox_y2": 220.0})
        labels_db.submit_point_edit(con, instance_key=key, annotator="bob",
                                    kind="too_small")
        is_due, reason = labels_backup.due(con, config.backup)
        check("a store holding only point work is due", is_due, reason)

        out = os.path.join(d, "point_edits.csv")
        n = labels_backup.export_point_csv(con, out)
        con.close()
        with open(out, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        check("export_point_csv reports the rows it wrote", n == len(rows) == 2,
              f"reported {n}, file has {len(rows)}")
        by = {r["annotator"]: r for r in rows}
        check("the points survive as the stored JSON",
              json.loads(by["alice"]["points"]) == pts, by["alice"]["points"])
        check("...with their count and the day they belong to",
              by["alice"]["n_placed"] == "4" and by["alice"]["dataset_id"] == "2026-07-03",
              str(by["alice"]))
        check("the too_small verdict is a row with no points",
              by["bob"]["kind"] == "too_small" and by["bob"]["points"] == "",
              str(by["bob"]))

        rec = _Recorder()
        r = labels_backup.run_backup(config, trigger="cli", poster=rec)
        check("the run fires on point work alone", r["status"] == "done", str(r))
        with zipfile.ZipFile(r["zip_path"]) as z:
            names = set(z.namelist())
            readme = z.read("README.md").decode("utf-8")
        check("point_edits.csv is in the bundle", "point_edits.csv" in names,
              str(sorted(names)))
        check("...and the README names it", "point_edits.csv" in readme)


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
              n_grp == 2 and n_cls == 11, f"{n_grp} groups, {n_cls} classes")
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


def test_a_failed_delivery_still_completes_the_backup():
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

        # DELIVERY IS NOT THE BACKUP. This assertion is the inverse of what it
        # used to be. Posting used to sit inside the try that marks a run
        # failed, so a rejected upload discarded a complete, valid zip: the
        # watermark was held, a 6-hour cooldown armed, rotation skipped. One
        # broken webhook meant no backup ever completed again while zips piled
        # up unrotated. The zip on disk IS the backup; Discord is a copy.
        r2 = labels_backup.run_backup(config, trigger="cli", force=True,
                                      poster=_Recorder(fail=True))
        check("a Discord outage does NOT fail the backup", r2["status"] == "done", str(r2))
        check("...and is recorded as a delivery failure", r2["discord"] == "failed", str(r2))
        check("...with the zip actually on disk",
              r2["zip_path"] and Path(r2["zip_path"]).is_file(), str(r2.get("zip_path")))
        err = r2.get("error") or ""
        check("the returned error is redacted (no URL, no token)",
              _WEBHOOK not in err and _TOKEN not in err, err)
        check("...and shows the placeholder instead", "<discord-webhook>" in err, err)

        con = db.connect(config.paths.labels_db_path)
        stored = con.execute(
            "SELECT error, discord FROM backup_runs ORDER BY run_id DESC LIMIT 1").fetchone()
        check("the STORED error (it travels inside the next zip) is redacted too",
              stored is not None and stored[0]
              and _WEBHOOK not in stored[0] and _TOKEN not in stored[0], str(stored))
        check("...and history can tell 'rejected' from 'never attempted'",
              stored is not None and stored[1] == "failed", str(stored))
        w2 = labels_db.get_meta(con, labels_backup.META_WATERMARK)
        check("a delivered-nowhere backup still ADVANCES the watermark "
              "(the data is safely in the zip)", w2 != w1, f"{w1} -> {w2}")
        check("...and does not arm the cooldown, so the weekly run is not wedged",
              "cooling down" not in labels_backup.due(con, config.backup)[1],
              str(labels_backup.due(con, config.backup)))
        con.close()


def test_a_genuine_failure_still_fails():
    """The other half of the split: if no backup EXISTS, the run must fail, hold
    the watermark and arm the cooldown. Making a delivery failure benign must not
    make a real one benign too."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=1)
        con.close()
        r1 = labels_backup.run_backup(config, trigger="cli", poster=_Recorder())
        check("baseline run succeeds", r1["status"] == "done", str(r1))
        con = db.connect(config.paths.labels_db_path)
        w1 = labels_db.get_meta(con, labels_backup.META_WATERMARK)
        con.close()

        # Something new must be waiting, or the due-gate answers "nothing new
        # since the last backup" and never reaches the cooldown clause at all.
        con = db.connect(config.paths.labels_db_path)
        labels_db.submit_annotation(
            con, instance_key=labels_db.instance_key(
                "2026-07-03", "camera_01", "00000077.jpg", [10, 20, 110, 220], 0),
            annotator="bob", outcome="labeled", choices=[_SHADED])
        con.close()

        # Break the zip step — i.e. the backup itself, not its delivery.
        real = labels_backup._write_zip
        labels_backup._write_zip = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        try:
            r2 = labels_backup.run_backup(config, trigger="cli", force=True,
                                          poster=_Recorder())
        finally:
            labels_backup._write_zip = real
        check("a real failure is still a failure", r2["status"] == "failed", str(r2))
        con = db.connect(config.paths.labels_db_path)
        check("...and holds the watermark",
              labels_db.get_meta(con, labels_backup.META_WATERMARK) == w1)
        check("...and arms the cooldown so it does not spin",
              "cooling down" in labels_backup.due(con, config.backup)[1],
              str(labels_backup.due(con, config.backup)))
        con.close()


def test_discord_refusing_an_attachment_walks_the_ladder():
    """Our budget is a GUESS about a limit that varies by guild and endpoint.
    When the guess is wrong the POST comes back 413 (or 400 + code 40005) — that
    must drop a rung, not fail the run. The last rung carries no file, so it
    cannot be refused for size."""
    for status, code, label in ((413, None, "413"), (400, 40005, "400 + code 40005")):
        with tempfile.TemporaryDirectory() as d:
            config = _config(d)
            os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
            con, _ = _seed(config, labeled=2)
            con.close()
            rec = _Recorder(oversize=status, discord_code=code)
            r = labels_backup.run_backup(config, trigger="cli", poster=rec)
            check(f"[{label}] the run completes", r["status"] == "done", str(r))
            check(f"[{label}] it lands on the summary rung",
                  r["discord"] == "posted_summary", str(r["discord"]))
            check(f"[{label}] the file rungs were tried first, then abandoned",
                  len(rec.calls) == 1 and rec.calls[0][2] is None,
                  f"{len(rec.calls)} recorded calls")
            body = rec.calls[0][1]
            check(f"[{label}] the message names the size and the retained path",
                  "MB" in body and str(r["zip_path"]) in body, body[:160])


def test_retention_still_runs_when_delivery_fails():
    """Rotation used to be unreachable after a delivery failure, so a broken
    webhook also meant zips accumulated forever."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d, keep=2)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=1)
        con.close()
        for _ in range(4):
            labels_backup.run_backup(config, trigger="cli", force=True,
                                     poster=_Recorder(fail=True))
        zips = sorted(Path(config.paths.backups_dir, "labels").glob("labels-*.zip"))
        check("keep=2 is honoured even though every delivery failed",
              len(zips) == 2, f"{len(zips)} zips: {[z.name for z in zips]}")


def test_the_upload_budget_sits_below_discords_floor():
    """8 MiB is the cap on an unboosted guild. A budget above it lets a zip pass
    our own ladder and get refused by the server instead."""
    check("default max_upload_bytes <= 8 MiB",
          BackupCfg().max_upload_bytes <= 8 * 1024 * 1024,
          str(BackupCfg().max_upload_bytes))


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


def test_a_forced_run_does_not_consume_the_scheduled_slot():
    """A --force run is an EXTRA copy, not a substitute.

    `due()` counted any `status='done'` row inside the window, so an operator who
    ran `labels backup --force` on Monday to check the pipeline silently spent
    that week's slot: the scheduler then reported "already backed up within the
    last 7 days" all week and the annotators' work waited until the following
    Monday to leave the box. This actually happened, and looked from the outside
    like a broken backup rather than a consumed one."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=2)
        con.close()

        res = labels_backup.run_backup(config, trigger="forced", force=True,
                                       poster=_Recorder())
        check("the forced run itself succeeds", res["status"] == "done", str(res))

        # A week of annotator work follows — otherwise `due()` short-circuits on
        # the watermark ("nothing new") and the weekly slot is never consulted,
        # which would make this test pass for the wrong reason.
        con, _ = _seed(config, labeled=3, annotator="bob")
        con.close()

        lcon = db.connect(config.paths.labels_db_path)
        try:
            ok, why = labels_backup.due(lcon, config.backup)
            check("new work after a FORCED run is still due", ok, why)
        finally:
            lcon.close()

        # ...whereas a genuinely SCHEDULED run does hold the slot, or the weekly
        # gate would mean nothing at all.
        labels_backup.run_backup(config, trigger="schedule", force=True,
                                 poster=_Recorder())
        con, _ = _seed(config, labeled=3, annotator="carol")
        con.close()
        lcon = db.connect(config.paths.labels_db_path)
        try:
            ok2, why2 = labels_backup.due(lcon, config.backup)
            check("the same new work after a SCHEDULED run is not", not ok2, why2)
            check("...and the reason given is the weekly slot",
                  "already backed up" in why2, why2)
        finally:
            lcon.close()


def test_a_store_that_has_never_succeeded_raises_the_alarm():
    """The staleness alarm is documented as what closes the loop when the gate
    itself is wedged. It read `max(finished_at) FROM backup_runs WHERE status =
    'done'` and returned silently on NULL — so it was dead in exactly the case
    where the backup had never once worked: every tick failing, no `done` row
    ever written, and no alarm, forever. A deployment that has never produced a
    backup is the most alarming state there is."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d, every_days=7)
        con, _ = _seed(config, labeled=1)
        # One failed attempt, 200 days ago, and nothing else.
        labels_backup.ensure_backup_tables(con)
        con.execute(
            "INSERT INTO backup_runs (run_id, \"trigger\", status, started_at, finished_at, error) "
            "VALUES (1, 'schedule', 'failed', now() - to_days(200), now() - to_days(200), 'disk full')")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            labels_backup._stale_alert(con, config.backup)
        out = buf.getvalue()
        con.close()
        check("the alarm fires when no backup has ever succeeded",
              "stale:" in out, repr(out))
        check("...and says so, rather than naming a date that does not exist",
              "NO backup has ever succeeded" in out, repr(out))
        check("...and is a line alerts/watch.py forwards",
              "[cownting.alert] LABEL-BACKUP" in out, repr(out))


def test_a_rung_that_fails_to_build_falls_through_to_the_summary():
    """Only a SIZE rejection used to drop a rung. But building the csv rung
    writes a second zip, and a full disk raises OSError there — which aborted the
    ladder before the summary rung, the one that carries no file and therefore
    cannot be rejected at all. The operator was told nothing whatsoever, although
    a complete full zip was sitting on the box."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d, max_upload_bytes=1)     # force past the 'full' rung
        os.environ[labels_backup.WEBHOOK_ENV] = _WEBHOOK
        con, _ = _seed(config, labeled=2)
        con.close()

        real_write = labels_backup._write_zip
        calls = {"n": 0}

        def _explode(path, members, *a, **kw):
            # The first call builds the full zip; the second is the csv rung.
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OSError(28, "No space left on device")
            return real_write(path, members, *a, **kw)

        rec = _Recorder()
        labels_backup._write_zip = _explode
        try:
            res = labels_backup.run_backup(config, trigger="cli", force=True, poster=rec)
        finally:
            labels_backup._write_zip = real_write

        check("the run still completes", res["status"] == "done", str(res))
        check("the summary rung was reached and delivered",
              res["discord"] == "posted_summary", str(res["discord"]))
        check("...carrying no attachment, which is why it cannot be rejected",
              bool(rec.calls) and rec.calls[-1][2] is None, str(rec.calls[-1:]))
        check("...and the full zip is still on the box",
              len(_zips(config)) == 1, str(_zips(config)))


def test_a_racing_ddl_is_contention_not_a_failure():
    """`_open_store` classified only `db.connect`. The DDL replay sat OUTSIDE the
    try — and `init_labels_db` ends in `CREATE OR REPLACE VIEW`, a catalog write
    that `labeling.ensure_labels_db` wraps in a 50-attempt retry loop precisely
    because it raced in production.

    So a backup whose DDL collided with a Label-page request was recorded as a
    genuine FAILURE: a spurious `[cownting.alert] LABEL-BACKUP failed` line, a
    non-zero CLI exit, and a 6 h cooldown armed against a perfectly healthy
    store. Contention must read as `skipped`; only a real error may fail."""
    with tempfile.TemporaryDirectory() as d:
        config = _config(d)
        con, _ = _seed(config, labeled=1)
        con.close()

        real = labels_backup.labels_db.init_labels_db

        def racing(c):
            raise Exception('TransactionContext Error: Catalog write-write '
                            'conflict on create with "v_current_answers"')

        def disk_full(c):
            raise Exception("IO Error: No space left on device")

        labels_backup.labels_db.init_labels_db = racing
        try:
            res = labels_backup.run_backup(config, trigger="schedule", force=True,
                                           poster=_Recorder())
        finally:
            labels_backup.labels_db.init_labels_db = real
        check("a racing DDL is reported as contention, not failure",
              res["status"] == "skipped", str(res))
        check("...so no cooldown is armed and no false alarm is raised",
              res.get("error") is None, str(res.get("error")))

        labels_backup.labels_db.init_labels_db = disk_full
        try:
            res2 = labels_backup.run_backup(config, trigger="schedule", force=True,
                                            poster=_Recorder())
        finally:
            labels_backup.labels_db.init_labels_db = real
        check("a genuine DDL error still fails, loudly",
              res2["status"] == "failed", str(res2))
        check("...naming the real cause", "No space left" in str(res2.get("error")),
              str(res2.get("error")))


def main():
    print("=== test_labels_backup ===")
    test_gate_and_bundle()
    test_csv_contains_the_answers()
    test_a_day_of_only_point_work_is_backed_up()
    test_snapshot_is_a_real_db()
    test_oversize_ladder_and_retention()
    test_a_failed_delivery_still_completes_the_backup()
    test_a_genuine_failure_still_fails()
    test_discord_refusing_an_attachment_walks_the_ladder()
    test_retention_still_runs_when_delivery_fails()
    test_the_upload_budget_sits_below_discords_floor()
    test_contention_is_skipped_not_failed()
    test_unset_webhook_is_a_clean_noop()
    test_a_forced_run_does_not_consume_the_scheduled_slot()
    test_a_store_that_has_never_succeeded_raises_the_alarm()
    test_a_rung_that_fails_to_build_falls_through_to_the_summary()
    test_a_racing_ddl_is_contention_not_a_failure()
    print("==========================")
    if _FAILED:
        print(f"{_FAILED} check(s) FAILED")
        sys.exit(1)
    print("PASS")


if __name__ == "__main__":
    main()
