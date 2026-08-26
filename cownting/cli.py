"""Cownting command-line interface (Typer).

Typical flow:
    cownting ingest                  # video -> sampled timestamped frames
    cownting segment                 # frames -> instance segmentation + overlays
    cownting localize                # project detections through the saved homography
    cownting kpis                    # print a quick summary
    cownting spotcheck manual.csv    # predicted-vs-manual count error

Stage 1b (fine-tune / re-label loop):
    cownting label-select            # pick a diverse frame subset to correct
    cownting label-export            # bootstrap masks -> CVAT task
    (correct masks in CVAT)
    cownting dataset-build           # corrections -> YOLO-seg dataset
    cownting train                   # fine-tune YOLO11-seg
    cownting eval-detect             # mask/box mAP on the val split
"""
from __future__ import annotations

import typer

from .config import Config

app = typer.Typer(add_completion=False, help="Offline cow / solar-field analysis pipeline.")

CONFIG_OPT = typer.Option("config/cownting.yaml", "--config", "-c", help="Path to the YAML config.")


def _load(config_path: str) -> Config:
    return Config.load(config_path)


@app.command("init-db")
def init_db(config: str = CONFIG_OPT):
    """Create the DuckDB schema."""
    from . import db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path)
    db.init_db(con)
    con.close()
    typer.echo(f"Initialized {cfg.paths.db_path}")


@app.command()
def ingest(
    config: str = CONFIG_OPT,
    dataset: str = typer.Option(None, help="Override the dataset id (default: capture-day slug)."),
    day: str = typer.Option(None, help="Override the capture day (ISO 'YYYY-MM-DD')."),
):
    """Decode each camera's video into timestamped, fps-subsampled frames."""
    from .pipeline import ingest as run

    cfg = _load(config)
    if dataset:
        cfg.dataset.id = dataset
    if day:
        cfg.dataset.day = day
    run(cfg)


@app.command()
def segment(config: str = CONFIG_OPT, limit: int = typer.Option(None, help="Only process N frames.")):
    """Run instance segmentation over unprocessed frames."""
    from .pipeline import segment as run

    run(_load(config), limit=limit)


@app.command()
def localize(
    config: str = CONFIG_OPT,
    dataset: str = typer.Option(None, help="Scope reassignment to one dataset (default: whole DB)."),
):
    """Assign each detection to a count area + panel shelter (image-space)."""
    from .pipeline import localize as run

    run(_load(config), dataset_id=dataset)


@app.command()
def pose(
    config: str = CONFIG_OPT,
    dataset: str = typer.Option(None, help="Scope to one dataset (default: whole DB)."),
    limit: int = typer.Option(None, help="Only pose N frames (preview)."),
):
    """Estimate keypoints -> posture (standing/lying/grazing/unknown) over stored
    detections. Reuses existing bboxes + frames, so it does NOT re-segment.
    Requires flags.pose_enabled."""
    from .pipeline import pose as run

    run(_load(config), dataset_id=dataset, limit=limit)


@app.command()
def datasets(config: str = CONFIG_OPT):
    """List the data-packages (days) in the DB with their frame/detection counts."""
    from . import db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path, read_only=True)
    df = db.datasets(con)
    con.close()
    if df.empty:
        typer.echo("No datasets yet. Run `cownting migrate` to backfill existing data, or `cownting ingest`.")
        return
    typer.echo(df.to_string(index=False))


@app.command()
def migrate(
    config: str = CONFIG_OPT,
    dataset: str = typer.Option(None, help="dataset_id for the existing rows (default: derived capture day)."),
    day: str = typer.Option(None, help="ISO day 'YYYY-MM-DD' for the existing rows (default: derived)."),
    label: str = typer.Option(None, help="Human label (default: a friendly 'Mon DD, YYYY')."),
):
    """Backfill a pre-dataset DB: stamp all existing frames+detections as one
    day-0 data-package so multi-day features light up without re-ingesting."""
    from . import db
    from .config import resolve_dataset

    cfg = _load(config)
    if dataset:
        cfg.dataset.id = dataset
    if day:
        cfg.dataset.day = day
    if label:
        cfg.dataset.label = label
    ds_id, ds_day, ds_label = resolve_dataset(cfg)

    con = db.connect(cfg.paths.db_path)
    db.init_db(con)  # applies the dataset_id ALTERs + creates the datasets table
    con.execute("UPDATE frames SET dataset_id = ? WHERE dataset_id IS NULL", [ds_id])
    con.execute("UPDATE detections SET dataset_id = ? WHERE dataset_id IS NULL", [ds_id])
    db.upsert_dataset(con, ds_id, ds_day, ds_label, status="localized")
    frames_now = con.execute("SELECT count(*) FROM frames WHERE dataset_id = ?", [ds_id]).fetchone()[0]
    dets_now = con.execute("SELECT count(*) FROM detections WHERE dataset_id = ?", [ds_id]).fetchone()[0]
    con.close()
    typer.echo(f"Migrated existing data -> dataset {ds_id!r} ({ds_label}): "
               f"{frames_now} frames, {dets_now} detections stamped.")


@app.command()
def process(config: str = CONFIG_OPT, limit: int = typer.Option(None, help="Only segment N frames.")):
    """Run the whole batch end to end: ingest -> segment -> localize."""
    from .pipeline import process as run

    run(_load(config), limit=limit)


@app.command()
def kpis(config: str = CONFIG_OPT):
    """Print a quick KPI summary."""
    from . import db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path, read_only=True)
    summary = db.kpi_summary(con)
    con.close()
    for k, v in summary.items():
        typer.echo(f"{k:>16}: {v}")


@app.command()
def spotcheck(manual_csv: str, config: str = CONFIG_OPT):
    """Predicted-vs-manual count error from a frame_path,manual_count CSV."""
    from .eval import count_error

    result = count_error(_load(config), manual_csv)
    for k, v in result.items():
        typer.echo(f"{k:>14}: {v}")


@app.command("label-select")
def label_select(config: str = CONFIG_OPT):
    """Stage 1b: pick a diverse frame subset to hand-correct (writes selected.txt)."""
    from .finetune import select_frames

    select_frames(_load(config))


@app.command("label-export")
def label_export(
    config: str = CONFIG_OPT,
    launch: bool = typer.Option(True, help="Open the CVAT editor after pushing."),
):
    """Stage 1b: bootstrap masks (Grounded-SAM2) and push a CVAT annotation task."""
    from .finetune import export_to_cvat

    export_to_cvat(_load(config), launch=launch)


@app.command("dataset-build")
def dataset_build(config: str = CONFIG_OPT):
    """Stage 1b: pull CVAT corrections into a YOLO-seg dataset (images/labels/data.yaml)."""
    from .finetune import build_dataset

    build_dataset(_load(config))


@app.command()
def train(config: str = CONFIG_OPT):
    """Stage 1b: fine-tune YOLO11-seg on the corrected masks."""
    from .finetune import train as run

    run(_load(config))


@app.command("eval-detect")
def eval_detect(
    config: str = CONFIG_OPT,
    weights: str = typer.Option(None, help="Weights to eval; defaults to finetune.weights_out."),
):
    """Stage 1b: mask/box mAP for the fine-tuned weights on the val split."""
    from .finetune import evaluate

    evaluate(_load(config), weights=weights)


user_app = typer.Typer(help="Manage dashboard login accounts (recovery / scripting).")
app.add_typer(user_app, name="user")


@user_app.command("list")
def user_list(config: str = CONFIG_OPT):
    """List login accounts and their roles."""
    from . import auth, db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path)
    auth.init_auth(con)
    users = auth.list_users(con)
    con.close()
    if not users:
        typer.echo("No users yet. `cownting serve` seeds a bootstrap admin on first boot.")
        return
    for u in users:
        typer.echo(f"{u['role']:>6}  {u['username']}")


@user_app.command("add")
def user_add(
    username: str,
    config: str = CONFIG_OPT,
    role: str = typer.Option(
        "user", "--role",
        help="Account role: user (view only), poweruser (upload/download/delete), or admin.",
    ),
    admin: bool = typer.Option(False, "--admin", help="Shorthand for --role admin."),
    password: str = typer.Option(None, help="Password (prompted securely if omitted)."),
):
    """Create a login account."""
    from . import auth, db

    role = "admin" if admin else role
    if role not in auth.ROLES:
        typer.secho(f"error: role must be one of {', '.join(auth.ROLES)}", fg=typer.colors.RED)
        raise typer.Exit(1)
    cfg = _load(config)
    if password is None:
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    con = db.connect(cfg.paths.db_path)
    auth.init_auth(con)
    try:
        auth.create_user(con, username, password, role=role)
    except ValueError as e:
        con.close()
        typer.secho(f"error: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    con.close()
    typer.secho(f"created {role} {username!r}.", fg=typer.colors.GREEN)


@user_app.command("passwd")
def user_passwd(
    username: str,
    config: str = CONFIG_OPT,
    password: str = typer.Option(None, help="New password (prompted securely if omitted)."),
):
    """Reset an account's password (use this if you're locked out)."""
    from . import auth, db

    cfg = _load(config)
    if password is None:
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
    con = db.connect(cfg.paths.db_path)
    auth.init_auth(con)
    try:
        auth.set_password(con, username, password)
    except ValueError as e:
        con.close()
        typer.secho(f"error: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    con.close()
    typer.secho(f"password updated for {username!r}.", fg=typer.colors.GREEN)


@user_app.command("delete")
def user_delete(username: str, config: str = CONFIG_OPT):
    """Delete a login account (refuses to remove the last admin)."""
    from . import auth, db

    cfg = _load(config)
    con = db.connect(cfg.paths.db_path)
    auth.init_auth(con)
    try:
        auth.delete_user(con, username)
    except ValueError as e:
        con.close()
        typer.secho(f"error: {e}", fg=typer.colors.RED)
        raise typer.Exit(1)
    con.close()
    typer.secho(f"deleted {username!r}.", fg=typer.colors.GREEN)


labels_app = typer.Typer(
    help="In-app annotation store (Label page): backup, export, status, reconcile. "
         "Unrelated to the Stage-1b label-select/label-export CVAT loop."
)
app.add_typer(labels_app, name="labels")


@labels_app.command("backup")
def labels_backup(
    config: str = CONFIG_OPT,
    force: bool = typer.Option(
        False, "--force",
        help="Bypass the weekly due-gate (not the claim: a run already in flight still wins).",
    ),
    keep: int = typer.Option(None, "--keep", help="Zips to retain (default: backup.keep)."),
    no_discord: bool = typer.Option(
        False, "--no-discord", help="Zip + rotate locally without posting to the webhook."
    ),
):
    """Back up the label store now: snapshot -> zip -> data/backups/labels/ -> Discord.

    'skipped' (not due, or the store is busy) exits 0 on purpose — treating
    contention as failure is how a nightly cron wedges the weekly schedule behind
    a permanent cooldown. Only a genuine failure exits 1.
    """
    from . import labels_backup as backup_mod

    res = backup_mod.run_backup(
        _load(config),
        trigger="forced" if force else "cli",
        force=force,
        keep=keep,
        discord=not no_discord,
    )
    if res["status"] == "failed":
        typer.secho(f"backup failed: {res['error']}", fg=typer.colors.RED)
        raise typer.Exit(1)
    if res["status"] == "skipped":
        typer.echo(f"skipped: {res['reason']}")
        return
    typer.secho(
        f"done: {res['zip_path']} ({res['zip_bytes']} bytes), "
        f"{res['new_annotations']} new annotations, discord={res['discord']}",
        fg=typer.colors.GREEN,
    )


@labels_app.command("export-csv")
def labels_export_csv(out: str, config: str = CONFIG_OPT):
    """Write the long-format annotations CSV (one row per annotation x choice)."""
    from . import db, labels_backup, labels_db

    cfg = _load(config)
    con = db.connect(cfg.paths.labels_db_path)
    labels_db.init_labels_db(con)
    n = labels_backup.export_csv(con, out)
    con.close()
    typer.echo(f"wrote {n} rows -> {out}")


@labels_app.command("status")
def labels_status(
    config: str = CONFIG_OPT,
    limit: int = typer.Option(5, "--limit", help="Recent backup runs to show."),
):
    """Backup health: gate state, watermark, recent runs.

    Prints WHETHER a webhook is configured, never the URL — the value is a bearer
    credential for posting into the channel.
    """
    from . import labels_backup

    st = labels_backup.status(_load(config), limit=limit)
    webhook = "yes" if st["webhook_configured"] else "no"
    if st["webhook_configured"] and not st["webhook_valid"]:
        webhook = "yes (NOT a discord.com/api/webhooks URL — will be refused)"
    typer.echo(f"    enabled: {st['enabled']} (every {st['every_days']}d, keep {st['keep']})")
    typer.echo(f"    webhook: {webhook}")
    typer.echo(f"  scheduler: {st['scheduler']['status']}"
               + (" (thread alive)" if st["scheduler"]["thread_alive"] else ""))
    if st["error"]:
        typer.secho(f"      store: {st['error']}", fg=typer.colors.YELLOW)
        return
    typer.echo(f"        due: {st['due']} — {st['due_reason']}")
    typer.echo(f"  watermark: {st['watermark'] or '(none — first run posts everything)'}")
    if not st["runs"]:
        typer.echo("       runs: none yet")
        return
    for r in st["runs"]:
        line = (f"  #{r['run_id']}  {r['status']:>7}  {r['trigger']:>8}  "
                f"finished={r['finished_at'] or '-'}  new={r['new_annotations']}  "
                f"discord={r['discord'] or '-'}")
        if r["error"]:
            line += f"  error={r['error']}"
        typer.echo(line)


@labels_app.command("reconcile")
def labels_reconcile(
    config: str = CONFIG_OPT,
    dataset: str = typer.Option(None, "--dataset", help="Scope to one dataset (default: all)."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Report states without writing."),
):
    """Re-attach labels to detections after a re-ingest (prints per-state counts)."""
    from pathlib import Path

    from . import db, labels_db

    cfg = _load(config)
    if not Path(cfg.paths.db_path).exists():
        # read_only=True refuses to create a file, and reconciling against a DB
        # that was never ingested is meaningless anyway.
        typer.echo(f"no main DB at {cfg.paths.db_path} — nothing to reconcile against.")
        return
    lcon = db.connect(cfg.paths.labels_db_path)
    labels_db.init_labels_db(lcon)
    mcon = db.connect(cfg.paths.db_path, read_only=True)
    # Without the archive DB an archived instance is indistinguishable from an
    # orphan; opening it read-write would CREATE an empty file on a box that
    # never archived anything, so probe for existence first.
    acon = None
    if Path(cfg.paths.archive_db_path).exists():
        acon = db.connect(cfg.paths.archive_db_path, read_only=True)
    try:
        res = labels_db.reconcile_dataset(
            lcon, mcon, dataset, actor="cli", archive_con=acon, dry_run=dry_run
        )
    finally:
        if acon is not None:
            acon.close()
        mcon.close()
        lcon.close()
    if dry_run:
        typer.echo("dry run — nothing written:")
    for k, v in res.items():
        typer.echo(f"{k:>20}: {v}")


@labels_app.command("reseed")
def labels_reseed(
    config: str = CONFIG_OPT,
    force: bool = typer.Option(
        False, "--force",
        help="Refresh seeded names/descriptions/order onto existing keys "
             "(never un-archives an operator's archive decision).",
    ),
):
    """Re-apply the shipped taxonomy seed.

    Without --force this is the idempotent presence-of-key seed (a no-op on an
    already-seeded store); with it, the shipped wording is refreshed onto
    existing keys, audited, and the revision bumped.
    """
    from . import db, labels_db

    cfg = _load(config)
    con = db.connect(cfg.paths.labels_db_path)
    labels_db.init_labels_db(con)
    n = labels_db.seed_taxonomy(con, force=force, actor="cli")
    rev = labels_db.taxonomy_revision(con)
    con.close()
    typer.echo(f"seed touched {n} rows; taxonomy revision is now {rev}.")


def _run_test_gate() -> bool:
    """Run `python -m tests` before serving. Returns True to proceed, False to
    abort. If the tests/ dir is absent (e.g. an installed build without it), the
    gate is skipped with a note rather than blocking."""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    if not (root / "tests").is_dir():
        typer.echo("serve: no tests/ dir found — skipping pre-boot test gate.")
        return True
    typer.echo("serve: running test gate (`python -m tests`) before boot…")
    rc = subprocess.run([sys.executable, "-m", "tests"], cwd=str(root)).returncode
    if rc != 0:
        typer.secho(
            "serve: ABORTED — tests failed. Fix them, or boot anyway with "
            "`serve --skip-tests`.",
            fg=typer.colors.RED,
        )
        return False
    typer.secho("serve: test gate passed.", fg=typer.colors.GREEN)
    return True


@app.command()
def serve(
    config: str = CONFIG_OPT,
    host: str = "127.0.0.1",
    port: int = 8000,
    skip_tests: bool = typer.Option(
        False, "--skip-tests", help="Boot without running the pre-boot test gate."
    ),
):
    """Run the FastAPI backend + serve the built React frontend (frontend/dist).

    Runs the test suite first and refuses to boot if it fails (override with
    --skip-tests), so a broken API contract never silently ships to the browser.
    """
    import uvicorn

    from .api import create_app

    if not skip_tests and not _run_test_gate():
        raise typer.Exit(1)

    uvicorn.run(create_app(_load(config)), host=host, port=port)


if __name__ == "__main__":
    app()
