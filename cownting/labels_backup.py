"""Weekly zip of the label store, rotated locally and posted to a Discord webhook.

`data/labels.duckdb` is the one piece of state on the box that cannot be
regenerated — frames, detections and overlays all come back from a re-ingest;
annotator hours do not — and because it deliberately sits outside the main DB, it
is also outside the `tar czf data/` ritual operators actually remember. So this
module backs it up on its own schedule: an in-process daemon thread (started from
`create_app`'s boot block, mirroring `localize_worker`'s `_LOCK` + `_state` +
`status()` shape) that ticks every 15 minutes and asks the DB *"has a week passed
since the last successful run, and did anything land since its watermark?"* —
never `sleep(7*86400)`, which would reset on every redeploy and therefore never
fire on a box that is redeployed weekly.

The failure taxonomy matters more than it looks (§6.2 of the roadmap):

- **Contention** — the store is held by another process, `db.connect` exhausts its
  ~9 s retry budget, or the claim is refused — is `status='skipped'`, no row
  written, **no cooldown armed**, exit code 0. Without that split, an operator who
  wires `labels backup --force` into a nightly host cron converts a transient lock
  error into a *permanently disabled* weekly backup: every nightly failure would
  re-arm the 6-hour cooldown and the scheduler's tick never finds a green window.
- **Genuine failure** — disk full, torn snapshot, Discord unreachable — writes a
  `failed` row, prints a `[cownting.alert] LABEL-BACKUP` line for `alerts/watch.py`
  to forward, arms a 6-hour cooldown, and does NOT advance the watermark.

The webhook URL comes from COWNTING_DISCORD_WEBHOOK at the point of use, never
from Config or YAML (the COWNTING_SECRET rule). Unset is a supported state: the
job still zips and rotates locally and does advance the watermark. The URL and
its token must never reach a log line, an exception string or a stored DB column
— `backup_runs.error` ends up inside the very zip that gets posted to a channel —
so everything printed or stored goes through `_redact()` first.

Import discipline: this module is opened by the CLI and the scheduler in
processes that never build a queue, so it imports `labels_db` (the store) and
`db.connect` (the retry) but never `labeling` (which drags PIL in behind it).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import duckdb

from . import __version__, db, labels_db
from .config import BackupCfg, Config

# The env var holding the Discord webhook URL. Read at the point of use, never at
# import and never from YAML: config/ is committed and bind-mounted :ro, and a
# webhook URL is a bearer credential for posting into the channel.
WEBHOOK_ENV = "COWNTING_DISCORD_WEBHOOK"

# label_meta key: max(submitted_at) covered by the last SUCCESSFUL run. Kept in
# the label store itself (not a sidecar file) so it travels inside every snapshot
# — a restored backup knows what has already been posted.
META_WATERMARK = "backup_watermark"

# First tick delay. LOAD-BEARING: longer than any test run, so the ~20 apps
# tests/test_api.py and tests/test_auth.py build in temp dirs never fire a backup,
# and the sleeping thread holds no file handle. Lowering it resurrects the Windows
# TemporaryDirectory-cleanup crash documented at tests/test_auth.py:209-217.
_FIRST_TICK_S = 120
_TICK_S = 15 * 60

# Genuine-failure cooldown. Long enough that a broken webhook does not spam the
# channel's alert feed every 15 minutes, short enough that the weekly cadence is
# not visibly dented.
_COOLDOWN_HOURS = 6

# A 'running' claim older than this is treated as abandoned (SIGKILL mid-run) and
# closed out as failed, so a crashed runner cannot wedge the job forever.
_CLAIM_LEASE_HOURS = 1

_ALERT = "[cownting.alert] LABEL-BACKUP"
_UA = "cownting-labels-backup/1.0 (+https://cownting.schutera.com)"
_ZIP_GLOB = "labels-*.zip"

# Accept only real Discord webhook endpoints. Anything else is refused WITHOUT
# logging the value — a typo'd URL is still someone's URL.
_WEBHOOK_OK_RE = re.compile(
    r"^https://(?:[\w.-]+\.)?discord(?:app)?\.com/api/webhooks/\S+$", re.IGNORECASE
)
# Redaction net for text that merely CONTAINS a webhook URL — urllib puts the full
# URL into HTTPError attributes and third-party text can embed one anywhere.
_WEBHOOK_ANY_RE = re.compile(r"https?://\S*discord(?:app)?\.com/api/webhooks/\S+", re.IGNORECASE)

_TXN_EXC = getattr(duckdb, "TransactionException", duckdb.Error)

# poster(webhook_url, content, file_path_or_None) -> None, raising on failure.
# Injected so the retry/oversize ladder is unit-testable with no network.
Poster = Callable[[str, str, "Path | None"], None]


class _StoreBusy(Exception):
    """The label store is held elsewhere — the CONTENTION class, never a failure."""


# ------------------------------------------------------------------- redaction

def _redact(text: str, webhook: str | None = None) -> str:
    """Scrub the webhook URL and its token from any string before it is printed,
    raised onward, or stored.

    `backup_runs.error` travels inside the zip that gets posted to the channel,
    and `alerts/watch.py` forwards matching log lines to a webhook of its own —
    either path would hand out the ability to post as this box. When `webhook` is
    None the current env value is scrubbed too, so a caller cannot forget."""
    out = _WEBHOOK_ANY_RE.sub("<discord-webhook>", text or "")
    if webhook is None:
        webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    if webhook:
        out = out.replace(webhook, "<discord-webhook>")
        token = webhook.rstrip("/").rsplit("/", 1)[-1]
        # Only a real token: replacing a short trailing segment ("1", "api") would
        # chew holes in unrelated text.
        if len(token) >= 8:
            out = out.replace(token, "<token>")
    return out


def _looks_like_webhook(url: str) -> bool:
    return bool(_WEBHOOK_OK_RE.match(url))


# ------------------------------------------------------------------- store access

def ensure_backup_tables(con: duckdb.DuckDBPyConnection) -> None:
    """Create the run-history table + its sequence. Idempotent.

    `labels_db.init_labels_db` declares the same DDL — deliberately, so the
    watermark and run history travel inside the very snapshot they describe (a
    snapshot without them would restore as "never backed up" and re-post
    everything). Re-declared here because this module also runs against stores
    created by builds that predate it, and a backup that fails on its own
    bookkeeping is the silent kind of dead."""
    con.execute("CREATE SEQUENCE IF NOT EXISTS seq_backup_run START 1;")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS backup_runs (
            run_id BIGINT DEFAULT nextval('seq_backup_run'),
            started_at TIMESTAMP, finished_at TIMESTAMP,
            status VARCHAR,
            trigger VARCHAR,
            holder VARCHAR,
            watermark_from TIMESTAMP, watermark_to TIMESTAMP,
            annotations BIGINT, new_annotations BIGINT,
            zip_path VARCHAR, zip_bytes BIGINT, discord VARCHAR, error VARCHAR
        );
        """
    )


def _open_store(config: Config) -> duckdb.DuckDBPyConnection:
    """Open the label store read-write and make sure the schema exists.

    Read-write even though a backup mostly reads: DuckDB refuses a second
    connection to one file with a different mode in the same process (labels_db §3
    documents the trap). A connect failure that is the cross-process lock is
    re-raised as `_StoreBusy` — the contention class, reported as `skipped` rather
    than `failed` — because the server in the container legitimately holds the file
    whenever a submit is in flight.

    Classification shares `db.is_transient_lock_error` rather than keeping a private
    substring list. `db.connect` has already exhausted ~9 s of retries by the time
    we see the error, so reaching here means the holder is not letting go on a
    backup's timescale; but the *set of wordings that mean "someone else holds it"*
    must be one definition, or a platform that gets added to one list and not the
    other turns a routine contention skip into a spurious failure (and a failure
    arms a 6 h cooldown, so the divergence silently suppresses backups)."""
    try:
        con = db.connect(config.paths.labels_db_path)
    except Exception as e:  # noqa: BLE001 — classify, then re-raise
        if db.is_transient_lock_error(e) or "lock" in str(e).lower():
            raise _StoreBusy(str(e)) from e
        raise
    labels_db.init_labels_db(con)
    ensure_backup_tables(con)
    return con


# ------------------------------------------------------------------- gate + claim

def due(con: duckdb.DuckDBPyConnection, backup: BackupCfg) -> tuple[bool, str]:
    """Is a scheduled run warranted? Returns (is_due, human reason).

    The order of the checks is the cheap-first order, but the semantics are AND:
    something new landed since the watermark, no successful run within
    `every_days`, and no genuine failure within the cooldown. An empty store is
    never due — there is nothing to lose yet."""
    # BOTH keyed tables. The geometry step writes to `mask_edits` and, for a
    # 'not a cow' verdict, to NOTHING else — the instance is retired, so the
    # annotator never answers it. An annotations-only watermark would therefore
    # never consider a day of pure outline work worth backing up, which is
    # exactly the work that has no other copy.
    last_ann = con.execute(
        "SELECT greatest("
        "  coalesce((SELECT max(submitted_at) FROM annotations), CAST('epoch' AS TIMESTAMP)),"
        "  coalesce((SELECT max(submitted_at) FROM mask_edits),  CAST('epoch' AS TIMESTAMP)))"
    ).fetchone()[0]
    if last_ann is None or str(last_ann).startswith("1970-01-01"):
        return False, "empty store: nothing to back up yet"
    wm = labels_db.get_meta(con, META_WATERMARK)
    if wm is not None and last_ann <= datetime.fromisoformat(wm):
        return False, f"nothing new since the last backup (watermark {wm})"
    # Cooldown: only the MOST RECENT finished run counts. A failed run followed by
    # a successful retry must not keep suppressing the schedule for 6 hours.
    row = con.execute(
        "SELECT status, finished_at > now() - to_hours(?) FROM backup_runs "
        "WHERE finished_at IS NOT NULL ORDER BY finished_at DESC, run_id DESC LIMIT 1",
        [_COOLDOWN_HOURS],
    ).fetchone()
    if row is not None and row[0] == "failed" and bool(row[1]):
        return False, f"cooling down: last run failed within {_COOLDOWN_HOURS}h"
    recent = con.execute(
        "SELECT count(*) FROM backup_runs WHERE status = 'done' "
        "AND finished_at > now() - to_days(CAST(? AS INTEGER))",
        [backup.every_days],
    ).fetchone()[0]
    if recent:
        return False, f"already backed up within the last {backup.every_days} days"
    return True, "due: new annotator work since the watermark"


def _claim(con: duckdb.DuckDBPyConnection, *, trigger: str) -> dict[str, Any] | None:
    """Claim the run via compare-and-set inside an explicit transaction.

    DuckDB grants one writer at a time, so the loser's transaction sees the
    winner's committed 'running' row — a real mutex covering `--workers N`, a
    manual CLI run racing the scheduler thread, and a second container. Returns
    None when the claim is refused (contention, NOT failure — the caller must
    write no row and arm no cooldown). A stale claim past the 1-hour lease is
    closed out as failed and reclaimed, so a SIGKILL mid-run cannot wedge the job
    forever."""
    holder = f"{socket.gethostname()}:{os.getpid()}"
    con.execute("BEGIN")
    try:
        fresh = con.execute(
            "SELECT count(*) FROM backup_runs WHERE status = 'running' "
            "AND started_at > now() - to_hours(?)",
            [_CLAIM_LEASE_HOURS],
        ).fetchone()[0]
        if fresh:
            con.execute("ROLLBACK")
            return None
        con.execute(
            "UPDATE backup_runs SET status = 'failed', finished_at = now(), "
            "error = 'abandoned: claim held past the lease (runner killed mid-run?)' "
            "WHERE status = 'running'"
        )
        wm_raw = labels_db.get_meta(con, META_WATERMARK)
        wm_from = datetime.fromisoformat(wm_raw) if wm_raw else None
        wm_to, total = con.execute(
            "SELECT max(submitted_at), count(*) FROM annotations"
        ).fetchone()
        if wm_from is None:
            new = int(total)
        else:
            new = con.execute(
                "SELECT count(*) FROM annotations WHERE submitted_at > ?", [wm_from]
            ).fetchone()[0]
        run_id = con.execute(
            'INSERT INTO backup_runs (started_at, status, "trigger", holder, '
            "watermark_from, watermark_to, annotations, new_annotations) "
            "VALUES (now(), 'running', ?, ?, ?, ?, ?, ?) RETURNING run_id",
            [trigger, holder, wm_from, wm_to, int(total), new],
        ).fetchone()[0]
        con.execute("COMMIT")
    except _TXN_EXC:
        # A concurrent writer beat us between BEGIN and COMMIT: contention.
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        return None
    except Exception:
        try:
            con.execute("ROLLBACK")
        except duckdb.Error:
            pass
        raise
    return {
        "run_id": int(run_id),
        "holder": holder,
        "watermark_from": wm_from,
        "watermark_to": wm_to,
        "annotations": int(total),
        "new_annotations": int(new),
    }


def _finish(
    con: duckdb.DuckDBPyConnection,
    run_id: int,
    *,
    status: str,
    discord: str | None = None,
    zip_path: str | None = None,
    zip_bytes: int | None = None,
    error: str | None = None,
) -> None:
    con.execute(
        "UPDATE backup_runs SET status = ?, finished_at = now(), discord = ?, "
        "zip_path = ?, zip_bytes = ?, error = ? WHERE run_id = ?",
        [status, discord, zip_path, zip_bytes, error, run_id],
    )


# ------------------------------------------------------------------- snapshot + bundle

def snapshot_db(src_path: str | Path, dest_path: str | Path) -> None:
    """Copy the store with the engine itself: CHECKPOINT -> ATTACH -> COPY FROM
    DATABASE -> CHECKPOINT snap. Never `shutil.copy`.

    DuckDB keeps unflushed pages in a `.duckdb.wal` sidecar, so copying the
    `.duckdb` alone while a label POST is in flight yields a torn file that only
    fails at RESTORE time — the worst possible moment to find out. `COPY FROM
    DATABASE` runs inside the engine against the current committed MVCC snapshot:
    writers are never blocked and the result (tables, views, sequences, data) is
    transactionally whole. The read-write open matters (same-mode rule; see
    `_open_store`)."""
    dest = Path(dest_path)
    if dest.exists():
        # ATTACH onto a leftover file would COPY into a non-empty catalog.
        dest.unlink()
    con = db.connect(str(src_path))
    try:
        try:
            con.execute("CHECKPOINT")
        except duckdb.Error:
            # A concurrent write transaction blocks a plain CHECKPOINT. Folding
            # the WAL first is a size optimisation, not correctness — the COPY
            # below reads a consistent MVCC snapshot either way.
            pass
        catalog = con.execute("SELECT current_database()").fetchone()[0]
        safe_dest = str(dest).replace("'", "''")
        safe_cat = str(catalog).replace('"', '""')
        con.execute(f"ATTACH '{safe_dest}' AS snap")
        con.execute(f'COPY FROM DATABASE "{safe_cat}" TO snap')
        con.execute("CHECKPOINT snap")
        con.execute("DETACH snap")
    finally:
        con.close()


# One row per (annotation, choice), long format — what an inter-rater computation
# wants. LEFT JOIN on annotation_choices so skips (which have no choices) still
# appear as rows; `class_name` is the snapshotted at-label-time name and
# `class_name_now`/`class_active_now` show what the taxonomy says today.
_CSV_SQL = """
SELECT a.annotation_id, a.instance_key, a.effective_key, a.version, a.outcome,
       a.skip_reason, a.dataset_id, a.camera_id, a.frame_basename,
       a.bbox_x1, a.bbox_y1, a.bbox_x2, a.bbox_y2, a.ordinal, a.ts,
       c.group_key, g.name AS group_name,
       c.class_key, c.class_name, cl.name AS class_name_now, cl.active AS class_active_now,
       a.annotator, a.annotator_role, a.annotator_real_role,
       a.acting_preview, a.auth_disabled,
       a.submitted_at, a.superseded_at, a.time_on_task_ms, a.client_elapsed_ms,
       a.input_mode, a.session_id, a.taxonomy_revision
FROM annotations a
LEFT JOIN annotation_choices c USING (annotation_id)
LEFT JOIN label_groups  g  ON g.group_key  = c.group_key
LEFT JOIN label_classes cl ON cl.class_key = c.class_key
ORDER BY a.submitted_at, a.annotation_id, c.ordinal
"""


_MASK_CSV_SQL = """
SELECT e.edit_id, e.instance_key, e.effective_key, e.version, e.kind,
       e.seeded_from, e.n_vertices, e.area_px, e.iou_source, e.mask_rev,
       e.dataset_id, e.camera_id, e.frame_basename,
       e.bbox_x1, e.bbox_y1, e.bbox_x2, e.bbox_y2, e.ordinal,
       e.annotator, e.annotator_role, e.annotator_real_role,
       e.acting_preview, e.auth_disabled,
       e.submitted_at, e.superseded_at, e.client_elapsed_ms, e.session_id,
       e.polygon
FROM mask_edits e
ORDER BY e.submitted_at, e.edit_id
"""


def export_mask_csv(con: duckdb.DuckDBPyConnection, out_path: str | Path) -> int:
    """Write the geometry-verdict CSV — every outline correction, 'not a cow'
    removal and confirmation, with the day and camera it belongs to.

    A sibling of the answers CSV rather than a join onto it: an instance can have
    a geometry verdict and no answer (a removal retires it before any question is
    asked), so folding the two together would either drop those rows or duplicate
    every answer once per verdict."""
    safe = str(out_path).replace("'", "''")
    row = con.execute(
        f"COPY ({_MASK_CSV_SQL}) TO '{safe}' (HEADER, DELIMITER ',')").fetchone()
    return int(row[0]) if row else 0


def export_csv(con: duckdb.DuckDBPyConnection, out_path: str | Path) -> int:
    """Write the long-format annotations CSV. Returns the number of rows written."""
    safe = str(out_path).replace("'", "''")
    row = con.execute(f"COPY ({_CSV_SQL}) TO '{safe}' (HEADER, DELIMITER ',')").fetchone()
    return int(row[0]) if row else 0


def _manifest(
    con: duckdb.DuckDBPyConnection,
    *,
    run: Mapping[str, Any],
    trigger: str,
    members: Sequence[str],
) -> dict[str, Any]:
    """Bundle metadata. Every aggregate is individually guarded: a future schema
    change must degrade the manifest, not raise a KeyError that escapes the
    bundle build, stamps the run failed, holds the watermark, and silently
    disables the job forever behind a 6-hour cooldown."""
    man: dict[str, Any] = {
        "kind": "cownting-labels-backup",
        "created_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "app_version": __version__,
        "run_id": run.get("run_id"),
        "trigger": trigger,
        "watermark_from": str(run["watermark_from"]) if run.get("watermark_from") else None,
        "watermark_to": str(run["watermark_to"]) if run.get("watermark_to") else None,
        "annotations": run.get("annotations"),
        "new_annotations": run.get("new_annotations"),
        "members": list(members),
    }
    try:
        man["schema_version"] = labels_db.get_meta(con, labels_db.META_SCHEMA_VERSION)
    except Exception:  # noqa: BLE001 — degrade, never fail the bundle
        pass
    try:
        man["taxonomy_revision"] = labels_db.taxonomy_revision(con)
    except Exception:  # noqa: BLE001
        pass
    try:
        man["annotators"] = int(
            con.execute("SELECT count(DISTINCT annotator) FROM annotations").fetchone()[0]
        )
    except Exception:  # noqa: BLE001
        pass
    try:
        man["outcomes"] = {
            str(k): int(v)
            for k, v in con.execute(
                "SELECT outcome, count(*) FROM annotations GROUP BY outcome ORDER BY outcome"
            ).fetchall()
        }
    except Exception:  # noqa: BLE001
        pass
    try:
        hist: dict[str, dict[str, int]] = {}
        for g, c, n in con.execute(
            "SELECT group_key, class_key, count(*) FROM v_current_answers "
            "GROUP BY group_key, class_key ORDER BY group_key, class_key"
        ).fetchall():
            hist.setdefault(str(g), {})[str(c)] = int(n)
        man["answers_by_group"] = hist
    except Exception:  # noqa: BLE001
        pass
    return man


def _readme(run: Mapping[str, Any], stamp: str, trigger: str) -> str:
    return f"""# cownting label-store backup

Created {stamp} (UTC) by run {run.get('run_id')} (trigger: {trigger}).
{run.get('annotations')} annotations, {run.get('new_annotations')} new since the previous backup.

Members:

- `labels.duckdb`    — engine-consistent snapshot of the whole store (COPY FROM DATABASE)
- `annotations.csv`  — long format, one row per (annotation, choice); skips included
- `mask_edits.csv`   — one row per geometry verdict: outline corrections, 'not a
  cow' removals and confirmations, with the day and camera each belongs to
- `taxonomy.json`    — groups + classes including archived ones, with the revision
- `MANIFEST.json`    — counts, watermarks, versions
- `README.md`        — this file

## Restore

1. Stop the app: `docker compose down`.
2. Copy `labels.duckdb` from this zip over `data/labels.duckdb` on the host.
3. `docker compose up -d` — `entrypoint.sh` self-heals file ownership on boot.

## Mixed-vintage warning

If this `labels.duckdb` is restored next to an OLDER `data/` tar (older
`cownting.duckdb` + frames), the next reconciliation will report a wall of
`hijacked` because the frame fingerprints disagree wholesale. That is the CORRECT
alarm and the labels are fine — do NOT delete `data/labels.duckdb` over it.
Restore a matching-vintage `data/` tar, or re-ingest, then reconcile again.

## Privacy

This archive contains annotator usernames and per-annotator timings. Share it
like personnel data, not like a database dump.
"""


def _write_zip(dest: Path, members: Sequence[tuple[str, Path]]) -> int:
    """Zip an ENUMERATED member list into `dest` (via `.part` + atomic replace).

    Never a directory walk: `data/.session_secret` (the cookie signing key, chmod
    0600) lives two directories up from the staging dir, and a glob-based backup
    that swept it into a zip posted to a Discord channel would hand out the
    ability to forge any user's login cookie. Returns the final size in bytes."""
    part = dest.with_name(dest.name + ".part")
    with zipfile.ZipFile(part, "w", zipfile.ZIP_DEFLATED) as z:
        for arcname, path in members:
            z.write(path, arcname)
    os.replace(part, dest)
    return dest.stat().st_size


def _prune(out_dir: Path, keep: int) -> list[str]:
    """Keep the newest `keep` zips, pruned BY NAME. Never fails a run.

    Names are UTC timestamps and sort chronologically; a zip restored onto the
    box carries an unrelated mtime that would evict the wrong file. Per-file
    errors are swallowed with a log line: a root-owned stray from a
    `docker compose exec` without `-u cownting` raises PermissionError here, and
    losing a rotation beats losing the backup that just succeeded."""
    deleted: list[str] = []
    keep = max(1, int(keep))
    try:
        zips = sorted(p for p in out_dir.glob(_ZIP_GLOB))
    except OSError:
        return deleted
    for p in zips[:-keep] if len(zips) > keep else []:
        try:
            p.unlink()
            deleted.append(p.name)
        except OSError as e:
            print(f"{_ALERT} prune: could not remove {p.name}: {_redact(str(e))}")
    return deleted


# ------------------------------------------------------------------- discord

def _discord_poster(webhook: str, content: str, file_path: Path | None) -> None:
    """Default poster: stdlib urllib + a hand-rolled multipart encoder.

    `requests` is not a dependency and `alerts/watch.py` already establishes
    urllib + explicit User-Agent + timeout as the house Discord shape. Not `curl`
    via subprocess — the webhook URL would land in the process argv table,
    visible to `docker top`. Raises on any non-2xx (urllib does that for us)."""
    if file_path is None:
        body = json.dumps({"content": content[:1900]}).encode()
        req = urllib.request.Request(
            webhook, data=body,
            headers={"Content-Type": "application/json", "User-Agent": _UA},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()
        return
    boundary = "cownting" + uuid.uuid4().hex
    payload = json.dumps({"content": content[:1900]}).encode()
    crlf = b"\r\n"
    body = b"".join([
        b"--", boundary.encode(), crlf,
        b'Content-Disposition: form-data; name="payload_json"', crlf,
        b"Content-Type: application/json", crlf, crlf,
        payload, crlf,
        b"--", boundary.encode(), crlf,
        b'Content-Disposition: form-data; name="files[0]"; filename="',
        file_path.name.encode(), b'"', crlf,
        b"Content-Type: application/zip", crlf, crlf,
        file_path.read_bytes(), crlf,
        b"--", boundary.encode(), b"--", crlf,
    ])
    req = urllib.request.Request(
        webhook, data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": _UA,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()


def _deliver(
    *,
    webhook: str,
    enabled: bool,
    poster: Poster,
    budget: int,
    zip_path: Path,
    zip_bytes: int,
    csv_members: Sequence[tuple[str, Path]],
    staging: Path,
    stamp: str,
    run: Mapping[str, Any],
) -> str:
    """Post the bundle, walking the oversize ladder. Returns the recorded mode.

    Ladder against `max_upload_bytes` (Discord's unboosted cap is 10 MiB and it
    rejects rather than truncating): full zip -> CSV-only zip (the `.duckdb` is
    the bulky page-aligned part; the analytically useful CSV stays small far
    longer) -> a summary message naming the retained on-disk path. Every rung
    advances the watermark, because re-running would produce the same oversize
    zip. Unset/blank webhook is a clean no-op; a non-Discord URL is refused
    without logging its value. Poster exceptions propagate — they are the
    genuine-failure class."""
    if not enabled:
        return "disabled"
    if not webhook:
        return "skipped"
    if not _looks_like_webhook(webhook):
        print(
            f"{_ALERT} webhook refused: {WEBHOOK_ENV} is not a "
            "discord.com/api/webhooks URL (value not logged)"
        )
        return "refused"
    base = (
        f"Label backup {stamp}: {run.get('annotations')} annotations "
        f"({run.get('new_annotations')} new)."
    )
    if zip_bytes <= budget:
        poster(webhook, base, zip_path)
        return "posted"
    csv_zip = staging / f"labels-{stamp}-csv.zip"
    csv_bytes = _write_zip(csv_zip, list(csv_members))
    if csv_bytes <= budget:
        print(f"{_ALERT} oversize: full zip is {zip_bytes} B (cap {budget} B) — posting CSV-only")
        poster(
            webhook,
            base + f" Full zip is {zip_bytes / 1e6:.1f} MB — over the upload cap, so the "
            f"CSV bundle is attached; the full zip is retained on the box at {zip_path}.",
            csv_zip,
        )
        return "posted_csv_only"
    print(f"{_ALERT} oversize: even CSV-only is {csv_bytes} B (cap {budget} B) — posting a summary")
    poster(
        webhook,
        base + f" Bundle exceeds the upload cap even CSV-only "
        f"({csv_bytes / 1e6:.1f} MB) — retained on the box at {zip_path}.",
        None,
    )
    return "posted_summary"


# ------------------------------------------------------------------- the run

def run_backup(
    config: Config,
    *,
    trigger: str = "cli",
    force: bool = False,
    keep: int | None = None,
    discord: bool = True,
    poster: Poster | None = None,
) -> dict[str, Any]:
    """One backup attempt, end to end. Never raises; returns a result dict:

        {status: 'done'|'skipped'|'failed', reason, run_id, zip_path, zip_bytes,
         discord, new_annotations, error}

    `status='skipped'` covers both "not due" and the whole contention class
    (store held elsewhere, claim refused) — no row is written and no cooldown is
    armed, so wiring this into a cron cannot disable the scheduler. `force=True`
    bypasses the due-gate but NOT the claim: a run already in flight still wins.
    `discord=False` (CLI --no-discord) zips + rotates locally and still advances
    the watermark. A failed run holds the watermark so nothing is ever silently
    dropped from the next post. Files land owned by whoever runs this — inside
    the container that is uid 10001 by construction (the scheduler runs after
    `entrypoint.sh`'s privilege drop); hand-runs must use `-u cownting`."""
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out_dir = Path(config.paths.backups_dir) / "labels"
    keep_n = config.backup.keep if keep is None else keep
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    post = poster or _discord_poster
    result: dict[str, Any] = {
        "status": "skipped", "reason": None, "trigger": trigger, "run_id": None,
        "zip_path": None, "zip_bytes": None, "discord": None,
        "new_annotations": None, "error": None,
    }
    try:
        lcon = _open_store(config)
    except _StoreBusy:
        result["reason"] = "store busy"
        _note(result)
        return result
    except Exception as e:  # noqa: BLE001 — genuine failure, but nowhere to write a row
        result.update(status="failed", error=_redact(str(e) or repr(e), webhook))
        print(f"{_ALERT} failed: {result['error']}")
        _note(result)
        return result

    staging: str | None = None
    try:
        if trigger == "schedule":
            _stale_alert(lcon, config.backup)
        if not force:
            ok, reason = due(lcon, config.backup)
            if not ok:
                result["reason"] = reason
                return result
        run = _claim(lcon, trigger=trigger)
        if run is None:
            # The frozen reason string (§6.2) for the whole contention class —
            # here it means another runner holds the claim.
            result["reason"] = "store busy"
            return result
        with _LOCK:
            _state["status"] = "running"
        result["run_id"] = run["run_id"]
        result["new_annotations"] = run["new_annotations"]
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            # Same filesystem as the final zip, so os.replace stays atomic.
            staging = tempfile.mkdtemp(prefix=".staging-", dir=out_dir)
            sdir = Path(staging)
            snapshot_db(config.paths.labels_db_path, sdir / "labels.duckdb")
            export_csv(lcon, sdir / "annotations.csv")
            export_mask_csv(lcon, sdir / "mask_edits.csv")
            (sdir / "taxonomy.json").write_text(
                json.dumps(labels_db.taxonomy(lcon, include_archived=True),
                           indent=2, default=str),
                encoding="utf-8",
            )
            members = [
                ("labels.duckdb", sdir / "labels.duckdb"),
                ("annotations.csv", sdir / "annotations.csv"),
                # Ordered right after the answers so the degraded, CSV-only
                # bundle (the fallback when the zip is over the webhook budget)
                # still carries the geometry work rather than only the answers.
                ("mask_edits.csv", sdir / "mask_edits.csv"),
                ("taxonomy.json", sdir / "taxonomy.json"),
                ("MANIFEST.json", sdir / "MANIFEST.json"),
                ("README.md", sdir / "README.md"),
            ]
            man = _manifest(lcon, run=run, trigger=trigger, members=[m[0] for m in members])
            (sdir / "MANIFEST.json").write_text(
                json.dumps(man, indent=2, default=str), encoding="utf-8"
            )
            (sdir / "README.md").write_text(_readme(run, stamp, trigger), encoding="utf-8")
            # run_id suffix (zero-padded so name-sort stays chronological): two
            # runs inside one second — trivial to do from a test or a nervous
            # operator — would otherwise silently overwrite one zip.
            zip_path = out_dir / f"labels-{stamp}-{run['run_id']:06d}.zip"
            zip_bytes = _write_zip(zip_path, members)
            result.update(zip_path=str(zip_path), zip_bytes=zip_bytes)
            mode = _deliver(
                webhook=webhook, enabled=discord, poster=post,
                budget=config.backup.max_upload_bytes,
                zip_path=zip_path, zip_bytes=zip_bytes,
                csv_members=members[1:], staging=sdir, stamp=stamp, run=run,
            )
            result["discord"] = mode
        except Exception as e:  # noqa: BLE001 — the genuine-failure class
            err = _redact(str(e) or repr(e), webhook)
            _finish(
                lcon, run["run_id"], status="failed",
                zip_path=result["zip_path"], zip_bytes=result["zip_bytes"], error=err,
            )
            print(f"{_ALERT} failed run {run['run_id']}: {err}")
            result.update(status="failed", error=err)
            return result
        # Success: close the row, THEN advance the watermark, THEN rotate.
        _finish(
            lcon, run["run_id"], status="done", discord=mode,
            zip_path=str(zip_path), zip_bytes=zip_bytes,
        )
        if run["watermark_to"] is not None:
            labels_db.set_meta(lcon, META_WATERMARK, str(run["watermark_to"]))
        _prune(out_dir, keep_n)
        result["status"] = "done"
        return result
    finally:
        if staging:
            shutil.rmtree(staging, ignore_errors=True)
        lcon.close()
        _note(result)


def _stale_alert(con: duckdb.DuckDBPyConnection, backup: BackupCfg) -> None:
    """Print the staleness alarm when the last success is over 2*every_days old.

    Printed on every scheduler tick while true, which `alerts/watch.py` already
    tails — this is what closes the loop when the gate itself is wedged (e.g. a
    persistent failure inside the cooldown window)."""
    row = con.execute(
        "SELECT max(finished_at), max(finished_at) < now() - to_days(CAST(? AS INTEGER)) "
        "FROM backup_runs WHERE status = 'done'",
        [2 * backup.every_days],
    ).fetchone()
    if row is not None and row[0] is not None and bool(row[1]):
        print(f"{_ALERT} stale: last successful backup {row[0]} — over {2 * backup.every_days} days ago")


# ------------------------------------------------------------------- scheduler

_LOCK = threading.Lock()
_config: Config | None = None
_running = False
_thread: threading.Thread | None = None
_state: dict[str, Any] = {
    "status": "idle",   # idle | running | done | skipped | failed
    "at": None,         # epoch secs of the last run_backup completion
    "last": None,       # the last run_backup result dict
}


def _note(result: dict[str, Any]) -> None:
    with _LOCK:
        _state.update(status=result["status"], at=time.time(), last=dict(result))


def status(config: Config, *, limit: int = 5) -> dict[str, Any]:
    """Backup health for the CLI and GET /api/labels/backup/status.

    Reports WHETHER a webhook is configured, never the URL. Degrades to an
    `error` field instead of raising when the store is held elsewhere, because a
    status probe must never look like an outage."""
    webhook = os.environ.get(WEBHOOK_ENV, "").strip()
    with _LOCK:
        sched = {
            "status": _state["status"],
            "at": _state["at"],
            "last": _state["last"],
            "thread_alive": bool(_thread is not None and _thread.is_alive()),
        }
    out: dict[str, Any] = {
        "enabled": config.backup.enabled,
        "every_days": config.backup.every_days,
        "keep": config.backup.keep,
        "max_upload_bytes": config.backup.max_upload_bytes,
        "webhook_configured": bool(webhook),
        "webhook_valid": bool(webhook) and _looks_like_webhook(webhook),
        "scheduler": sched,
        "due": None, "due_reason": None, "watermark": None,
        "runs": [], "error": None,
    }
    try:
        con = _open_store(config)
    except _StoreBusy:
        out["error"] = "store busy"
        return out
    except Exception as e:  # noqa: BLE001 — degrade, never 500 a status probe
        out["error"] = _redact(str(e) or repr(e), webhook)
        return out
    try:
        is_due, reason = due(con, config.backup)
        out["due"], out["due_reason"] = is_due, reason
        out["watermark"] = labels_db.get_meta(con, META_WATERMARK)
        cols = ("run_id", "started_at", "finished_at", "status", "trigger", "holder",
                "new_annotations", "zip_path", "zip_bytes", "discord", "error")
        rows = con.execute(
            'SELECT run_id, started_at, finished_at, status, "trigger", holder, '
            "new_annotations, zip_path, zip_bytes, discord, error "
            "FROM backup_runs ORDER BY run_id DESC LIMIT ?",
            [max(1, int(limit))],
        ).fetchall()
        out["runs"] = [
            {
                k: (_redact(v, webhook) if k == "error" and isinstance(v, str)
                    else str(v) if isinstance(v, datetime) else v)
                for k, v in zip(cols, r)
            }
            for r in rows
        ]
    finally:
        con.close()
    return out


def start_scheduler(config: Config) -> None:
    """Spawn the tick thread. Called from `create_app`'s boot block; idempotent.

    In-process rather than host cron or a compose sidecar: zero cross-process
    DuckDB lock contention, files land owned by uid 10001 by construction (the
    thread lives past `entrypoint.sh`'s privilege drop, so nothing ever needs
    healing), and it ships in the image so a `git pull && docker compose up -d
    --build` deploy carries it with no host-side setup. `backup.enabled` is read
    on every tick, so the thread is started unconditionally and a disabled config
    costs one sleeping daemon thread that touches no file."""
    global _config, _running, _thread
    with _LOCK:
        _config = config
        if _running:
            return
        _running = True
        try:
            _thread = threading.Thread(target=_ticker, name="labels-backup", daemon=True)
            _thread.start()
        except BaseException:
            # Spawn failed ("can't start new thread" under load). Roll back so a
            # dead scheduler can't report itself running forever.
            _running = False
            raise


def _ticker() -> None:
    time.sleep(_FIRST_TICK_S)
    while True:
        with _LOCK:
            cfg = _config
        if cfg is not None and cfg.backup.enabled:
            try:
                run_backup(cfg, trigger="schedule")
            except Exception as e:  # noqa: BLE001 — run_backup shouldn't raise; keep ticking
                # Message only, no traceback: an exception chain can embed the
                # webhook URL (urllib puts it in HTTPError attributes).
                print(f"{_ALERT} tick crashed: {_redact(str(e) or repr(e))}")
        time.sleep(_TICK_S)
