"""Annotation store: schema, the stable instance key, taxonomy, writes, agreement.

Everything here lives in `data/labels.duckdb` (`paths.labels_db_path`), a file that
NOTHING in the ingest path ever opens. `pipeline.ingest` calls `db.purge_dataset` +
`rmtree` on a re-upload, `api.delete_camera` purges one camera and
`db.archive_dataset` moves a whole day into the archive DB — annotator hours are
the one thing on the box that cannot be regenerated, so they are deliberately kept
outside every table those operations touch, and every annotation carries
denormalised provenance so it stays self-describing after its detection row is
gone.

`labeling.py` imports this module; **the reverse import is forbidden** — this file
owns the DDL, the key, the taxonomy and the writes, and `labeling.py` owns the
queue, the crop and the ATTACH context manager on top of them. See
docs/roadmap/M3_labeling.md §2-3, which freezes both halves.

The load-bearing pair is `instance_key()` (Python) and `instance_key_sql()` (a
DuckDB expression): the queue mints keys in SQL, the submit path re-derives them in
Python, and any divergence between the two silently rejects roughly half of all
submissions with a confusing 400. They are built from one recipe and pinned to each
other by `tests/test_labels_schema.py::test_python_and_sql_keys_agree`.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from datetime import date, datetime
from typing import Any, Iterable, Mapping, Sequence

import duckdb

# --------------------------------------------------------------------------- key

KEY_VERSION = "v1"
KEY_LEN = 32                 # chars of hex kept from the sha256 — 128 bits
_SEP = "\x1f"                # ASCII unit separator: impossible in a dataset id, a
                             # camera id under uploads.CAMERA_ID_RE, or a basename,
                             # so no component can bleed into its neighbour.
_SEP_SQL = "chr(31)"
_BACKSLASH_SQL = "chr(92)"   # written as chr() so no SQL/Python escaping ambiguity

# Alias of the `detections` relation the SQL expressions are built against. Trusted
# (it comes from our own callers, never a request), but interpolated into SQL, so
# it is whitelisted rather than assumed — the same discipline db.crosstab applies to
# feature SQL.
_ALIAS_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# The pixel fingerprint (§2.3): blake2b-128 over `size ⌷ first 64 KiB` of the JPEG.
# frame_path is deterministic in (dataset_id, camera_id, frame_idx), so re-uploading
# DIFFERENT footage for the same day reproduces identical path strings over
# completely different pixels; without this an exact key match would be a wrong-cow
# match made with full confidence.
FRAME_SIG_BYTES = 64 * 1024


def frame_basename(frame_path: str) -> str:
    """The filename component of a stored `frame_path`, split on BOTH separators.

    `frame_path` is `str(artifacts_dir/<ds>/frames/<cam>/<idx:08d>.jpg)` — CWD-
    relative and OS-native, so the same physical frame is a backslash string on the
    Windows dev box and a slash string in the container (`pipeline.py` records a
    shipped bug from exactly that). `os.path.basename` only splits on the *host's*
    separator, which would key one deployment's frames differently from the other's,
    so the split is done explicitly on both."""
    return frame_path.replace("\\", "/").rsplit("/", 1)[-1]


def _q(v: float) -> int:
    """Quantise a pixel coordinate: `floor(v + 0.5)`, never `round()`.

    Python's `round()` is banker's rounding (`round(0.5) == 0`) and DuckDB's is
    half-away-from-zero (`round(0.5) == 1`). One side of the key is minted in SQL
    and the other verified in Python, so a `round()` on either would reject about
    half of all half-pixel coordinates. `floor` also agrees on the negative
    coordinates Ultralytics emits unclamped."""
    return math.floor(float(v) + 0.5)


def instance_key(
    dataset_id: str | None,
    camera_id: str,
    frame_path: str,
    bbox: Sequence[float],
    ordinal: int = 0,
) -> str:
    """The stable, content-derived id of one detected animal (§2.2).

    `detections.detection_id` cannot serve: `DET_COLS` excludes it, so `clip_camera`
    → `restore_clip` re-mints a fresh id for the same physical cow, and a rebuilt
    main DB restarts `seq_det` at 1. A label keyed on it silently re-attaches to the
    WRONG animal after an undo-clip, with no error anywhere.

    Every input is instead a `DET_COLS` column that round-trips through
    `clipped_detections` bit-identically. `frame_path` is reduced to its basename
    (everything the basename discards is either already in the key or deployment
    config), coordinates are quantised with `_q`, and `ordinal` disambiguates rows
    that share a quantised box — `detections` has no PK and nothing forbids two.

    `bbox` is `[x1, y1, x2, y2]` in full-frame pixels; `frame_path` may be a full
    path or an already-reduced basename."""
    base = frame_basename(frame_path or "")
    if not base:
        raise ValueError(
            "cannot key a detection with an empty frame_path — this DB was ingested "
            "with `ingest.save_frames: false`, so its detections have no image on "
            "disk to identify, crop or label"
        )
    if not camera_id:
        raise ValueError("cannot key a detection with an empty camera_id")
    if len(bbox) != 4:
        raise ValueError(f"bbox must be [x1, y1, x2, y2], got {len(bbox)} values")
    parts = [
        KEY_VERSION,
        dataset_id or "",
        camera_id,
        base,
        *(str(_q(v)) for v in bbox),
        str(int(ordinal)),
    ]
    return hashlib.sha256(_SEP.join(parts).encode("utf-8")).hexdigest()[:KEY_LEN]


def instance_ordinal_sql(alias: str = "d") -> str:
    """The `ordinal` component of the key as a DuckDB window expression (§2.2).

    `dense_rank`, **not** `row_number`: rows tied on every ordering column are
    byte-identical in content and DuckDB's parallel scan may emit them in either
    order, so `row_number` would hand them arbitrary, non-reproducible ordinals and
    a surviving row could present as ordinal 0 while its only label was written
    under ordinal 1. `dense_rank` gives content-identical rows the same ordinal and
    therefore the same key — which is also semantically right, since two boxes
    identical to the pixel produce a crop an annotator physically cannot tell apart.

    Every ordering column is in `DET_COLS` and none is rewritten after insert
    (`db.update_region` / `update_shelter` / `update_posture` touch only `posture`,
    `region_id`, `under_panel`, `panel_id`), so the ordinal survives clip/restore.

    Being a window function it is only valid in a SELECT list; the queue computes it
    in a subquery and filters outside."""
    a = _prefix(alias)
    return (
        "(dense_rank() OVER ("
        f"PARTITION BY coalesce({a}dataset_id, ''), {a}camera_id, {a}frame_path, "
        f"{_q_sql(a + 'bbox_x1')}, {_q_sql(a + 'bbox_y1')}, "
        f"{_q_sql(a + 'bbox_x2')}, {_q_sql(a + 'bbox_y2')} "
        f"ORDER BY {a}score DESC NULLS LAST, {a}area_px DESC NULLS LAST, "
        f"{a}ground_px_x NULLS LAST, {a}ground_px_y NULLS LAST, {a}ts NULLS LAST"
        ") - 1)"
    )


def instance_key_sql(alias: str = "d") -> str:
    """`instance_key()` as a DuckDB expression over a `detections`-shaped relation.

    Byte-for-byte the same recipe as the Python producer, which is the only reason
    the queue (SQL) and the submit path (Python) can agree on what a cow is called.
    Three details carry that agreement and none may be "simplified":

    - each number is cast BIGINT then VARCHAR, so `12.0` never reaches the hash as
      `"12.0"` where Python would have written `"12"`;
    - the separator is `chr(31)`, matching `_SEP`;
    - the basename is taken with a regex over a slash-normalised path, because
      DuckDB's own path helpers split on the *server's* separator and would key a
      Windows-ingested frame differently from the same frame in the container.

    Contains a window function (the ordinal), so it belongs in a SELECT list."""
    a = _prefix(alias)
    material = f" || {_SEP_SQL} || ".join([
        f"'{KEY_VERSION}'",
        f"coalesce({a}dataset_id, '')",
        f"{a}camera_id",
        _basename_sql(a + "frame_path"),
        _q_sql(a + "bbox_x1"), _q_sql(a + "bbox_y1"),
        _q_sql(a + "bbox_x2"), _q_sql(a + "bbox_y2"),
        f"CAST({instance_ordinal_sql(alias)} AS VARCHAR)",
    ])
    return f"substr(sha256({material}), 1, {KEY_LEN})"


def frame_sig(path: str) -> str | None:
    """Pixel fingerprint of a frame file, or None when it is unreadable/gone.

    `O(1)` on a 4K JPEG and enough to separate two unrelated captures at the same
    path. None is an explicit third state, not a silent pass: `pipeline.ingest`
    rmtrees `artifacts/<dataset_id>` before re-indexing, so a missing file is
    routine, and the reconciler reports the NULL case as `attached_unverified`
    rather than attaching or rejecting on it."""
    try:
        with open(path, "rb") as f:
            head = f.read(FRAME_SIG_BYTES)
            size = f.seek(0, 2)
    except OSError:
        return None
    return frame_sig_of(size, head)


def frame_sig_of(size: int, head: bytes) -> str:
    """`frame_sig` from bytes the caller already has — the crop endpoint stats and
    opens every frame it serves, so it fingerprints for free rather than re-reading."""
    h = hashlib.blake2b(digest_size=16)
    h.update(f"{int(size)}".encode("utf-8"))
    h.update(_SEP.encode("utf-8"))
    h.update(head)
    return h.hexdigest()


def _prefix(alias: str) -> str:
    if alias and not _ALIAS_RE.match(alias):
        raise ValueError(f"invalid SQL alias {alias!r}")
    return f"{alias}." if alias else ""


def _q_sql(col: str) -> str:
    return f"CAST(CAST(floor({col} + 0.5) AS BIGINT) AS VARCHAR)"


def _basename_sql(col: str) -> str:
    return f"regexp_extract(replace({col}, {_BACKSLASH_SQL}, '/'), '[^/]*$')"


# ------------------------------------------------------------------------ domains

OUTCOMES: tuple[str, ...] = ("labeled", "skipped", "undone")

# A skip is a judgement about the instance, not a failure to answer: `multiple_cows`
# in particular is a direct signal that the crop padding or the detector merged two
# animals, which is why skips are annotations and not a 400.
SKIP_REASONS: tuple[str, ...] = ("bad_crop", "no_cow", "multiple_cows", "occluded", "other")

# The class-icon vocabulary. `label_classes.icon` stores a NAME from this tuple and
# nothing else. Powerusers add classes at runtime, so the icon cannot be hardcoded
# per class key in the frontend — but the value is rendered into the DOM by
# <ClassIcon>, so free text here would be a stored-XSS hole wearing a taxonomy hat,
# and a filename would be an asset request the deployment's strict CSP refuses. The
# names are deliberately generic (`probe`, not `head_probing`) so a hand-created
# class can reuse one. `dot` is the neutral member and the way to say "no icon";
# the frontend also falls back to it for NULL and for any name it does not know, so
# a value this list once contained can never break the renderer.
CLASS_ICONS: tuple[str, ...] = (
    "shade", "sun", "eye-off", "question", "grass", "lying", "standing", "probe", "dot",
)

EVENT_KINDS: tuple[str, ...] = (
    "session_start", "served", "submitted", "skipped", "undo", "relabel",
    "info_opened", "session_end",
    # Per-decision timing (M3_labeling_ux.md §6.1). `served` is written once per
    # BATCH, so the time it yields for item k of 8 includes items 1..k-1 — useful
    # as the abandonment denominator, useless as effort. `presented` fires when an
    # item actually reaches the screen and `answered` fires per QUESTION, which is
    # the only way to separate "how long did Sun exposure take" from "how long did
    # Behaviour take". The acceptance targets are stated per decision, so without
    # these two the redesign cannot be evaluated at all.
    #
    # These were emitted by the page before they were accepted here, so every one
    # 400'd and was dropped on the floor while the UI carried on looking healthy.
    "presented", "answered",
)

RECONCILE_STATES: tuple[str, ...] = (
    "attached", "attached_unverified", "aliased", "hijacked", "ambiguous_merge",
    "clipped", "archived", "orphan",
)

# Re-attachment thresholds (§2.4 step 2). The runner-up guard is what stops a looser
# NMS setting on re-ingest — which merges two adjacent cows into one box — from
# aliasing both source keys onto the merged target.
IOU_ATTACH = 0.70
IOU_RUNNER_UP = 0.50

META_REVISION = "taxonomy_revision"
META_SCHEMA_VERSION = "schema_version"
SCHEMA_VERSION = "1"

_GROUP_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CLASS_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,63}$")


def _valid_icon(icon: str | None) -> str | None:
    """`icon` normalised, or `ValueError` when it is not a CLASS_ICONS name.

    `None` passes through as None (the caller's "not given"/"leave unchanged"), which
    renders as the neutral dot — so an operator who wants no icon picks 'dot' rather
    than needing a null-out path. Everything else is rejected rather than stored and
    rendered: this string reaches the DOM, and the taxonomy editor is
    poweruser-writable at runtime."""
    if icon is None:
        return None
    name = icon.strip()
    if name not in CLASS_ICONS:
        raise ValueError(
            f"icon must be one of {list(CLASS_ICONS)}, got {icon!r} — icons are drawn "
            "from a fixed vocabulary by name, never supplied as markup or a filename"
        )
    return name


# ---------------------------------------------------------------------------- DDL

def init_labels_db(con: duckdb.DuckDBPyConnection) -> None:
    """Create/upgrade the label store. Idempotent; safe on every boot and CLI run.

    Follows `db.init_db`'s ordering discipline exactly — sequences, `CREATE TABLE IF
    NOT EXISTS`, the forward-compat `ALTER … ADD COLUMN IF NOT EXISTS` block,
    indexes, `CREATE OR REPLACE VIEW`, then the idempotent seed — because a boot
    that runs these out of order fails on a store created by an older build, and a
    DDL error here takes the WHOLE app down rather than just the Label page.

    No foreign keys anywhere, matching the rest of the repo and avoiding DuckDB's FK
    restrictions, which fight the soft-archive flow. Callers must open the file
    read-WRITE even for reports: DuckDB refuses a second connection to one file with
    a different mode in the same process, and that error text matches none of
    `db.connect`'s retry substrings, so a `read_only` open surfaces as an un-retried
    500."""
    for seq in ("seq_annotation", "seq_label_event", "seq_taxonomy_audit", "seq_backup_run"):
        con.execute(f"CREATE SEQUENCE IF NOT EXISTS {seq} START 1;")

    # A GROUP is a question ("Sun exposure"). Poweruser-editable at runtime and
    # never hard-deleted: a delete would orphan every stored answer and silently
    # change what the historical data means.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS label_groups (
            group_key    VARCHAR PRIMARY KEY,
            name         VARCHAR NOT NULL,
            description  VARCHAR,
            sort_order   INTEGER NOT NULL DEFAULT 100,
            multi_select BOOLEAN NOT NULL DEFAULT FALSE,
            required     BOOLEAN NOT NULL DEFAULT TRUE,
            active       BOOLEAN NOT NULL DEFAULT TRUE,
            archived_at  TIMESTAMP,
            created_by   VARCHAR, created_at TIMESTAMP DEFAULT now(),
            updated_by   VARCHAR, updated_at TIMESTAMP
        );
        """
    )
    # A CLASS is an option inside a group ("Shaded"). `description` is NOT NULL: an
    # option with no written definition is the single largest source of annotator
    # disagreement, which is the whole reason the (i) icon exists. `icon` is nullable
    # and holds a NAME from CLASS_ICONS — never markup, never an asset path (see
    # CLASS_ICONS for why); NULL renders as the neutral dot.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS label_classes (
            class_key    VARCHAR PRIMARY KEY,
            group_key    VARCHAR NOT NULL,
            name         VARCHAR NOT NULL,
            description  VARCHAR NOT NULL,
            icon         VARCHAR,
            sort_order   INTEGER NOT NULL DEFAULT 100,
            is_escape    BOOLEAN NOT NULL DEFAULT FALSE,
            active       BOOLEAN NOT NULL DEFAULT TRUE,
            archived_at  TIMESTAMP,
            created_by   VARCHAR, created_at TIMESTAMP DEFAULT now(),
            updated_by   VARCHAR, updated_at TIMESTAMP
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS taxonomy_audit (
            audit_id    BIGINT PRIMARY KEY DEFAULT nextval('seq_taxonomy_audit'),
            "at"        TIMESTAMP DEFAULT now(),   -- quoted: AT is a DuckDB keyword
            actor       VARCHAR,
            actor_role  VARCHAR,
            action      VARCHAR,
            target_kind VARCHAR,
            target_key  VARCHAR,
            before_json VARCHAR,
            after_json  VARCHAR,
            revision    BIGINT
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS label_meta (
            key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP DEFAULT now()
        );
        """
    )
    # One row per (instance, annotator, submission). APPEND-ONLY: the product of this
    # feature IS annotator variability, so an overwrite would destroy the signal it
    # exists to measure, and an annotator changing their mind after a description is
    # sharpened is a measurement (intra-rater reliability), not a correction.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS annotations (
            annotation_id   BIGINT PRIMARY KEY DEFAULT nextval('seq_annotation'),
            instance_key    VARCHAR NOT NULL,
            effective_key   VARCHAR NOT NULL,
            key_version     VARCHAR NOT NULL DEFAULT 'v1',
            annotator       VARCHAR NOT NULL,
            version         INTEGER NOT NULL DEFAULT 1,
            superseded_at   TIMESTAMP,
            outcome         VARCHAR NOT NULL DEFAULT 'labeled',
            skip_reason     VARCHAR,
            flag_note       VARCHAR,
            dataset_id      VARCHAR,
            camera_id       VARCHAR,
            frame_path      VARCHAR,
            frame_basename  VARCHAR,
            frame_sig       VARCHAR,
            ts              TIMESTAMP,
            bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_x2 DOUBLE, bbox_y2 DOUBLE,
            ordinal         INTEGER NOT NULL DEFAULT 0,
            det_score       DOUBLE,
            session_id      VARCHAR,
            serve_event_id  BIGINT,
            served_at       TIMESTAMP,
            submitted_at    TIMESTAMP DEFAULT now(),
            time_on_task_ms BIGINT,
            client_elapsed_ms BIGINT,
            input_mode      VARCHAR,
            annotator_role      VARCHAR,
            annotator_real_role VARCHAR,
            acting_preview  BOOLEAN DEFAULT FALSE,
            auth_disabled   BOOLEAN DEFAULT FALSE,
            app_version     VARCHAR,
            taxonomy_revision BIGINT,
            client_info     VARCHAR,
            viewport        VARCHAR,
            queue_reason    VARCHAR,
            UNIQUE (instance_key, annotator, version)
        );
        """
    )
    # One row per selected class, so a multi-select group needs no schema change.
    # group_key and class_name are denormalised on purpose: reports never join the
    # taxonomy, and the snapshotted name survives a poweruser rename.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS annotation_choices (
            annotation_id BIGINT  NOT NULL,
            class_key     VARCHAR NOT NULL,
            group_key     VARCHAR NOT NULL,
            class_name    VARCHAR,
            ordinal       INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (annotation_id, class_key)
        );
        """
    )
    # Append-only effort telemetry. 'served' is the non-forgeable time-on-task clock:
    # it is written server-side by the queue, so a client cannot claim a shorter
    # elapsed time than it actually took.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS label_events (
            event_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_label_event'),
            "at"         TIMESTAMP DEFAULT now(),
            session_id   VARCHAR,
            annotator    VARCHAR,
            kind         VARCHAR NOT NULL,
            instance_key VARCHAR,
            class_key    VARCHAR,
            detail       VARCHAR
        );
        """
    )
    # `UNIQUE (new_key, dataset_id)` enforces the injective alias mapping in the
    # STORE, not only in reconcile_dataset's code: a merged target claimed by two
    # source keys presents later as two annotators disagreeing about "the same"
    # instance, which is a fabricated disagreement that drags kappa down and
    # conflates two animals irrecoverably.
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS instance_key_aliases (
            old_key        VARCHAR NOT NULL,
            new_key        VARCHAR NOT NULL,
            reason         VARCHAR,
            iou            DOUBLE,
            dataset_id     VARCHAR,
            camera_id      VARCHAR,
            frame_basename VARCHAR,
            created_at     TIMESTAMP DEFAULT now(),
            created_by     VARCHAR,
            PRIMARY KEY (old_key, new_key),
            UNIQUE (new_key, dataset_id)
        );
        """
    )
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS reconciliations (
            run_id       VARCHAR NOT NULL,
            "at"         TIMESTAMP DEFAULT now(),
            dataset_id   VARCHAR,
            instance_key VARCHAR NOT NULL,
            state        VARCHAR NOT NULL,
            new_key      VARCHAR,
            iou          DOUBLE,
            detail       VARCHAR,
            PRIMARY KEY (run_id, instance_key)
        );
        """
    )
    # Written by labels_backup, declared here so the watermark travels inside the
    # very backup it describes — a snapshot that did not contain its own run history
    # would restore as "never backed up" and re-post everything.
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

    # Forward-compat slot (the db.init_db idiom): a store created by an earlier build
    # gets the column added rather than a broken query. `effective_key` cannot be
    # re-added NOT NULL to a populated table, so it arrives nullable and is
    # backfilled from `instance_key` — which is exactly its value at insert.
    con.execute("ALTER TABLE annotations ADD COLUMN IF NOT EXISTS effective_key VARCHAR")
    con.execute("ALTER TABLE annotations ADD COLUMN IF NOT EXISTS frame_sig VARCHAR")
    con.execute("ALTER TABLE annotations ADD COLUMN IF NOT EXISTS queue_reason VARCHAR")
    con.execute("UPDATE annotations SET effective_key = instance_key WHERE effective_key IS NULL")
    # An existing data/labels.duckdb upgrades in place here — there is no migration
    # step for this store and a boot that needed one would take the WHOLE app down,
    # not just the Label page. The seed below backfills the shipped icons onto rows
    # where this arrives NULL; it deliberately does not DEFAULT to anything, because
    # NULL is how "nobody has picked an icon yet" is told apart from a deliberate
    # neutral 'dot'.
    con.execute("ALTER TABLE label_classes ADD COLUMN IF NOT EXISTS icon VARCHAR")

    # PK/UNIQUE already build ART indexes; these cover the remaining hot paths — the
    # queue's anti-join on effective_key above all.
    for name, table, cols in (
        ("idx_ann_effective", "annotations", "effective_key"),
        ("idx_ann_annotator", "annotations", "annotator"),
        ("idx_ann_submitted", "annotations", "submitted_at"),
        ("idx_ann_dataset", "annotations", "dataset_id"),
        ("idx_choice_ann", "annotation_choices", "annotation_id"),
        ("idx_choice_class", "annotation_choices", "class_key"),
        ("idx_event_at", "label_events", '"at"'),
        ("idx_alias_old", "instance_key_aliases", "old_key"),
    ):
        con.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({cols})")

    # CREATE OR REPLACE, not IF NOT EXISTS: a view is derived, so a code change to
    # its body must take effect on the next boot rather than staying pinned to
    # whatever shipped first.
    con.execute(
        """
        CREATE OR REPLACE VIEW v_current_annotations AS
        SELECT * FROM annotations WHERE superseded_at IS NULL AND outcome <> 'undone';
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW v_current_answers AS
        SELECT a.annotation_id, a.effective_key, a.instance_key, a.annotator, a.version,
               a.dataset_id, a.camera_id, a.ts, a.submitted_at, a.time_on_task_ms,
               a.acting_preview, a.auth_disabled, a.taxonomy_revision,
               c.group_key, c.class_key, c.class_name
        FROM annotations a JOIN annotation_choices c USING (annotation_id)
        WHERE a.superseded_at IS NULL AND a.outcome = 'labeled';
        """
    )
    # Labeled and skipped are counted SEPARATELY. Counting a skip as coverage would
    # retire exactly the ambiguous instances where inter-rater variability is most
    # informative, biasing the corpus toward easy cases and inflating kappa.
    con.execute(
        """
        CREATE OR REPLACE VIEW v_instance_coverage AS
        SELECT effective_key,
               count(DISTINCT annotator) FILTER (WHERE outcome = 'labeled') AS n_annotators_labeled,
               count(DISTINCT annotator) FILTER (WHERE outcome = 'skipped') AS n_annotators_skipped,
               count(*) FILTER (WHERE outcome = 'labeled') AS n_labeled,
               count(*) FILTER (WHERE outcome = 'skipped') AS n_skipped,
               max(submitted_at) AS last_submitted_at
        FROM annotations WHERE superseded_at IS NULL GROUP BY effective_key;
        """
    )
    con.execute(
        """
        CREATE OR REPLACE VIEW v_effective_key AS
        SELECT annotation_id, instance_key AS label_key, effective_key,
               instance_key <> effective_key AS reattached
        FROM annotations;
        """
    )

    con.execute(
        "INSERT INTO label_meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO NOTHING",
        [META_SCHEMA_VERSION, SCHEMA_VERSION],
    )
    seed_taxonomy(con)


# --------------------------------------------------------------------------- meta

def get_meta(con: duckdb.DuckDBPyConnection, key: str) -> str | None:
    row = con.execute("SELECT value FROM label_meta WHERE key = ?", [key]).fetchone()
    return row[0] if row else None


def set_meta(con: duckdb.DuckDBPyConnection, key: str, value: str) -> None:
    con.execute(
        "INSERT INTO label_meta (key, value, updated_at) VALUES (?, ?, now()) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = now()",
        [key, str(value)],
    )


def taxonomy_revision(con: duckdb.DuckDBPyConnection) -> int:
    """Monotonic counter bumped by every taxonomy change.

    Stamped on every annotation, so a later report can ask "did agreement improve
    after we rewrote the Feeding description?", and echoed by the client on submit so
    the server can 409 a stale in-flight answer instead of 400ing it forever."""
    raw = get_meta(con, META_REVISION)
    return int(raw) if raw else 0


# ----------------------------------------------------------------------- taxonomy

# The shipped taxonomy (§3.4). The descriptions are the annotator-facing definitions
# and are deliberately long: an option with no written definition is the single
# largest source of disagreement, and these were written against real frames. Every
# group carries a "Cannot tell" escape, because a forced guess is noise that looks
# like disagreement. Every class also carries a CLASS_ICONS name: an annotator
# answering by number key spots a shape long before they read a word, and the icon is
# what keeps the option list scannable at speed rather than re-read per instance.
SEED_GROUPS: tuple[dict[str, Any], ...] = (
    {
        "group_key": "sun_exposure",
        "name": "Sun exposure",
        "sort_order": 10,
        "multi_select": False,
        "required": True,
        "description": (
            "Where the RINGED animal's body is relative to shade at this instant. "
            "Judge the body, not the head or the legs, and judge only the ringed "
            "animal — other cows in the crop are context. This is the question the "
            "whole agrivoltaics study turns on: do the panels actually get used as "
            "shade?"
        ),
        "classes": (
            {
                "class_key": "sun_exposure.shaded",
                "name": "Shaded",
                "icon": "shade",
                "sort_order": 10,
                "is_escape": False,
                "description": "Most of the body sits in panel shade, not just overcast dullness.",
            },
            {
                "class_key": "sun_exposure.direct_sun",
                "name": "Direct sun",
                "icon": "sun",
                "sort_order": 20,
                "is_escape": False,
                "description": "Most of the body is in open sun, casting its own sharp shadow.",
            },
            {
                "class_key": "sun_exposure.not_visible",
                "name": "Not visible",
                "icon": "eye-off",
                "sort_order": 30,
                "is_escape": False,
                "description": "Too little of the body is visible to judge: cut off, blocked, or blown out.",
            },
            {
                "class_key": "sun_exposure.cannot_tell",
                "name": "Cannot tell",
                "icon": "question",
                "sort_order": 40,
                "is_escape": True,
                "description": "The body is visible, but sun versus shade will not resolve.",
            },
        ),
    },
    {
        "group_key": "behaviour",
        "name": "Behaviour",
        "sort_order": 20,
        "multi_select": False,
        "required": True,
        "description": (
            "What the RINGED animal is doing in this single frame. You are looking at "
            "a timelapse still, not video, so judge geometry — head height, leg line, "
            "what the muzzle is touching — never inferred motion. Pick the one class "
            "that fits the completed posture; do not label an intention."
        ),
        "classes": (
            {
                "class_key": "behaviour.feeding",
                "name": "Feeding",
                "icon": "grass",
                "sort_order": 10,
                "is_escape": False,
                "description": "Muzzle down at grass or feed while standing, not at hardware.",
            },
            {
                "class_key": "behaviour.lying",
                "name": "Lying",
                "icon": "lying",
                "sort_order": 20,
                "is_escape": False,
                "description": "Belly on the ground, with no daylight under the barrel.",
            },
            {
                "class_key": "behaviour.standing",
                "name": "Standing",
                "icon": "standing",
                "sort_order": 30,
                "is_escape": False,
                "description": "On all four feet, head at or above the shoulder, not eating.",
            },
            {
                "class_key": "behaviour.head_probing",
                "name": "Head probing",
                "icon": "probe",
                "sort_order": 40,
                "is_escape": False,
                "description": "Head down at hardware, ground, or water, investigating rather than eating.",
            },
            {
                "class_key": "behaviour.not_visible",
                "name": "Not visible",
                "icon": "eye-off",
                "sort_order": 45,
                "is_escape": False,
                "description": "Too little of the body is visible to read the posture.",
            },
            {
                "class_key": "behaviour.cannot_tell",
                "name": "Cannot tell",
                "icon": "question",
                "sort_order": 50,
                "is_escape": True,
                "description": "The body is visible, but the pose will not resolve.",
            },
        ),
    },
)


def seed_taxonomy(
    con: duckdb.DuckDBPyConnection,
    *,
    force: bool = False,
    actor: str = "system",
    actor_role: str | None = None,
) -> int:
    """Insert the shipped taxonomy, once. Returns the number of rows written.

    A group or class is inserted only when its KEY HAS NEVER EXISTED, and an
    existing row is never touched. Presence-of-key, not `active = TRUE`: a class a
    poweruser archived must stay archived across every reboot, and a description they
    sharpened after watching annotators disagree must never be reverted by a
    container restart.

    The ONE in-place write on an existing row is the icon backfill, and it is guarded
    by `icon IS NULL` for exactly the same reason: a store created before the icon
    column existed must get the shipped icons without a migration step, while an icon
    a poweruser has since picked is an operator edit and is left alone. The candidate
    set is read BEFORE the insert loop, so a row inserted below (which already carries
    its icon) is never also counted as a backfill.

    The revision is bumped (and an audit row written) only when something actually
    changed — inserts or backfilled icons. Bumping unconditionally would make every
    boot 409 every in-flight submission. The count returned covers both, which is what
    `cownting labels reseed` prints as "rows touched".

    `force` is the escape hatch behind `cownting labels reseed --force`: it refreshes
    names, descriptions, icons and ordering from this module onto existing keys — but
    never `active`/`archived_at`, because reverting an operator's archive decision is
    exactly what the presence-of-key rule exists to prevent — and always audits and
    bumps, so before/after agreement stays comparable."""
    have_groups = {r[0] for r in con.execute("SELECT group_key FROM label_groups").fetchall()}
    have_classes = {r[0] for r in con.execute("SELECT class_key FROM label_classes").fetchall()}
    iconless = {r[0] for r in con.execute(
        "SELECT class_key FROM label_classes WHERE icon IS NULL").fetchall()}
    written = 0
    icons = 0
    for g in SEED_GROUPS:
        if g["group_key"] not in have_groups:
            con.execute(
                "INSERT INTO label_groups (group_key, name, description, sort_order, "
                "multi_select, required, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, now())",
                [g["group_key"], g["name"], g["description"], g["sort_order"],
                 g["multi_select"], g["required"], actor],
            )
            written += 1
        elif force:
            con.execute(
                "UPDATE label_groups SET name = ?, description = ?, sort_order = ?, "
                "multi_select = ?, required = ?, updated_by = ?, updated_at = now() "
                "WHERE group_key = ?",
                [g["name"], g["description"], g["sort_order"], g["multi_select"],
                 g["required"], actor, g["group_key"]],
            )
        for c in g["classes"]:
            if c["class_key"] not in have_classes:
                con.execute(
                    "INSERT INTO label_classes (class_key, group_key, name, description, "
                    "icon, sort_order, is_escape, created_by, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
                    [c["class_key"], g["group_key"], c["name"], c["description"],
                     _valid_icon(c["icon"]), c["sort_order"], c["is_escape"], actor],
                )
                written += 1
                continue
            if c["class_key"] in iconless:
                # `AND icon IS NULL` is repeated in SQL, not left to the set read
                # above: a poweruser picking an icon between the read and here must
                # win, and losing that race would silently overwrite their choice.
                con.execute(
                    "UPDATE label_classes SET icon = ? WHERE class_key = ? AND icon IS NULL",
                    [_valid_icon(c["icon"]), c["class_key"]],
                )
                icons += 1
            if force:
                con.execute(
                    "UPDATE label_classes SET name = ?, description = ?, icon = ?, "
                    "sort_order = ?, is_escape = ?, updated_by = ?, updated_at = now() "
                    "WHERE class_key = ?",
                    [c["name"], c["description"], _valid_icon(c["icon"]),
                     c["sort_order"], c["is_escape"], actor, c["class_key"]],
                )
    if written or icons or force:
        _audit(con, actor=actor, actor_role=actor_role, action="seed",
               target_kind="group", target_key=None, before=None,
               after={"written": written, "icons": icons, "force": force})
    return written + icons


def taxonomy(con: duckdb.DuckDBPyConnection, *, include_archived: bool = False) -> dict[str, Any]:
    """The questions and their options, ordered, plus the current revision.

    `include_archived=False` is what the Label page reads; the poweruser editor
    passes True, because restoring an archived class means seeing it first."""
    gwhere = "" if include_archived else " WHERE active"
    cwhere = "" if include_archived else " WHERE active"
    groups = _rows(con.execute(
        "SELECT group_key, name, description, sort_order, multi_select, required, "
        "active, archived_at FROM label_groups"
        f"{gwhere} ORDER BY sort_order, group_key"
    ))
    classes = _rows(con.execute(
        "SELECT class_key, group_key, name, description, icon, sort_order, is_escape, "
        "active, archived_at FROM label_classes"
        f"{cwhere} ORDER BY group_key, sort_order, class_key"
    ))
    by_group: dict[str, list[dict[str, Any]]] = {}
    for c in classes:
        by_group.setdefault(str(c["group_key"]), []).append(c)
    for g in groups:
        g["classes"] = by_group.get(str(g["group_key"]), [])
    return {"revision": taxonomy_revision(con), "groups": groups}


def known_classes(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    """Every class key that has EVER existed, active or not, with its group and name.

    Submit validates against this, never against the active set: a poweruser
    archiving a class mid-session must not 400 an answer already on the annotator's
    screen. Staleness is handled by the revision check (409), which is recoverable;
    a 400 is not."""
    return {
        str(r["class_key"]): {"group_key": r["group_key"], "name": r["name"],
                             "is_escape": r["is_escape"], "active": r["active"]}
        for r in _rows(con.execute(
            "SELECT class_key, group_key, name, is_escape, active FROM label_classes"
        ))
    }


def resolve_choices(
    con: duckdb.DuckDBPyConnection,
    answers: Mapping[str, Any],
    *,
    require_complete: bool = True,
) -> list[dict[str, Any]]:
    """Turn a submit body's `{group_key: class_key | [class_key]}` into choice rows.

    Raises `ValueError` on an unknown class, a class that does not belong to the
    group it was answered under, more than one selection in a single-select group, or
    (when `require_complete`) an unanswered required group. Completeness is checked
    against the CURRENT taxonomy, which is only sound because the caller has already
    409'd a revision skew — validating completeness against current state while
    accepting a stale revision is the trap that would 400 every in-flight submit
    forever after a poweruser adds a required group.

    `class_name` is snapshotted here, at label time, so a later rename does not
    rewrite what the annotator actually chose."""
    known = known_classes(con)
    groups = {str(g["group_key"]): g for g in taxonomy(con)["groups"]}
    out: list[dict[str, Any]] = []
    for group_key, picked in answers.items():
        # A repeated class_key is deduped, not rejected: annotation_choices is keyed
        # (annotation_id, class_key), so the same option sent twice by a jittery
        # client would otherwise abort the whole submit on a PK violation that the
        # caller cannot distinguish from the version race.
        keys = [picked] if isinstance(picked, str) else list(picked or [])
        g = groups.get(group_key)
        if g is not None and not g["multi_select"] and len(keys) > 1:
            raise ValueError(f"group {group_key!r} is single-select but got {len(keys)} answers")
        for i, class_key in enumerate(dict.fromkeys(keys)):
            meta = known.get(class_key)
            if meta is None:
                raise ValueError(f"unknown class {class_key!r}")
            if meta["group_key"] != group_key:
                raise ValueError(
                    f"class {class_key!r} belongs to group {meta['group_key']!r}, "
                    f"not {group_key!r}"
                )
            out.append({"class_key": class_key, "group_key": group_key,
                        "class_name": meta["name"], "ordinal": i})
    if require_complete:
        answered = {c["group_key"] for c in out}
        missing = [k for k, g in groups.items() if g["required"] and k not in answered]
        if missing:
            raise ValueError("unanswered required group(s): " + ", ".join(sorted(missing)))
    return out


def create_group(
    con: duckdb.DuckDBPyConnection,
    *,
    group_key: str,
    name: str,
    description: str | None = None,
    multi_select: bool = False,
    required: bool = True,
    sort_order: int | None = None,
    actor: str = "system",
    actor_role: str | None = None,
) -> dict[str, Any]:
    """Add a question. Returns the whole taxonomy (the Admin.tsx idiom: the client
    replaces its state wholesale, so there is nothing optimistic to reconcile)."""
    if not _GROUP_KEY_RE.match(group_key or ""):
        raise ValueError(
            "group_key must be lowercase letters, digits or _ (no dot: class keys "
            "are '<group_key>.<slug>' and the dot is the separator)"
        )
    if not (name or "").strip():
        raise ValueError("a group needs a name")
    if _exists(con, "label_groups", "group_key", group_key):
        raise ValueError(f"group {group_key!r} already exists")
    if sort_order is None:
        sort_order = _next_sort(con, "label_groups")
    con.execute(
        "INSERT INTO label_groups (group_key, name, description, sort_order, "
        "multi_select, required, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, now())",
        [group_key, name.strip(), description, sort_order, bool(multi_select),
         bool(required), actor],
    )
    _audit(con, actor=actor, actor_role=actor_role, action="create_group",
           target_kind="group", target_key=group_key, before=None,
           after=_one(con, "label_groups", "group_key", group_key))
    return taxonomy(con, include_archived=True)


def update_group(
    con: duckdb.DuckDBPyConnection,
    group_key: str,
    *,
    name: str | None = None,
    description: str | None = None,
    sort_order: int | None = None,
    multi_select: bool | None = None,
    required: bool | None = None,
    active: bool | None = None,
    actor: str = "system",
    actor_role: str | None = None,
) -> dict[str, Any]:
    """Edit a question. `None` means "leave unchanged" (the `db.upsert_dataset`
    coalesce idiom). `active=False` archives and `active=True` restores — there is no
    delete anywhere in this feature, because a hard delete would orphan every stored
    answer and silently change what the historical data means."""
    before = _one(con, "label_groups", "group_key", group_key)
    if before is None:
        raise ValueError(f"unknown group {group_key!r}")
    sets, params = _set_clause({
        "name": name.strip() if isinstance(name, str) else None,
        "description": description,
        "sort_order": sort_order,
        "multi_select": multi_select,
        "required": required,
        "active": active,
    })
    if active is not None:
        sets.append("archived_at = " + ("NULL" if active else "now()"))
    if not sets:
        return taxonomy(con, include_archived=True)
    sets += ["updated_by = ?", "updated_at = now()"]
    params.append(actor)
    con.execute(f"UPDATE label_groups SET {', '.join(sets)} WHERE group_key = ?",
                params + [group_key])
    action = "update_group" if active is None else ("restore_group" if active else "archive_group")
    _audit(con, actor=actor, actor_role=actor_role, action=action, target_kind="group",
           target_key=group_key, before=before,
           after=_one(con, "label_groups", "group_key", group_key))
    return taxonomy(con, include_archived=True)


def move_group(
    con: duckdb.DuckDBPyConnection,
    group_key: str,
    direction: str,
    *,
    actor: str = "system",
    actor_role: str | None = None,
) -> dict[str, Any]:
    """Reorder a question one slot up or down. `sort_order` is ALSO the hotkey row
    index, so this changes annotators' muscle memory — the editor shows a live key
    preview for that reason.

    The whole list is renumbered densely rather than swapping two values, because
    `sort_order` defaults to 100 for every hand-created group and a swap between two
    rows that share a value is a no-op the user experiences as a broken button."""
    order = [str(r[0]) for r in con.execute(
        "SELECT group_key FROM label_groups ORDER BY sort_order, group_key"
    ).fetchall()]
    _reorder(con, "label_groups", "group_key", order, group_key, direction, actor)
    _audit(con, actor=actor, actor_role=actor_role, action="move", target_kind="group",
           target_key=group_key, before={"order": order},
           after={"dir": direction, "order": [str(r[0]) for r in con.execute(
               "SELECT group_key FROM label_groups ORDER BY sort_order, group_key").fetchall()]})
    return taxonomy(con, include_archived=True)


def create_class(
    con: duckdb.DuckDBPyConnection,
    group_key: str,
    *,
    name: str,
    description: str,
    class_key: str | None = None,
    slug: str | None = None,
    icon: str | None = None,
    is_escape: bool = False,
    sort_order: int | None = None,
    actor: str = "system",
    actor_role: str | None = None,
) -> dict[str, Any]:
    """Add an option to a question.

    `description` is required and refused when blank — server-side, not only in the
    editor's disabled button. An option with no written definition is the single
    largest source of annotator disagreement, and it is the reason the (i) icon
    exists at all.

    `icon` is optional and must name a CLASS_ICONS member; omitted, it stays NULL and
    the option renders with the neutral dot. It is never free text (`_valid_icon`)."""
    if _one(con, "label_groups", "group_key", group_key) is None:
        raise ValueError(f"unknown group {group_key!r}")
    if not (name or "").strip():
        raise ValueError("a class needs a name")
    if not (description or "").strip():
        raise ValueError(
            "a class needs a description — it is what the annotator reads behind the "
            "(i) icon, and an undefined option is the largest source of disagreement"
        )
    if class_key is None:
        slug = slug or re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
        if not _CLASS_SLUG_RE.match(slug or ""):
            raise ValueError("class slug must be lowercase letters, digits or _")
        class_key = f"{group_key}.{slug}"
    if not class_key.startswith(f"{group_key}."):
        raise ValueError(f"class_key must be '{group_key}.<slug>', got {class_key!r}")
    if _exists(con, "label_classes", "class_key", class_key):
        raise ValueError(f"class {class_key!r} already exists")
    if sort_order is None:
        sort_order = _next_sort(con, "label_classes", "group_key", group_key)
    con.execute(
        "INSERT INTO label_classes (class_key, group_key, name, description, icon, "
        "sort_order, is_escape, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, now())",
        [class_key, group_key, name.strip(), description.strip(), _valid_icon(icon),
         sort_order, bool(is_escape), actor],
    )
    _audit(con, actor=actor, actor_role=actor_role, action="create_class",
           target_kind="class", target_key=class_key, before=None,
           after=_one(con, "label_classes", "class_key", class_key))
    return taxonomy(con, include_archived=True)


def update_class(
    con: duckdb.DuckDBPyConnection,
    class_key: str,
    *,
    name: str | None = None,
    description: str | None = None,
    icon: str | None = None,
    sort_order: int | None = None,
    is_escape: bool | None = None,
    active: bool | None = None,
    actor: str = "system",
    actor_role: str | None = None,
) -> dict[str, Any]:
    """Edit an option. `None` means "leave unchanged"; `active` archives/restores.

    Archiving is safe by construction: `annotation_choices` snapshots `class_name` at
    label time and `v_current_answers` reads that snapshot, so an answer given before
    the archive keeps rendering with the name the annotator actually saw.

    `icon` must name a CLASS_ICONS member; 'dot' is how an operator clears one back to
    neutral, since `None` here already means "leave unchanged"."""
    before = _one(con, "label_classes", "class_key", class_key)
    if before is None:
        raise ValueError(f"unknown class {class_key!r}")
    if description is not None and not description.strip():
        raise ValueError("a class description cannot be blanked")
    sets, params = _set_clause({
        "name": name.strip() if isinstance(name, str) else None,
        "description": description.strip() if isinstance(description, str) else None,
        "icon": _valid_icon(icon),
        "sort_order": sort_order,
        "is_escape": is_escape,
        "active": active,
    })
    if active is not None:
        sets.append("archived_at = " + ("NULL" if active else "now()"))
    if not sets:
        return taxonomy(con, include_archived=True)
    sets += ["updated_by = ?", "updated_at = now()"]
    params.append(actor)
    con.execute(f"UPDATE label_classes SET {', '.join(sets)} WHERE class_key = ?",
                params + [class_key])
    action = "update_class" if active is None else ("restore_class" if active else "archive_class")
    _audit(con, actor=actor, actor_role=actor_role, action=action, target_kind="class",
           target_key=class_key, before=before,
           after=_one(con, "label_classes", "class_key", class_key))
    return taxonomy(con, include_archived=True)


def move_class(
    con: duckdb.DuckDBPyConnection,
    class_key: str,
    direction: str,
    *,
    actor: str = "system",
    actor_role: str | None = None,
) -> dict[str, Any]:
    """Reorder an option within its group (see `move_group` for the renumbering)."""
    row = _one(con, "label_classes", "class_key", class_key)
    if row is None:
        raise ValueError(f"unknown class {class_key!r}")
    order = [str(r[0]) for r in con.execute(
        "SELECT class_key FROM label_classes WHERE group_key = ? ORDER BY sort_order, class_key",
        [row["group_key"]],
    ).fetchall()]
    _reorder(con, "label_classes", "class_key", order, class_key, direction, actor)
    _audit(con, actor=actor, actor_role=actor_role, action="move", target_kind="class",
           target_key=class_key, before={"order": order}, after={"dir": direction})
    return taxonomy(con, include_archived=True)


def _audit(
    con: duckdb.DuckDBPyConnection,
    *,
    actor: str,
    actor_role: str | None,
    action: str,
    target_kind: str,
    target_key: str | None,
    before: Mapping[str, Any] | None,
    after: Mapping[str, Any] | None,
) -> int:
    """Record a taxonomy change and bump the revision. Returns the new revision.

    The bump and the audit row are one operation on purpose: a change that moved the
    taxonomy without moving the revision would leave every open client submitting
    against a taxonomy it can no longer be told apart from."""
    rev = taxonomy_revision(con) + 1
    set_meta(con, META_REVISION, str(rev))
    con.execute(
        "INSERT INTO taxonomy_audit (actor, actor_role, action, target_kind, target_key, "
        "before_json, after_json, revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [actor, actor_role, action, target_kind, target_key,
         _dumps(before), _dumps(after), rev],
    )
    return rev


def _reorder(
    con: duckdb.DuckDBPyConnection, table: str, key_col: str, order: list[str],
    key: str, direction: str, actor: str,
) -> None:
    if direction not in ("up", "down"):
        raise ValueError("dir must be 'up' or 'down'")
    if key not in order:
        raise ValueError(f"unknown {key_col} {key!r}")
    i = order.index(key)
    j = i - 1 if direction == "up" else i + 1
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
    for pos, k in enumerate(order):
        con.execute(
            f"UPDATE {table} SET sort_order = ?, updated_by = ?, updated_at = now() "
            f"WHERE {key_col} = ?",
            [(pos + 1) * 10, actor, k],
        )


# ------------------------------------------------------------------------- writes

# Accepted keys of submit_annotation's two loose dicts. Declared so a typo raises
# instead of silently dropping the provenance that makes an orphaned label readable.
PROVENANCE_COLS: tuple[str, ...] = (
    "dataset_id", "camera_id", "frame_path", "frame_basename", "frame_sig", "ts",
    "bbox_x1", "bbox_y1", "bbox_x2", "bbox_y2", "ordinal", "det_score",
)
TELEMETRY_COLS: tuple[str, ...] = (
    "skip_reason", "flag_note", "session_id", "serve_event_id", "client_elapsed_ms",
    "input_mode", "annotator_role", "annotator_real_role", "acting_preview",
    "auth_disabled", "app_version", "taxonomy_revision", "client_info", "viewport",
    "queue_reason",
)


def submit_annotation(
    con: duckdb.DuckDBPyConnection,
    *,
    instance_key: str,
    effective_key: str | None = None,
    annotator: str,
    outcome: str = "labeled",
    choices: Sequence[Mapping[str, Any]] = (),
    provenance: Mapping[str, Any] | None = None,
    telemetry: Mapping[str, Any] | None = None,
) -> int:
    """Append one submission and supersede the annotator's previous answer.

    Never an overwrite: `version` is `n + 1` and the prior current row is stamped
    `superseded_at`. Append-only is what makes the store safe to back up
    incrementally by `submitted_at`, removes read-modify-write from the hot write
    path, and — the actual reason — preserves the annotator variability this whole
    feature exists to measure.

    Concurrency is optimistic. Two racing submits both compute `version = n + 1`, one
    loses the `UNIQUE (instance_key, annotator, version)` race and this raises
    `ValueError`, which `api.py` maps to 409 at the existing boundary.

    The whole function runs in an explicit `BEGIN`/`COMMIT`/`ROLLBACK` — a
    half-written annotation with no choice rows would look like an empty answer
    forever — so it must only ever be called on a fresh per-request labels
    connection, never nested inside another transaction.

    `provenance` carries the denormalised `DET_COLS` columns (see `PROVENANCE_COLS`)
    that keep the label self-describing after its detection row is purged.
    `telemetry` carries everything the submission itself reported (`TELEMETRY_COLS`),
    including `skip_reason` and `flag_note`, both of which are judgements about the
    act of answering rather than facts about the instance. `choices` is a sequence of
    `{class_key, group_key, class_name[, ordinal]}` — use `resolve_choices` to build
    it from the request body.

    Superseding matches on `effective_key`, while versioning matches on
    `instance_key`: after a reconciliation moved a label, "my current answer about
    this cow" is an `effective_key` question, but the UNIQUE — and therefore the race
    detection — is declared on `instance_key`. Returns the new `annotation_id`."""
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome must be one of {OUTCOMES}, got {outcome!r}")
    if not annotator:
        raise ValueError("an annotation needs an annotator")
    prov = _picked(provenance, PROVENANCE_COLS, "provenance")
    tel = _picked(telemetry, TELEMETRY_COLS, "telemetry")
    if outcome == "skipped" and tel.get("skip_reason") not in SKIP_REASONS:
        raise ValueError(f"skip_reason must be one of {SKIP_REASONS}")
    if outcome == "labeled" and not choices:
        raise ValueError("a labeled annotation needs at least one class choice")
    effective_key = effective_key or instance_key

    # The served event is the server's own clock. Scoped to this annotator so one
    # client cannot borrow another's older serve to claim a longer time-on-task.
    served_at = None
    serve_event_id = tel.get("serve_event_id")
    if serve_event_id is not None:
        row = con.execute(
            "SELECT \"at\" FROM label_events WHERE event_id = ? AND kind = 'served' "
            "AND annotator IS NOT DISTINCT FROM ?",
            [serve_event_id, annotator],
        ).fetchone()
        served_at = row[0] if row else None

    annotation_id = con.execute("SELECT nextval('seq_annotation')").fetchone()[0]
    cols = ["annotation_id", "instance_key", "effective_key", "key_version", "annotator",
            "version", "outcome", *PROVENANCE_COLS, *TELEMETRY_COLS]

    version = 0
    con.execute("BEGIN TRANSACTION")
    try:
        prev = con.execute(
            "SELECT max(version) FROM annotations WHERE instance_key = ? AND annotator = ?",
            [instance_key, annotator],
        ).fetchone()[0]
        version = int(prev or 0) + 1
        con.execute(
            "UPDATE annotations SET superseded_at = now() WHERE annotator = ? "
            "AND superseded_at IS NULL AND outcome <> 'undone' "
            "AND (effective_key = ? OR instance_key = ?)",
            [annotator, effective_key, instance_key],
        )
        values: list[Any] = [
            annotation_id, instance_key, effective_key, KEY_VERSION, annotator,
            version, outcome,
            *(prov.get(c) for c in PROVENANCE_COLS),
            *(tel.get(c) for c in TELEMETRY_COLS),
        ]
        # ordinal is NOT NULL, and both flags feed the agreement exclusion predicate,
        # so a NULL there would quietly drop the row from every headline number.
        values[cols.index("ordinal")] = int(prov.get("ordinal") or 0)
        values[cols.index("acting_preview")] = bool(tel.get("acting_preview"))
        values[cols.index("auth_disabled")] = bool(tel.get("auth_disabled"))
        con.execute(
            f"INSERT INTO annotations ({', '.join(cols)}, served_at, submitted_at, "
            "time_on_task_ms) VALUES "
            f"({', '.join(['?'] * len(cols))}, CAST(? AS TIMESTAMP), now(), "
            "CASE WHEN ? IS NULL THEN NULL ELSE "
            "CAST(date_diff('millisecond', CAST(? AS TIMESTAMP), now()) AS BIGINT) END)",
            values + [served_at, served_at, served_at],
        )
        for i, ch in enumerate(choices):
            con.execute(
                "INSERT INTO annotation_choices (annotation_id, class_key, group_key, "
                "class_name, ordinal) VALUES (?, ?, ?, ?, ?)",
                [annotation_id, ch["class_key"], ch["group_key"], ch.get("class_name"),
                 int(ch.get("ordinal", i))],
            )
        _insert_event(
            con, kind="submitted" if outcome == "labeled" else "skipped",
            session_id=tel.get("session_id"), annotator=annotator,
            instance_key=instance_key, class_key=None,
            detail=str(tel.get("skip_reason")) if outcome == "skipped" else None,
        )
        con.execute("COMMIT")
    except duckdb.ConstraintException as e:
        con.execute("ROLLBACK")
        raise ValueError(
            f"{annotator!r} already has version {version} of {instance_key!r} — a "
            "concurrent submit won the race; re-fetch and resubmit"
        ) from e
    except Exception:
        con.execute("ROLLBACK")
        raise
    return int(annotation_id)


def undo_last(
    con: duckdb.DuckDBPyConnection,
    annotator: str,
    instance_key: str,
    *,
    session_id: str | None = None,
) -> int | None:
    """Supersede this annotator's current answer about one instance. Returns the
    superseded `annotation_id`, or None when they have none.

    An undo is a supersede, never a delete: the row stays with `outcome='undone'` and
    `superseded_at` set, and a `label_events` row of `kind='undo'` records it.
    `annotation_id` is deliberately never a client-supplied mutation key — it is a
    dense `nextval` sequence, so a `DELETE /labels/{id}` route would let any plain
    user guess ids and destroy another annotator's submissions. There is no delete
    route anywhere in this feature.

    Scoped to `annotator` by the WHERE clause, so B can never undo A's work. Matches
    either key, because the client echoes the key the queue served and a
    reconciliation may since have moved it."""
    row = con.execute(
        "SELECT annotation_id FROM annotations WHERE annotator = ? "
        "AND superseded_at IS NULL AND outcome <> 'undone' "
        "AND (effective_key = ? OR instance_key = ?) "
        "ORDER BY submitted_at DESC, annotation_id DESC LIMIT 1",
        [annotator, instance_key, instance_key],
    ).fetchone()
    if not row:
        return None
    annotation_id = int(row[0])
    con.execute(
        "UPDATE annotations SET superseded_at = now(), outcome = 'undone' "
        "WHERE annotation_id = ?",
        [annotation_id],
    )
    _insert_event(con, kind="undo", session_id=session_id, annotator=annotator,
                  instance_key=instance_key, class_key=None, detail=str(annotation_id))
    return annotation_id


def log_event(
    con: duckdb.DuckDBPyConnection,
    *,
    kind: str,
    session_id: str | None = None,
    annotator: str | None = None,
    instance_key: str | None = None,
    class_key: str | None = None,
    detail: str | None = None,
) -> int:
    """Append one effort-telemetry event and return its `event_id`.

    The queue writes `kind='served'` per item and threads the id back as
    `serve_event_id`; that is a write, but to the LABELS file, so the main-DB read
    path the no-leasing decision protects stays untouched. It buys a non-forgeable
    server-side `time_on_task_ms`, measurable abandonment, and a server/client delta
    that detects tab-away. `info_opened` carries `class_key` — which description was
    read — and is the cheapest available signal that a class definition is
    ambiguous."""
    if kind not in EVENT_KINDS:
        raise ValueError(f"kind must be one of {EVENT_KINDS}, got {kind!r}")
    return _insert_event(con, kind=kind, session_id=session_id, annotator=annotator,
                         instance_key=instance_key, class_key=class_key, detail=detail)


def _insert_event(
    con: duckdb.DuckDBPyConnection, *, kind: str, session_id: str | None,
    annotator: str | None, instance_key: str | None, class_key: str | None,
    detail: str | None,
) -> int:
    event_id = con.execute("SELECT nextval('seq_label_event')").fetchone()[0]
    con.execute(
        "INSERT INTO label_events (event_id, \"at\", session_id, annotator, kind, "
        "instance_key, class_key, detail) VALUES (?, now(), ?, ?, ?, ?, ?, ?)",
        [event_id, session_id, annotator, kind, instance_key, class_key, detail],
    )
    return int(event_id)


# ----------------------------------------------------------------- reconciliation

def reconcile_dataset(
    lcon: duckdb.DuckDBPyConnection,
    mcon: duckdb.DuckDBPyConnection,
    dataset_id: str | None = None,
    run_id: str | None = None,
    actor: str = "system",
    *,
    archive_con: duckdb.DuckDBPyConnection | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Re-attach labels to detections after a re-ingest. Non-destructive (§2.4).

    The key survives clip/restore and archival by construction, but not a re-ingest
    with a changed detector, which shifts bboxes by a few pixels. This walks the
    state machine per instance, in order: exact key match (verified against
    `frame_sig`), IoU alias, archived, clipped, orphan. `instance_key` is NEVER
    rewritten — it is the audit trail of what the annotator actually hashed — only
    `effective_key`, which is what every consumer joins, groups and anti-joins on.

    Two guards carry the whole design:

    - a `frame_sig` MISMATCH is `hijacked`, not an attach. `frame_path` is
      deterministic in `(dataset_id, camera_id, frame_idx)`, so re-uploading
      different footage for the same day reproduces identical path strings over
      completely different pixels, and an exact key match there is a wrong-cow match
      made with full confidence. A NULL on either side is `attached_unverified` and
      counted separately, never silently attached.
    - the alias mapping is injective per run. A looser NMS setting on re-ingest merges
      two adjacent cows into one box; evaluated per annotation the runner-up guard
      passes for both source keys and both would alias onto the merged target, which
      presents later as two annotators disagreeing about "the same" instance. Any
      `new_key` claimed by more than one `old_key` is refused and left as
      `ambiguous_merge`.

    Aliases fan out from the annotation's own `instance_key` and never chain, so
    repeated runs are idempotent and resolution needs no recursive CTE. Pass
    `archive_con` (an open `cownting_archive.duckdb`) to distinguish `archived` from
    `orphan`; without it archived instances report as orphans. Callers treat this as
    advisory: `uploads._run` must never fail an upload over a reconciliation."""
    run_id = run_id or uuid.uuid4().hex
    scope = "" if dataset_id is None else " WHERE dataset_id IS NOT DISTINCT FROM ?"
    params: list[Any] = [] if dataset_id is None else [dataset_id]
    labels = _rows(lcon.execute(
        "SELECT instance_key, "
        "       arg_max(effective_key, annotation_id)  AS effective_key, "
        "       arg_max(dataset_id, annotation_id)     AS dataset_id, "
        "       arg_max(camera_id, annotation_id)      AS camera_id, "
        "       arg_max(frame_basename, annotation_id) AS frame_basename, "
        "       arg_max(bbox_x1, annotation_id) AS bbox_x1, "
        "       arg_max(bbox_y1, annotation_id) AS bbox_y1, "
        "       arg_max(bbox_x2, annotation_id) AS bbox_x2, "
        "       arg_max(bbox_y2, annotation_id) AS bbox_y2, "
        "       arg_max(frame_sig, annotation_id) FILTER (WHERE frame_sig IS NOT NULL) AS frame_sig, "
        "       count(*) AS n_annotations "
        f"FROM annotations{scope} GROUP BY instance_key",
        params,
    ))
    report: dict[str, Any] = {
        "run_id": run_id, "dataset_id": dataset_id, "dry_run": bool(dry_run),
        "instances": len(labels),
        "annotations": sum(int(r["n_annotations"]) for r in labels),
        "states": {s: 0 for s in RECONCILE_STATES},
    }
    if not labels:
        return report

    cameras = sorted({str(r["camera_id"]) for r in labels if r["camera_id"]})
    live = _live_index(mcon, "detections", cameras, dataset_id)
    # The dataset-scoped candidate search has a fallback: `cownting migrate` stamps a
    # derived dataset_id onto rows that had NULL, which changes every key on the
    # legacy partition, and a search scoped by the annotation's own (NULL) dataset
    # would find nothing and declare every label an orphan — while the queue,
    # keying off live detections, re-serves the whole set as new work.
    wide = _live_index(mcon, "detections", cameras, None) if dataset_id is not None else live
    clipped = _live_index(mcon, "clipped_detections", cameras, dataset_id)
    archived = _live_index(archive_con, "detections", cameras, dataset_id)

    outcomes: list[dict[str, Any]] = []
    proposals: dict[str, dict[str, Any]] = {}
    for row in labels:
        key = str(row["instance_key"])
        eff = str(row["effective_key"] or key)
        hit = live["by_key"].get(eff) or live["by_key"].get(key)
        if hit is not None:
            state = _verify_sig(row["frame_sig"], hit["frame_path"])
            outcomes.append({"instance_key": key, "state": state,
                             "new_key": hit["instance_key"], "iou": None,
                             "detail": None if state == "attached" else hit["frame_path"]})
            continue
        frame = (str(row["camera_id"]), str(row["frame_basename"]))
        pool = live["by_frame"].get(frame) or wide["by_frame"].get(frame, [])
        cand = _best_candidate(row, pool)
        if cand is not None:
            proposals[key] = cand
            continue
        if key in archived["by_key"] or eff in archived["by_key"]:
            outcomes.append({"instance_key": key, "state": "archived", "new_key": None,
                             "iou": None, "detail": "in the archive DB; still renderable"})
        elif key in clipped["by_key"] or eff in clipped["by_key"]:
            outcomes.append({"instance_key": key, "state": "clipped", "new_key": None,
                             "iou": None, "detail": "staged by clip_camera; re-attaches on restore"})
        else:
            outcomes.append({"instance_key": key, "state": "orphan", "new_key": None,
                             "iou": None, "detail": None})

    # Injectivity, enforced across the whole run rather than per annotation.
    claims: dict[str, list[str]] = {}
    for old_key, cand in proposals.items():
        claims.setdefault(str(cand["instance_key"]), []).append(old_key)
    for old_key, cand in proposals.items():
        new_key = str(cand["instance_key"])
        rival = _alias_owner(lcon, new_key, cand["dataset_id"])
        if len(claims[new_key]) > 1 or (rival is not None and rival != old_key):
            others = sorted(set(claims[new_key]) - {old_key} | ({rival} if rival else set()))
            outcomes.append({
                "instance_key": old_key, "state": "ambiguous_merge", "new_key": new_key,
                "iou": cand["iou"],
                "detail": "target also claimed by " + ", ".join(others),
            })
            continue
        outcomes.append({"instance_key": old_key, "state": "aliased", "new_key": new_key,
                         "iou": cand["iou"], "detail": None})
        if not dry_run:
            _write_alias(lcon, old_key=old_key, new_key=new_key, reason="reingest_iou",
                         iou=cand["iou"], dataset_id=cand["dataset_id"],
                         camera_id=cand["camera_id"], frame_basename=cand["frame_basename"],
                         actor=actor)
            lcon.execute(
                "UPDATE annotations SET effective_key = ? WHERE instance_key = ?",
                [new_key, old_key],
            )

    for o in outcomes:
        report["states"][o["state"]] = report["states"].get(o["state"], 0) + 1
        if not dry_run:
            lcon.execute(
                "INSERT OR REPLACE INTO reconciliations (run_id, \"at\", dataset_id, "
                "instance_key, state, new_key, iou, detail) VALUES (?, now(), ?, ?, ?, ?, ?, ?)",
                [run_id, dataset_id, o["instance_key"], o["state"], o["new_key"],
                 o["iou"], o["detail"]],
            )
    return report


def rekey_after_migrate(
    lcon: duckdb.DuckDBPyConnection,
    dataset_id: str,
    *,
    actor: str = "system",
) -> int:
    """Re-attach the NULL-dataset partition after `cownting migrate`. Returns the
    number of annotations moved.

    `migrate` stamps a derived `dataset_id` onto every frame and detection that had
    NULL, and `dataset_id` is the first component of the key — so every label on that
    partition would otherwise be an orphan while the queue re-serves the whole set as
    fresh work. Nothing about the animal changed, so the new key is computable
    directly from the stored provenance and no IoU search is needed or wanted.

    The ordinal is unaffected: its partition includes `coalesce(dataset_id, '')`, and
    stamping ONE id onto ALL of the NULL rows leaves the grouping identical.

    `annotations.dataset_id` is deliberately NOT rewritten. It is the provenance the
    key was hashed from; rewriting it would make `instance_key` un-reproducible from
    the row that carries it, which is the one invariant the reconciler's step 1
    depends on."""
    rows = _rows(lcon.execute(
        "SELECT instance_key, "
        "       arg_max(effective_key, annotation_id)  AS effective_key, "
        "       arg_max(camera_id, annotation_id)      AS camera_id, "
        "       arg_max(frame_basename, annotation_id) AS frame_basename, "
        "       arg_max(frame_path, annotation_id)     AS frame_path, "
        "       arg_max(bbox_x1, annotation_id) AS bbox_x1, "
        "       arg_max(bbox_y1, annotation_id) AS bbox_y1, "
        "       arg_max(bbox_x2, annotation_id) AS bbox_x2, "
        "       arg_max(bbox_y2, annotation_id) AS bbox_y2, "
        "       arg_max(ordinal, annotation_id) AS ordinal "
        "FROM annotations WHERE dataset_id IS NULL GROUP BY instance_key"
    ))
    moved = 0
    for r in rows:
        base = str(r["frame_basename"] or r["frame_path"] or "")
        try:
            new_key = instance_key(
                dataset_id, str(r["camera_id"]), base,
                [r["bbox_x1"], r["bbox_y1"], r["bbox_x2"], r["bbox_y2"]],
                int(r["ordinal"] or 0),
            )
        except (TypeError, ValueError):
            continue  # provenance too thin to re-key; the reconciler reports it
        old_key = str(r["instance_key"])
        # `dataset_id` deliberately stays NULL on the row (see above), so a second
        # run re-derives the same new_key — skip on the already-moved effective_key
        # rather than re-issuing the UPDATE and over-reporting the move.
        if new_key == old_key or new_key == str(r["effective_key"] or ""):
            continue
        if not _write_alias(lcon, old_key=old_key, new_key=new_key, reason="migrate",
                            iou=None, dataset_id=dataset_id, camera_id=r["camera_id"],
                            frame_basename=base, actor=actor):
            continue
        lcon.execute("UPDATE annotations SET effective_key = ? WHERE instance_key = ?",
                     [new_key, old_key])
        moved += 1
    return moved


def annotation_count(
    con: duckdb.DuckDBPyConnection, dataset_id: str | None = None,
    camera_id: str | None = None,
) -> int:
    """How many annotations a destructive operation is about to put through
    reconciliation. `pipeline.ingest` prints it and `/api/uploads` returns it in the
    confirmation payload: `purge_dataset` + `rmtree` is already the most destructive
    button in the app and it should not also be the one that silently strands
    annotator hours."""
    clauses, params = [], []
    if dataset_id is not None:
        clauses.append("dataset_id IS NOT DISTINCT FROM ?")
        params.append(dataset_id)
    if camera_id is not None:
        clauses.append("camera_id = ?")
        params.append(camera_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    return int(con.execute(f"SELECT count(*) FROM annotations{where}", params).fetchone()[0])


def _live_index(
    con: duckdb.DuckDBPyConnection | None, table: str, cameras: Sequence[str],
    dataset_id: str | None,
) -> dict[str, Any]:
    """Current detections keyed both by `instance_key` and by frame, for matching.

    Filtering by dataset_id/camera_id BEFORE the window is safe because both are
    partition columns of the ordinal, so the ordinals (and therefore the keys) are
    identical to what a full-table scan would produce."""
    empty: dict[str, Any] = {"by_key": {}, "by_frame": {}}
    if con is None or not cameras or not _has_table(con, table):
        return empty
    clauses = ["d.camera_id IN (" + ", ".join(["?"] * len(cameras)) + ")"]
    params: list[Any] = list(cameras)
    if dataset_id is not None:
        clauses.append("d.dataset_id IS NOT DISTINCT FROM ?")
        params.append(dataset_id)
    rows = _rows(con.execute(
        "SELECT * FROM (SELECT d.dataset_id, d.camera_id, d.frame_path, "
        f"       {_basename_sql('d.frame_path')} AS frame_basename, "
        "       d.bbox_x1, d.bbox_y1, d.bbox_x2, d.bbox_y2, "
        f"       {instance_key_sql('d')} AS instance_key "
        f"FROM {table} d WHERE " + " AND ".join(clauses) + ") t "
        "WHERE instance_key IS NOT NULL",
        params,
    ))
    by_key: dict[str, dict[str, Any]] = {}
    by_frame: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for r in rows:
        by_key.setdefault(str(r["instance_key"]), r)
        by_frame.setdefault((str(r["camera_id"]), str(r["frame_basename"])), []).append(r)
    return {"by_key": by_key, "by_frame": by_frame}


def _verify_sig(stored: Any, frame_path: Any) -> str:
    """Step 1/1b/1c of the state machine. NULL on either side is an explicit third
    state, so a missing image never accidentally satisfies or fails the check."""
    if not stored:
        return "attached_unverified"
    now = frame_sig(str(frame_path)) if frame_path else None
    if now is None:
        return "attached_unverified"
    return "attached" if now == str(stored) else "hijacked"


def _best_candidate(
    row: Mapping[str, Any], cands: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Best IoU ≥ 0.70 with a runner-up ≤ 0.50 among one frame's boxes, else None.

    A failed guard is NOT a reason to widen the search: the runner-up test failing
    means two live boxes are both plausible, and picking either would attach the
    label to a coin-flip animal. Only an EMPTY candidate set justifies the caller's
    `(camera_id, frame_basename)` fallback."""
    if not cands:
        return None
    box = (row["bbox_x1"], row["bbox_y1"], row["bbox_x2"], row["bbox_y2"])
    scored = sorted(
        ((_iou(box, (c["bbox_x1"], c["bbox_y1"], c["bbox_x2"], c["bbox_y2"])), c)
         for c in cands),
        key=lambda p: p[0], reverse=True,
    )
    best_iou, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else 0.0
    if best_iou < IOU_ATTACH or runner_up > IOU_RUNNER_UP:
        return None
    return {**best, "iou": float(best_iou)}


def _iou(a: Sequence[Any], b: Sequence[Any]) -> float:
    try:
        ax1, ay1, ax2, ay2 = (float(v) for v in a)
        bx1, by1, bx2, by2 = (float(v) for v in b)
    except (TypeError, ValueError):
        return 0.0
    iw = min(ax2, bx2) - max(ax1, bx1)
    ih = min(ay2, by2) - max(ay1, by1)
    if iw <= 0 or ih <= 0:
        return 0.0
    inter = iw * ih
    union = (ax2 - ax1) * (ay2 - ay1) + (bx2 - bx1) * (by2 - by1) - inter
    return inter / union if union > 0 else 0.0


def _alias_owner(con: duckdb.DuckDBPyConnection, new_key: str, dataset_id: Any) -> str | None:
    row = con.execute(
        "SELECT old_key FROM instance_key_aliases WHERE new_key = ? "
        "AND dataset_id IS NOT DISTINCT FROM ?",
        [new_key, dataset_id],
    ).fetchone()
    return str(row[0]) if row else None


def _write_alias(
    con: duckdb.DuckDBPyConnection, *, old_key: str, new_key: str, reason: str,
    iou: float | None, dataset_id: Any, camera_id: Any, frame_basename: Any, actor: str,
) -> bool:
    """Record one re-attachment. False when the store's own `UNIQUE (new_key,
    dataset_id)` guard refuses it — the invariant is enforced by the store, not only
    by the code above, so a bug there degrades to "not re-attached" rather than to
    two annotators' answers about two animals merged into one."""
    try:
        con.execute(
            "INSERT INTO instance_key_aliases (old_key, new_key, reason, iou, dataset_id, "
            "camera_id, frame_basename, created_at, created_by) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, now(), ?)",
            [old_key, new_key, reason, iou, dataset_id, camera_id, frame_basename, actor],
        )
    except duckdb.ConstraintException:
        return _alias_owner(con, new_key, dataset_id) == old_key
    return True


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


# ---------------------------------------------------------------------- agreement

# Every agreement query starts from `v_current_answers` (superseded versions and
# skips are already excluded there) and applies the SAME exclusion predicate, so the
# headline numbers are all computed over one population. coalesce() is not
# decoration: a row written before those columns existed carries NULL, and a bare
# `NOT acting_preview` would silently drop it from every statistic.
_ANS_CTE = """WITH ans AS (
    SELECT v.effective_key, v.annotator, v.class_key
    FROM v_current_answers v{escape_join}
    WHERE v.group_key = ?
      AND NOT coalesce(v.acting_preview, FALSE)
      AND NOT coalesce(v.auth_disabled, FALSE){escape_where}
)"""

_PAIRWISE_BODY = """
pairs AS (
    SELECT a.effective_key AS item, a.annotator AS rater_a, b.annotator AS rater_b,
           CASE WHEN a.class_key = b.class_key THEN 1 ELSE 0 END AS agree
    FROM ans a JOIN ans b
      ON a.effective_key = b.effective_key AND a.annotator < b.annotator
)
SELECT count(*) AS n_pairs, count(DISTINCT item) AS n_items,
       coalesce(sum(agree), 0) AS n_agree,
       sum(agree) * 1.0 / nullif(count(*), 0) AS pairwise_agreement
FROM pairs
"""

# Cohen's kappa is defined for two raters, and labeling here is opportunistic, so
# this reports it per rater PAIR over the items they both answered rather than
# inventing a single number over a variable panel.
_COHEN_BODY = """
pairs AS (
    SELECT a.effective_key AS item, a.annotator AS rater_a, b.annotator AS rater_b,
           a.class_key AS class_a, b.class_key AS class_b
    FROM ans a JOIN ans b
      ON a.effective_key = b.effective_key AND a.annotator < b.annotator
),
totals AS (
    SELECT rater_a, rater_b, count(*) AS n_common,
           sum(CASE WHEN class_a = class_b THEN 1 ELSE 0 END) AS n_agree
    FROM pairs GROUP BY 1, 2
),
marg AS (
    SELECT p.rater_a, p.rater_b, k.class_key,
           count(*) FILTER (WHERE p.class_a = k.class_key) AS na,
           count(*) FILTER (WHERE p.class_b = k.class_key) AS nb
    FROM pairs p CROSS JOIN (SELECT DISTINCT class_key FROM ans) k
    GROUP BY 1, 2, 3
),
chance AS (
    SELECT m.rater_a, m.rater_b,
           sum((m.na * 1.0 / t.n_common) * (m.nb * 1.0 / t.n_common)) AS p_e
    FROM marg m JOIN totals t ON t.rater_a = m.rater_a AND t.rater_b = m.rater_b
    GROUP BY 1, 2
)
SELECT t.rater_a, t.rater_b, t.n_common, t.n_agree,
       t.n_agree * 1.0 / t.n_common AS p_o, c.p_e,
       CASE WHEN 1 - c.p_e = 0 THEN NULL
            ELSE (t.n_agree * 1.0 / t.n_common - c.p_e) / (1 - c.p_e) END AS kappa
FROM totals t JOIN chance c ON c.rater_a = t.rater_a AND c.rater_b = t.rater_b
ORDER BY t.n_common DESC, t.rater_a, t.rater_b
"""

# Per-item normalisation, NOT the classic equal-n form: items are labeled
# opportunistically here (targets 2, overlap slice 3, and whoever shows up), so a
# fixed rater count does not exist. Items with a single rater carry no agreement
# information and are dropped rather than counted as perfect.
_FLEISS_BODY = """
counts AS (
    SELECT effective_key AS item, class_key, count(DISTINCT annotator) AS n_ij
    FROM ans GROUP BY 1, 2
),
items AS (
    SELECT item, sum(n_ij) AS n_i, sum(n_ij * n_ij) AS sq FROM counts GROUP BY 1
),
usable AS (SELECT * FROM items WHERE n_i >= 2),
agree AS (
    SELECT avg((sq - n_i) * 1.0 / (n_i * (n_i - 1))) AS p_bar,
           count(*) AS n_items, sum(n_i) AS n_ratings
    FROM usable
),
p_j AS (
    SELECT c.class_key,
           sum(c.n_ij) * 1.0 / nullif((SELECT sum(n_i) FROM usable), 0) AS p
    FROM counts c JOIN usable u ON u.item = c.item GROUP BY 1
),
chance AS (SELECT sum(p * p) AS p_e FROM p_j)
SELECT a.n_items, a.n_ratings, a.p_bar, c.p_e,
       CASE WHEN c.p_e IS NULL OR 1 - c.p_e = 0 THEN NULL
            ELSE (a.p_bar - c.p_e) / (1 - c.p_e) END AS kappa
FROM agree a, chance c
"""

# Multi-select cannot go through the exact-match SQL: the self-join emits one row per
# selected CLASS rather than per annotator, inflating n_common and double-counting a
# rater in Fleiss' n_ij — a plausible wrong number rather than an error.
_JACCARD_BODY = """
sets AS (
    SELECT effective_key AS item, annotator, list_sort(list(DISTINCT class_key)) AS classes
    FROM ans GROUP BY 1, 2
),
pairs AS (
    SELECT a.item, a.annotator AS rater_a, b.annotator AS rater_b,
           len(list_intersect(a.classes, b.classes)) AS inter,
           len(list_distinct(list_concat(a.classes, b.classes))) AS uni
    FROM sets a JOIN sets b ON a.item = b.item AND a.annotator < b.annotator
)
SELECT count(*) AS n_pairs, count(DISTINCT item) AS n_items,
       avg(CASE WHEN uni = 0 THEN NULL ELSE inter * 1.0 / uni END) AS jaccard,
       sum(CASE WHEN inter = uni THEN 1 ELSE 0 END) * 1.0 / nullif(count(*), 0)
           AS exact_set_agreement
FROM pairs
"""


def _agreement_sql(body: str, *, exclude_escape: bool) -> str:
    return _ANS_CTE.format(
        escape_join=("\n    JOIN label_classes c ON c.class_key = v.class_key"
                     if exclude_escape else ""),
        escape_where="\n      AND NOT c.is_escape" if exclude_escape else "",
    ) + "," + body


# Each takes one parameter: the group_key. The *_NO_ESCAPE variants drop `is_escape`
# classes, which is the "Cannot tell as missing data" sensitivity reading of the
# headline number (§9.1) — a research choice, so both are computable and neither is
# hardcoded as the truth.
SQL_PAIRWISE_AGREEMENT = _agreement_sql(_PAIRWISE_BODY, exclude_escape=False)
SQL_PAIRWISE_AGREEMENT_NO_ESCAPE = _agreement_sql(_PAIRWISE_BODY, exclude_escape=True)
SQL_COHENS_KAPPA = _agreement_sql(_COHEN_BODY, exclude_escape=False)
SQL_COHENS_KAPPA_NO_ESCAPE = _agreement_sql(_COHEN_BODY, exclude_escape=True)
SQL_FLEISS_KAPPA = _agreement_sql(_FLEISS_BODY, exclude_escape=False)
SQL_FLEISS_KAPPA_NO_ESCAPE = _agreement_sql(_FLEISS_BODY, exclude_escape=True)
SQL_MULTISELECT_JACCARD = _agreement_sql(_JACCARD_BODY, exclude_escape=False)
SQL_MULTISELECT_JACCARD_NO_ESCAPE = _agreement_sql(_JACCARD_BODY, exclude_escape=True)

# Effort reads `annotations`, not `v_current_answers`: an orphaned label is still
# work someone did, and a skip is still a judgement someone made.
SQL_EFFORT_BY_ANNOTATOR = """
SELECT annotator,
       count(*)                                                          AS submissions,
       count(*) FILTER (WHERE outcome = 'labeled' AND superseded_at IS NULL) AS labeled,
       count(*) FILTER (WHERE outcome = 'skipped' AND superseded_at IS NULL) AS skipped,
       count(*) FILTER (WHERE outcome = 'undone')                        AS undone,
       count(*) FILTER (WHERE version > 1)                               AS relabels,
       count(DISTINCT effective_key)                                     AS instances,
       count(DISTINCT session_id)                                        AS sessions,
       median(time_on_task_ms)                                           AS median_ms,
       sum(time_on_task_ms)                                              AS total_ms,
       sum(client_elapsed_ms)                                            AS client_ms,
       min(submitted_at)                                                 AS first_at,
       max(submitted_at)                                                 AS last_at,
       count(*) FILTER (WHERE acting_preview)                            AS preview_rows,
       count(*) FILTER (WHERE auth_disabled)                             AS anonymous_rows
FROM annotations GROUP BY annotator ORDER BY labeled DESC, annotator
"""

# Which definitions annotators actually had to re-read. A class whose (i) icon is
# opened often is a class whose wording is doing badly, and rewriting it is the
# cheapest available agreement improvement.
SQL_INFO_ICON_PRESSURE = """
SELECT e.class_key, c.group_key, c.name AS class_name,
       count(*)                        AS opens,
       count(DISTINCT e.annotator)     AS annotators,
       count(DISTINCT e.session_id)    AS sessions,
       max(e."at")                     AS last_opened_at
FROM label_events e
LEFT JOIN label_classes c ON c.class_key = e.class_key
WHERE e.kind = 'info_opened'
GROUP BY 1, 2, 3 ORDER BY opens DESC, e.class_key
"""

# Served, never answered. The server/client elapsed delta is the tab-away detector:
# 40 minutes of server elapsed against 6 seconds of client-active time is not 40
# minutes of attention.
SQL_ABANDONED = """
SELECT s.annotator,
       count(*)                                        AS served,
       count(*) FILTER (WHERE a.annotation_id IS NULL) AS abandoned,
       count(*) FILTER (WHERE a.annotation_id IS NULL) * 1.0 / nullif(count(*), 0)
                                                       AS abandon_rate,
       median(a.time_on_task_ms)                       AS median_answered_ms,
       median(a.client_elapsed_ms)                     AS median_client_ms
FROM (SELECT event_id, annotator FROM label_events WHERE kind = 'served') s
LEFT JOIN annotations a ON a.serve_event_id = s.event_id
GROUP BY 1 ORDER BY served DESC, s.annotator
"""


def agreement(
    con: duckdb.DuckDBPyConnection,
    group_key: str,
    *,
    metric: str | None = None,
    exclude_escape: bool = False,
) -> dict[str, Any]:
    """Inter-rater agreement for one question, dispatched on `multi_select`.

    Single-select goes to the exact-match pairwise + per-pair Cohen queries;
    multi-select goes to Jaccard over the selected sets. `metric` ('exact' |
    'jaccard') forces one and raises `ValueError` when it contradicts the group,
    because the wrong query does not fail — it returns a plausible wrong number."""
    multi = _multi_select(con, group_key)
    want = metric or ("jaccard" if multi else "exact")
    if want not in ("exact", "jaccard"):
        raise ValueError("metric must be 'exact' or 'jaccard'")
    if want == "exact" and multi:
        raise ValueError(
            f"group {group_key!r} is multi-select: the exact-match self-join emits one "
            "row per selected class, not per annotator, so it would inflate n_pairs "
            "and report a plausible wrong number — use metric='jaccard'"
        )
    if want == "jaccard" and not multi:
        raise ValueError(
            f"group {group_key!r} is single-select: Jaccard over one-element sets is "
            "just exact match with a worse name — use metric='exact'"
        )
    out: dict[str, Any] = {"group_key": group_key, "multi_select": multi, "metric": want,
                           "exclude_escape": bool(exclude_escape)}
    if want == "jaccard":
        sql = SQL_MULTISELECT_JACCARD_NO_ESCAPE if exclude_escape else SQL_MULTISELECT_JACCARD
        out.update(_rows(con.execute(sql, [group_key]))[0])
        return out
    sql = SQL_PAIRWISE_AGREEMENT_NO_ESCAPE if exclude_escape else SQL_PAIRWISE_AGREEMENT
    out.update(_rows(con.execute(sql, [group_key]))[0])
    ksql = SQL_COHENS_KAPPA_NO_ESCAPE if exclude_escape else SQL_COHENS_KAPPA
    out["pairs"] = _rows(con.execute(ksql, [group_key]))
    return out


def fleiss_kappa(
    con: duckdb.DuckDBPyConnection,
    group_key: str,
    *,
    exclude_escape: bool = False,
) -> dict[str, Any]:
    """Fleiss' kappa over the whole panel for one single-select question.

    Refuses a multi-select group rather than falling through: a rater who picked two
    classes is counted twice in `n_ij`, which quietly breaks the per-item
    normalisation and returns a number that looks fine."""
    if _multi_select(con, group_key):
        raise ValueError(
            f"group {group_key!r} is multi-select: Fleiss' n_ij would count one rater "
            "once per selected class — use agreement(..., metric='jaccard')"
        )
    sql = SQL_FLEISS_KAPPA_NO_ESCAPE if exclude_escape else SQL_FLEISS_KAPPA
    out: dict[str, Any] = {"group_key": group_key, "exclude_escape": bool(exclude_escape)}
    out.update(_rows(con.execute(sql, [group_key]))[0])
    return out


def _multi_select(con: duckdb.DuckDBPyConnection, group_key: str) -> bool:
    row = con.execute(
        "SELECT multi_select FROM label_groups WHERE group_key = ?", [group_key]
    ).fetchone()
    if row is None:
        raise ValueError(f"unknown group {group_key!r}")
    return bool(row[0])


# -------------------------------------------------------------------------- utils

def _rows(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    """Result rows as JSON-ready dicts. Timestamps become ISO strings here rather
    than at the route, so `api.py` stays free of serialisation logic."""
    cols = [c[0] for c in cur.description]
    return [{c: _plain(v) for c, v in zip(cols, r)} for r in cur.fetchall()]


def _plain(v: Any) -> Any:
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return v


def _one(con: duckdb.DuckDBPyConnection, table: str, key_col: str, key: str) -> dict[str, Any] | None:
    rows = _rows(con.execute(f"SELECT * FROM {table} WHERE {key_col} = ?", [key]))
    return rows[0] if rows else None


def _exists(con: duckdb.DuckDBPyConnection, table: str, key_col: str, key: str) -> bool:
    return con.execute(
        f"SELECT 1 FROM {table} WHERE {key_col} = ?", [key]
    ).fetchone() is not None


def _next_sort(
    con: duckdb.DuckDBPyConnection, table: str, scope_col: str | None = None,
    scope: str | None = None,
) -> int:
    where = f" WHERE {scope_col} = ?" if scope_col else ""
    params = [scope] if scope_col else []
    row = con.execute(f"SELECT max(sort_order) FROM {table}{where}", params).fetchone()
    return int(row[0] or 0) + 10


def _set_clause(fields: Mapping[str, Any]) -> tuple[list[str], list[Any]]:
    sets, params = [], []
    for col, val in fields.items():
        if val is not None:
            sets.append(f"{col} = ?")
            params.append(val)
    return sets, params


def _picked(src: Mapping[str, Any] | None, allowed: Iterable[str], what: str) -> dict[str, Any]:
    d = dict(src or {})
    unknown = sorted(set(d) - set(allowed))
    if unknown:
        raise ValueError(f"unknown {what} field(s): {', '.join(unknown)}")
    return d


def _dumps(v: Mapping[str, Any] | None) -> str | None:
    return None if v is None else json.dumps(v, default=str, sort_keys=True)
