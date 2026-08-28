"""Serving side of the in-app Label page: the queue, the ATTACH, and the crop.

`labels_db` owns the store — its DDL, the instance key, the writes, the agreement
SQL. This module owns everything that has to touch the *main* DB or the frame
JPEGs in order to serve an instance: the derived queue scan and its sampling
policy, the context manager that ATTACHes the label store onto a main-DB
connection, the pure crop geometry each queue item carries, and the two renderers
behind `/api/img/label-crop` and `/api/img/label-frame` (which share one
banner mask, so the tile and the hold-to-peek frame cannot disagree about how
much of the burned-in clock the annotator gets to see). The import direction is
one-way
(`labeling` -> `labels_db`) and must stay that way: `labels_db` is opened by the
CLI and the weekly backup job in processes that never build a queue and must not
drag pandas/PIL/the main DB in behind it.

Nothing here writes to the main DB. The queue does write one `served` row per
item, but into `labels.duckdb` through the attached alias — see attached_labels()
for why the direction of the ATTACH is not a free choice.

Every non-obvious decision below is argued in docs/roadmap/M3_labeling.md
§2.2 (the key), §4.2 (sampling), §4.3 (the write path) and §4.5 (the crop).
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path
from typing import Iterator, Protocol, Sequence
from urllib.parse import quote, urlencode

import duckdb

from . import db
from . import labels_db
from . import quality
from .config import AnnotationCfg, Config

# Bump when the rendered pixels change (fill colour, resampling, JPEG quality,
# banner geometry). It is ETag material, so a render change invalidates every
# cached crop; without it an annotator keeps being served last week's rendering
# from their browser cache and a masking fix never reaches the people it protects.
RENDER_VERSION = "1"

# The alias the label store is ATTACHed under. Module-level because the queue SQL
# is written against it and a mismatch is a "table not found" at runtime only.
LABELS_ALIAS = "labels"

# The Brinno timestamp bar occupies the bottom ~4% of every frame. Borrowed from
# quality._BANNER_CROP rather than re-declared: if the two ever drift apart one of
# them starts showing the annotator the wall-clock time, and time of day IS the
# answer to "Sun exposure" (§4.5) — a leak that inflates agreement instead of
# breaking anything visibly.
_BANNER_TOP = quality._BANNER_CROP

# Neutral fill for out-of-frame padding and for the masked banner. Mid grey on
# purpose: cow shade is dark and sunlit grass is bright, so a mid grey reads as
# "no information here" against both, where black would pass for shadow.
_FILL = (96, 96, 96)

_JPEG_QUALITY = 88

# Longest edge of the full-frame view behind `/api/img/label-frame`. A frame is
# ~5-10x the pixels of a crop and it is fetched on a KEY-HOLD, so the raw JPEG
# would make hold-to-peek feel broken on the first press of every item. 1600 is
# wide enough that the whole field of view stays readable on a laptop and still
# roughly a quarter of the bytes. Not in AnnotationCfg: it is a rendering detail
# of one route, and RENDER_VERSION already covers changing it.
FRAME_MAX_WIDTH = 1600

# The client sends back the keys already in its buffer instead of an offset (§4.2).
# Capped because it arrives in a query string from a session that `require_labeler`
# lets any role hold, and it becomes N bound parameters in the hot scan.
_MAX_EXCLUDE = 200


class AnchorLike(Protocol):
    """The shape `verify_anchor` reads off `api.InstanceAnchor`.

    Structural, not an import: `api` imports this module, so naming its pydantic
    model here would close the cycle. The field names are frozen in §4.1.
    """

    dataset_id: str | None
    camera_id: str
    frame_file: str
    bbox: list[float]
    ordinal: int


# --------------------------------------------------------------------------- connections
def labels_connect(config: Config) -> duckdb.DuckDBPyConnection:
    """A read-write connection to the label store, with `db.connect`'s retry.

    Read-write even for readers and for the backup job: DuckDB refuses a second
    connection to one file with a different mode in one process, and that error
    text matches none of `db.connect`'s retry substrings, so a `read_only=True`
    open surfaces as an un-retried 500 the moment a submit is in flight (§3, and
    `api.py:189-196` documents the same trap for the main DB).
    """
    return db.connect(config.paths.labels_db_path)


_INIT_LOCK = threading.Lock()
_INITIALISED: set[str] = set()   # store paths whose schema THIS process has built


def ensure_labels_db(config: Config) -> None:
    """Create/upgrade the label store's schema on its own short-lived connection.

    Guaranteed before every ATTACH rather than once at boot, because ATTACH on a
    path that does not exist yet happily creates an *empty* database — the queue
    would then fail on `labels.v_instance_coverage` with a bare "table does not
    exist", which is what a CLI process, a fresh box or a `data/` restored without
    `labels.duckdb` all look like.

    Actually *running* the DDL every time, though, is both wasteful and unsafe. The
    DDL ends in `CREATE OR REPLACE VIEW`, which is a catalog write: two requests
    arriving together — the browser opens the Label page and fires taxonomy, queue
    and progress at once — raced and one died with "Catalog write-write conflict on
    alter with ... v_current_annotations", a 500 on a completely healthy store.
    Tests never saw it because they call sequentially.

    So the schema is built once per process under a lock, and the memo is keyed on
    the path AND re-checked against the file still existing, which keeps the
    property the eager call was there for: delete `labels.duckdb` under a running
    server and the next request rebuilds it rather than serving off a stale memo.
    A cross-process race (the CLI initialising while the server does) is still
    possible, so the conflict itself is retried — the DDL is idempotent, so the
    other writer finishing the job is success, not failure.
    """
    path = os.path.abspath(str(config.paths.labels_db_path))
    if path in _INITIALISED and os.path.exists(path):
        return
    with _INIT_LOCK:
        # Re-check inside the lock: several threads queue up on a cold start and
        # only the first should pay for the DDL.
        if path in _INITIALISED and os.path.exists(path):
            return
        delay = 0.02
        last: Exception | None = None
        for _ in range(50):
            lcon = labels_connect(config)
            try:
                labels_db.init_labels_db(lcon)
                _INITIALISED.add(path)
                return
            except Exception as e:  # noqa: BLE001 — retry only the catalog race
                if db.is_transient_lock_error(e):
                    last = e
                    time.sleep(delay)
                    delay = min(delay * 1.5, 0.2)
                    continue
                raise
            finally:
                lcon.close()
        assert last is not None
        raise last  # exhausted retries — surface the real DuckDB error


@contextmanager
def attached_labels(con: duckdb.DuckDBPyConnection, config: Config) -> Iterator[str]:
    """ATTACH the label store onto an open MAIN-DB connection; DETACH on the way out.

    The direction is fixed and it is the whole point (§4.2). `localize_worker`
    holds a writer on the main DB for an entire localize pass, fired by clip,
    restore, delete-camera and every areas save. Attaching main into a *labels*
    connection would put every label read and every label write behind that lock;
    attaching labels into main confines the exposure to the queue's reads, and
    leaves `submit_annotation` on a plain labels connection that never sees the
    main DB at all.

    The bare ATTACH is retried with `db.connect`'s backoff (~9 s) on the shared
    `db.is_transient_lock_error` predicate. The loop is separate from
    `db.connect`'s because that retries an *open* and this retries a statement,
    but the predicate must NOT be: it was copy-pasted here once, and a fix applied
    only to `db.connect` left this ATTACH still 500ing on Windows. The clash is
    routine, not exotic: the store is held read-write by every concurrent submit
    and by the schema-init above, which released its handle microseconds ago.

    DETACH runs only if the ATTACH landed. A leaked alias does not fail here — it
    fails the *next* ATTACH on the same pooled connection, surfacing as a random
    500 on an unrelated request with nothing in the traceback pointing back here.

    Yields the alias name so callers never hardcode it.
    """
    ensure_labels_db(config)
    # ATTACH takes a literal path, not a bind parameter. The path is trusted
    # config; escape quotes defensively, exactly as db.archive_dataset does.
    safe = str(config.paths.labels_db_path).replace("'", "''")
    attached = False
    delay = 0.02
    last: Exception | None = None
    for _ in range(50):
        try:
            con.execute(f"ATTACH '{safe}' AS {LABELS_ALIAS}")
            attached = True
            break
        except Exception as e:  # noqa: BLE001 — retry only the file-handle clash
            if db.is_transient_lock_error(e):
                last = e
                time.sleep(delay)
                delay = min(delay * 1.5, 0.2)
                continue
            raise
    if not attached:
        assert last is not None
        raise last  # exhausted retries — surface the real DuckDB error
    try:
        yield LABELS_ALIAS
    finally:
        con.execute(f"DETACH {LABELS_ALIAS}")


# --------------------------------------------------------------------------- keys
def verify_anchor(instance_key: str, anchor: AnchorLike) -> bool:
    """True when `instance_key` is the hash of `anchor` — the submit path's whole guard.

    `POST /api/label/submit` never opens the main DB (§4.3): the client echoes the
    anchor the queue served and this re-derives the key from it in Python. Because
    the key *is* the hash of the anchor, a stored row whose key and provenance
    disagree is unrepresentable, and the write needs no detection row to still
    exist — it may have been purged, clipped or archived since the crop was drawn.

    Returns a bool rather than raising: `labels_db` raises `ValueError` for the
    lost UNIQUE race that `api.py` maps to 409, and a forged anchor is a 400.
    """
    try:
        bbox = [float(v) for v in (anchor.bbox or [])]
        if len(bbox) != 4:
            return False
        recomputed = labels_db.instance_key(
            anchor.dataset_id,
            anchor.camera_id,
            anchor.frame_file,
            bbox,
            int(anchor.ordinal or 0),
        )
    except Exception:  # noqa: BLE001 — an unhashable anchor is a bad request, not a 500
        return False
    return bool(instance_key) and recomputed == instance_key


def hex_threshold(fraction: float) -> str:
    """The cut point for the deep-overlap subset: `substr(instance_key,1,4) < this`.

    Membership is derived from the key itself, so every annotator independently
    agrees on the same subset with zero coordination and no table to keep in sync.
    Lexicographic comparison over equal-length lowercase hex is numeric
    comparison, because '0'..'9' sort before 'a'..'f' in ASCII.

    The two edges are strings rather than numbers on purpose: `''` is smaller than
    every key (so fraction 0 selects nothing) and `'g'` is larger than every hex
    digit (so fraction 1 selects everything). Returning `'10000'` for 1.0 would
    silently select *nothing*, since '1' sorts before 'f'.
    """
    if fraction <= 0:
        return ""
    if fraction >= 1:
        return "g"
    return f"{int(fraction * 0x10000):04x}"


# --------------------------------------------------------------------------- the queue
def _scan_sql(cfg: AnnotationCfg, alias: str, *, annotator: str,
              dataset: str | None, camera: str | None, day: str | None) -> tuple[str, list]:
    """The CTE chain every queue/progress read starts from, plus its parameters.

    Ends at a relation named `scoped` carrying one row per live detection with its
    key, its coverage counts, its target and whether the caller has already
    answered it. The queue is derived per request rather than materialised because
    detections are written from three places (`pipeline.segment`, `db.restore_clip`'s
    raw SQL, the CLI) and `purge_dataset` deletes them from under any table we
    could have kept (§1).

    The scope filters live in the innermost CTE so the hash is computed over the
    filtered rows, not the whole table. That is only safe because `dataset_id`,
    `camera_id` and `frame_path` are all *partition* columns of the ordinal window
    (and `day` is a function of `dataset_id`), so a filter drops whole partitions
    and can never renumber the survivors. Adding a filter on anything else here —
    a score floor, a time window — would silently change ordinals, hence keys,
    hence orphan every label already written for those rows.
    """
    # An empty frame_path means the ingest ran with `save_frames: false`, so there
    # is no JPEG to crop and `labels_db.instance_key` refuses the row outright. Left
    # in the queue it would serve an instance whose crop 404s and whose submit 400s.
    # It is also a partition column, so filtering on it cannot renumber anything.
    clauses = ["d.frame_path IS NOT NULL", "d.frame_path <> ''"]
    # A NULL bbox is the same class of unlabelable row, and `detections` permits it:
    # DET_COLS fills every absent column with NULL, so any insert path that omits the
    # box (an early-stage row, a hand-built import) lands here. Such a row has no
    # crop to render and no key to hash — before this guard it reached _item() and
    # raised TypeError on float(None), turning one malformed row into a 500 for the
    # whole queue. All four are partition columns of the ordinal window, so
    # filtering on them cannot renumber the survivors.
    clauses += [f"d.bbox_{c} IS NOT NULL" for c in ("x1", "y1", "x2", "y2")]
    params: list = []
    if dataset is not None:
        clauses.append("d.dataset_id = ?")
        params.append(dataset)
    if camera is not None:
        clauses.append("d.camera_id = ?")
        params.append(camera)
    if day is not None:
        clauses.append("d.dataset_id IN (SELECT dataset_id FROM datasets WHERE day = CAST(? AS DATE))")
        params.append(day)

    threshold = hex_threshold(cfg.overlap_fraction)
    # The trailing two are the annotator, once for the `mine` anti-join and once
    # for `mymask`. Order matters: these are positional and must match the order
    # the joins appear in the CTE below.
    params += [threshold, threshold, cfg.overlap_targets, cfg.targets_per_instance,
               annotator, annotator]

    return (
        f"""
        WITH ranked AS (
            -- d.* because instance_key_sql folds the ordinal window in and needs
            -- every column it hashes AND every column that window orders by.
            -- `ordinal` is projected separately from the same producer because the
            -- item payload carries it back: the client cannot derive the ordinal
            -- from its anchor, so it echoes the one it was served and
            -- `verify_anchor` re-hashes it. Restating this window by hand instead
            -- would serve keys no submit could reproduce.
            SELECT d.*, {labels_db.instance_ordinal_sql('d')} AS ordinal
            FROM detections d
            WHERE {' AND '.join(clauses)}
        ),
        keyed AS (
            SELECT r.*, {labels_db.instance_key_sql('r')} AS instance_key FROM ranked r
        ),
        scoped AS (
            SELECT k.instance_key, k.dataset_id, k.camera_id, k.frame_path, k.ordinal,
                   k.score, k.bbox_x1, k.bbox_y1, k.bbox_x2, k.bbox_y2,
                   ds.day AS day,
                   -- The MODEL's outline. `ranked` is SELECT d.* so it arrives
                   -- here for free, but this projection is explicit, so without
                   -- naming it the column is dropped before _item ever sees it.
                   k.mask_poly AS model_mask_poly,
                   k.mask_parts AS model_mask_parts,
                   coalesce(cov.n_annotators_labeled, 0) AS n_labeled,
                   coalesce(cov.n_annotators_skipped, 0) AS n_skipped,
                   (substr(k.instance_key, 1, 4) < ?) AS overlap,
                   CASE WHEN substr(k.instance_key, 1, 4) < ? THEN ? ELSE ? END AS target,
                   (mine.effective_key IS NOT NULL) AS mine_done,
                   (fp.effective_key IS NOT NULL) AS false_positive,
                   mymask.polygon AS my_mask_poly,
                   coalesce(mymask.n_verdicts, 0) > 0 AS geom_done
            FROM keyed k
            LEFT JOIN datasets ds ON ds.dataset_id = k.dataset_id
            -- Every join is on effective_key, never instance_key (§2.4): after a
            -- reconciliation moved a label onto a re-ingested detection, joining
            -- the audit column would re-serve an entire already-labelled day as
            -- fresh work and stop pairing the annotators who answered it.
            LEFT JOIN {alias}.v_instance_coverage cov ON cov.effective_key = k.instance_key
            -- Anti-join, not a correlated EXISTS: DuckDB will not take a
            -- correlated subquery inside the FILTER clauses `progress` needs.
            LEFT JOIN (SELECT DISTINCT effective_key FROM {alias}.v_current_annotations
                       WHERE annotator = ?) mine ON mine.effective_key = k.instance_key
            -- Declared not-an-animal by someone (M4a §4.2). Unlike a skip this
            -- retires the instance for EVERYONE, not just its author: there are
            -- no questions to ask about a cow that is not there, and re-serving
            -- it would collect answers about a shadow. Not annotator-scoped, and
            -- an anti-join for the same reason `mine` is one.
            LEFT JOIN {alias}.v_false_positives fp ON fp.effective_key = k.instance_key
            -- MY OWN standing geometry verdict. Two things come out of one
            -- grouped join: the corrected polygon if I drew one, and whether I
            -- have said ANYTHING about this instance's geometry — which is what
            -- tells the client the geometry step is already passed.
            --
            -- Scoped to this annotator on purpose, exactly like `mine`: showing
            -- another annotator's traced outline would anchor this one's
            -- judgement, the same independence rule that keeps `n_annotators`
            -- from ever carrying WHAT the others answered. It also means the
            -- gate is passed per-annotator, which is right — the step is a
            -- measurement each of them makes.
            --
            -- Until masks are persisted (M4 phase 0) `polygon` here is the only
            -- outline that exists for an instance; afterwards it COALESCEs over
            -- detections.mask_poly.
            LEFT JOIN (SELECT effective_key,
                              max(polygon) FILTER (WHERE kind = 'polygon'
                                                   AND polygon IS NOT NULL) AS polygon,
                              count(*) AS n_verdicts
                       FROM {alias}.v_current_mask_edits
                       WHERE annotator = ? GROUP BY effective_key)
                      mymask ON mymask.effective_key = k.instance_key
        )""",
        params,
    )


def queue(con: duckdb.DuckDBPyConnection, config: Config, *, annotator: str,
          limit: int | None = None, exclude: Sequence[str] = (),
          camera: str | None = None, day: str | None = None,
          dataset: str | None = None, mine: str = "todo", order: str = "fresh",
          session_id: str | None = None) -> dict:
    """One batch of instances to label, plus the policy and scope that produced it.

    Returns `{"items": [...], "matching": int, "policy": {...}, "filters": {...}}`;
    the item shape is frozen in §5.3 and mirrored by `LabelItem` in `types.ts`.

    **No leasing, no reservation, no claiming.** Two annotators may be served the
    same instance at the same moment; the cost is one extra independent annotation,
    which is the data this feature exists to collect. A lease table would make this
    GET a *writer* on the main DB — DuckDB's single-writer lock on the hottest read
    path — and would be unrecoverable across the restart every
    `docker compose up -d --build` performs. It also makes this GET idempotent, so
    React 19 StrictMode's double-invoked effect is harmless by construction.

    **Coverage counts labels only.** Skips feed the separate `skip_retire` counter,
    so an instance one annotator found unjudgeable is still offered to the next and
    only retires once `skip_retire` distinct annotators have declined it. Counting a
    skip as coverage would retire exactly the ambiguous instances where inter-rater
    variability is most informative.

    **Pagination is `limit` + `exclude`, never an offset or a cursor.** Other
    annotators retire items out from under you — whatever reaches `target` leaves
    the pool mid-session — so any positional cursor would skip or repeat items.
    The queue is self-consuming instead: whatever you label or skip drops out of
    the anti-join, so re-fetching always advances.

    `dataset` defaults to the whole DB and deliberately does not go through
    `resolve_ds` — labeling is cross-day by design. Every response echoes the
    applied scope in `filters`, which is the defence against `lib/api.ts`'s
    `withDs()` stamping the currently-selected day onto a cross-day queue.
    """
    cfg = config.annotation
    n = max(1, min(int(limit or cfg.batch_size), cfg.max_batch_size))
    excl = list(dict.fromkeys(k for k in exclude if k))[:_MAX_EXCLUDE]
    mine = "all" if mine == "all" else "todo"
    order = "spread" if order == "spread" else "fresh"

    with attached_labels(con, config) as alias:
        cte, params = _scan_sql(cfg, alias, annotator=annotator, dataset=dataset,
                                camera=camera, day=day)
        # `NOT s.false_positive` is unconditional — it is not a preference like
        # `mine`, and no filter combination may bring back an instance somebody
        # has judged not to be an animal.
        where = ["s.n_labeled < s.target", "s.n_skipped < ?", "NOT s.false_positive"]
        params.append(cfg.skip_retire)
        if excl:
            where.append(f"s.instance_key NOT IN ({', '.join('?' * len(excl))})")
            params += excl
        if mine == "todo":
            where.append("NOT s.mine_done")
        # `order=fresh` drains the newest day first so a new upload observably
        # appears at the head of the queue; `order=spread` drops the day term for a
        # stratified sample across the whole season. The md5 tail is a stable
        # per-annotator permutation: two annotators walk the same pool in different
        # orders, so they meet in the middle rather than racing down one list.
        #
        # Deliberately NOT ordered by `n_labeled ASC` (M3 UX §6.7): sorting
        # zero-coverage items ahead of once-labeled ones means no instance gets its
        # second label until the whole pool has been swept once, so every pair
        # feeding the kappa queries would come from the fatigued back half of each
        # session. The WHERE clause already caps coverage at target.
        day_term = "s.day DESC NULLS LAST, " if order == "fresh" else ""
        params.append(annotator)
        params.append(n)
        rows = _rows(con, f"""{cte}
            SELECT s.*, count(*) OVER () AS matching
            FROM scoped s
            WHERE {' AND '.join(where)}
            ORDER BY {day_term}md5(? || s.instance_key)
            LIMIT ?
            """, params)

        # A 'served' row per item, written into labels.duckdb through the alias —
        # a write, but not to the main DB, so the read path the no-leasing decision
        # protects is untouched. It buys a non-forgeable server-side
        # time_on_task_ms, measurable abandonment (served with no matching
        # annotation), and the server/client delta that detects tab-away (§4.2).
        event_ids: list[int | None] = []
        for r in rows:
            ev = con.execute(
                f"INSERT INTO {alias}.label_events (session_id, annotator, kind, instance_key) "
                "VALUES (?, ?, 'served', ?) RETURNING event_id",
                [session_id, annotator, r["instance_key"]],
            ).fetchone()
            event_ids.append(int(ev[0]) if ev else None)

    items = [_item(r, cfg, event_ids[i]) for i, r in enumerate(rows)]
    return {
        "items": items,
        "matching": int(rows[0]["matching"]) if rows else 0,
        "policy": {
            "targets_per_instance": cfg.targets_per_instance,
            "overlap_fraction": cfg.overlap_fraction,
            "overlap_targets": cfg.overlap_targets,
            "skip_retire": cfg.skip_retire,
            "batch_size": cfg.batch_size,
            "max_batch_size": cfg.max_batch_size,
        },
        "filters": {"dataset": dataset, "camera": camera, "day": day,
                    "mine": mine, "order": order, "limit": n, "excluded": len(excl)},
    }


def progress(con: duckdb.DuckDBPyConnection, config: Config, *, annotator: str = "",
             dataset: str | None = None, camera: str | None = None) -> dict:
    """Pool-level counts for the Label page's progress panel, over the same scan.

    Only the numbers that need the main DB live here — how big the labelable pool
    is and how much of it is still servable. Per-annotator effort (items/hour,
    time on task) is `labels_db`'s `SQL_EFFORT_BY_ANNOTATOR`, and agreement is its
    `agreement()`; the route merges the two rather than either module growing the
    other's SQL.

    `pool_total` is the terminal-state discriminator the frontend needs: zero means
    "no footage processed at all" (link to /data), while zero `remaining` against a
    non-zero pool means "caught up" (§5.6).
    """
    cfg = config.annotation
    with attached_labels(con, config) as alias:
        cte, params = _scan_sql(cfg, alias, annotator=annotator, dataset=dataset,
                                camera=camera, day=None)
        params += [cfg.skip_retire, cfg.skip_retire, cfg.skip_retire]
        rows = _rows(con, f"""{cte}
            SELECT count(*)                                              AS pool_total,
                   count(*) FILTER (WHERE n_labeled > 0)                 AS labeled,
                   count(*) FILTER (WHERE n_labeled >= target)           AS at_target,
                   count(*) FILTER (WHERE n_labeled < target
                                      AND n_skipped >= ?)                AS retired,
                   -- `remaining` and `mine_remaining` must agree with what the
                   -- queue will actually serve, or the panel counts down towards
                   -- a number of items that can never arrive and the page never
                   -- reaches "you're caught up".
                   count(*) FILTER (WHERE n_labeled < target
                                      AND n_skipped < ?
                                      AND NOT false_positive)            AS remaining,
                   count(*) FILTER (WHERE n_labeled < target
                                      AND n_skipped < ? AND NOT mine_done
                                      AND NOT false_positive)            AS mine_remaining,
                   -- The two halves of an honest progress bar: annotations that
                   -- exist against annotations the policy is asking for. Summing
                   -- `target` rather than multiplying by targets_per_instance is
                   -- what makes the 20% deep-overlap slice show up in the total.
                   coalesce(sum(n_labeled), 0)                           AS annotations_labeled,
                   coalesce(sum(target), 0)                              AS targets_total
            FROM scoped
            """, params)
    out = {k: int(v or 0) for k, v in rows[0].items()} if rows else {}
    out["filters"] = {"dataset": dataset, "camera": camera, "annotator": annotator}
    out["policy"] = {"targets_per_instance": cfg.targets_per_instance,
                     "overlap_targets": cfg.overlap_targets,
                     "skip_retire": cfg.skip_retire}
    return out


def _rows(con: duckdb.DuckDBPyConnection, sql: str, params: list) -> list[dict]:
    """Fetch as dicts, not via `.df()`: pandas would coerce the NULL `dataset_id`
    and `score` columns to NaN, which is not JSON and does not compare equal to
    None anywhere downstream."""
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def _item(row: dict, cfg: AnnotationCfg, serve_event_id: int | None) -> dict:
    """One queue item in the frozen §5.3 shape.

    Deliberately absent: the frame's clock time. The caption shows day + camera and
    nothing finer, for the same reason the banner is masked — time of day predicts
    the "Sun exposure" answer, and shipping it in the payload would hand it to the
    annotator through devtools even if the UI never drew it. Traceability is served
    by `instance_key`, which the flag payload carries.
    """
    frame_file = labels_db.frame_basename(row["frame_path"])
    bbox = [float(row["bbox_x1"]), float(row["bbox_y1"]),
            float(row["bbox_x2"]), float(row["bbox_y2"])]
    _src, out, ring = crop_geometry(bbox, cfg.crop_pad, cfg.crop_max_width)
    day = row["day"]

    # The outline, in BOTH spaces, both derived server-side from the one stored
    # polygon (M4a §4.1). `mask` is crop-local because that is what the editor and
    # the ring draw in; `mask_frame` is full-frame because that is what the
    # hold-Space peek draws over, and its viewBox is the frame's own dimensions.
    # Sending both rather than converting on the client is the whole point: a
    # second copy of crop_geometry's arithmetic in TypeScript is how a saved
    # polygon would silently shear away from the pixels it was drawn on.
    mask_frame: list[list[float]] | None = None
    mask_crop: list[list[float]] | None = None
    # MY OWN CORRECTION WINS over the model's outline. Both are full-frame px, so
    # this is a plain coalesce and not a conversion — and the precedence is the
    # whole point: having corrected an outline, the annotator must not be handed
    # the model's version back on the next visit as though the work never
    # happened. `mask_seed` reports which one they are looking at, so a submitted
    # correction records what it was actually drawn over.
    raw = row.get("my_mask_poly")
    seed_kind = "edit" if raw else ("model" if row.get("model_mask_poly") else "bbox")
    if not raw:
        raw = row.get("model_mask_poly")
    if raw:
        try:
            pts = json.loads(raw)
        except (TypeError, ValueError):
            pts = None
        if isinstance(pts, list) and len(pts) >= 3:
            mask_frame = [[float(p[0]), float(p[1])] for p in pts]
            # CLAMPED to the crop the annotator is actually served. A YOLO mask is
            # cropped to its own box by the model, but the SAM2 backend applies no
            # such crop and the stored bbox is the DINO box — so a mask can bleed
            # past the padded square. Unclamped, the annotator would be shown an
            # outline running off the canvas, nudge one vertex, and have the save
            # rejected by the route's inside-the-crop check AFTER doing the work.
            # Clamping at the edge is honest: it is the shape as far as this crop
            # can show it.
            mask_crop = [[round(min(max(v, 0.0), float(out)), 2) for v in p]
                         for p in frame_to_crop(mask_frame, bbox, pad=cfg.crop_pad,
                                                max_width=cfg.crop_max_width)]
    # Original dimensions, for the peek overlay's viewBox. One lazy header open
    # per item, the same cost class as the frame_sig read below, and the only way
    # the client can place a full-frame coordinate on a DOWNSCALED frame image.
    frame_w, frame_h = frame_size(row["frame_path"])
    return {
        "instance_key": row["instance_key"],
        "dataset_id": row["dataset_id"],
        "day": day.isoformat() if day is not None else None,
        "camera_id": row["camera_id"],
        "frame_file": frame_file,
        "bbox": bbox,
        "ordinal": int(row["ordinal"]),
        "score": float(row["score"]) if row["score"] is not None else None,
        # Threaded onto the item so the submit body carries it without a second
        # read: a stat plus 64 KiB per item, against a crop the annotator is about
        # to wait on a full JPEG decode for.
        "frame_sig": labels_db.frame_sig(row["frame_path"]),
        "crop_url": crop_url(camera_id=row["camera_id"], frame_file=frame_file,
                             bbox=bbox, dataset_id=row["dataset_id"],
                             pad=cfg.crop_pad, max_width=cfg.crop_max_width),
        # The uncropped frame behind the same instance, for hold-to-peek. Built
        # here for crop_url's reason, which is not a style preference: a
        # client-built URL routes through lib/api.ts's withDs() and would 404
        # every item whose day is not the selected one. Banner-masked by its
        # route, so the peek leaks no more clock time than the crop does.
        "frame_url": frame_url(camera_id=row["camera_id"], frame_file=frame_file,
                               dataset_id=row["dataset_id"]),
        "crop_w": out,
        "crop_h": out,
        "ring": [round(v, 2) for v in ring],
        # The frame's ORIGINAL size — null when the JPEG is gone, which the
        # client reads as "draw no overlay" rather than drawing a misplaced one.
        "frame_w": frame_w,
        "frame_h": frame_h,
        "mask": mask_crop,
        "mask_frame": mask_frame,
        # WHICH outline the annotator is looking at: their own correction, the
        # model's, or (when neither exists) the ring rectangle the editor falls
        # back to. Echoed on submit as `seeded_from`, so a stored correction says
        # what it was drawn over — the difference between "the annotator improved
        # the model" and "the annotator refined their own earlier pass", which is
        # the difference between a model-quality statistic and a self-consistency
        # one.
        "mask_seed": seed_kind if mask_crop is not None else "bbox",
        "mask_parts": (int(row["model_mask_parts"])
                       if row.get("model_mask_parts") is not None else None),
        # Whether THIS annotator has already judged this instance's geometry.
        # Served rather than remembered by the client: the step's memory used to
        # live in React state, so a reload re-asked about a cow the annotator had
        # already corrected — and pressing Enter on that second asking is what
        # used to destroy the correction (labels_db.submit_mask_edit).
        "geom_done": bool(row.get("geom_done")),
        # How many annotators already answered it — never WHAT they answered.
        # Showing the distribution would anchor the next annotator and destroy the
        # independence the agreement statistic assumes.
        "n_annotators": int(row["n_labeled"]),
        "target": int(row["target"]),
        "overlap": bool(row["overlap"]),
        "serve_event_id": serve_event_id,
    }


# --------------------------------------------------------------------------- the crop
def crop_url(*, camera_id: str, frame_file: str, bbox: Sequence[float],
             dataset_id: str | None = None, pad: float, max_width: int) -> str:
    """The `/api/img/label-crop/...` URL for one instance, built SERVER-side.

    The frontend never constructs this. Every other image URL it builds goes
    through `lib/api.ts`'s `withDs()`, which stamps the currently-selected day onto
    any `/api/` URL — and the queue is cross-day by design, so a client-built URL
    would 404 every item that came from another day.

    Coordinates are formatted with `repr`, not `%g`: the endpoint re-derives the
    crop geometry from these numbers and must land on the same `out_size` and the
    same source box the queue already told the client about, so six significant
    digits is not enough.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    params: list[tuple[str, str]] = []
    if dataset_id:
        params.append(("dataset", dataset_id))
    params += [("x1", repr(x1)), ("y1", repr(y1)), ("x2", repr(x2)), ("y2", repr(y2)),
               ("pad", repr(float(pad))), ("w", str(int(max_width)))]
    return (f"/api/img/label-crop/{quote(camera_id, safe='')}/{quote(frame_file, safe='')}"
            f"?{urlencode(params)}")


def frame_url(*, camera_id: str, frame_file: str, dataset_id: str | None = None,
              max_width: int = FRAME_MAX_WIDTH) -> str:
    """The `/api/img/label-frame/...` URL for one instance, built SERVER-side.

    Same rule as `crop_url` and for the same reason: the frontend must NEVER
    construct this. Every image URL `lib/api.ts` builds goes through `withDs()`,
    which stamps the currently-selected day onto any `/api/` URL — and the queue
    is cross-day by design, so a client-built URL would 404 the moment the
    annotator holds space on an item from any day but the selected one. The
    failure would be invisible on the happy path (single-day dataset, local
    testing) and total in production.

    `dataset` is carried exactly as `crop_url` carries it: omitted entirely when
    the row has no dataset_id, so the pre-dataset flat layout in
    `frame_path_for` is still reachable rather than being sent an empty segment.

    `w` rides in the URL rather than being implied by the server so the ETag the
    route computes covers the size the client actually asked for, and so bumping
    FRAME_MAX_WIDTH changes the URL instead of silently reusing cached pixels at
    the old width.
    """
    params: list[tuple[str, str]] = []
    if dataset_id:
        params.append(("dataset", dataset_id))
    params.append(("w", str(int(max_width))))
    return (f"/api/img/label-frame/{quote(camera_id, safe='')}/{quote(frame_file, safe='')}"
            f"?{urlencode(params)}")


def crop_geometry(bbox: Sequence[float], pad: float, max_width: int
                  ) -> tuple[tuple[int, int, int, int], int, tuple[float, float, float, float]]:
    """Pure geometry for one crop: `(src_box, out_size, ring)`.

    `src_box` is a SQUARE in full-frame pixels, `(x0, y0, x1, y1)`, and may extend
    past the frame edge — the renderer neutral-fills what falls outside instead of
    clamping. That is what makes this computable without knowing the frame's
    dimensions, so the queue can emit `crop_w`, `crop_h` and `ring` per item from
    the bbox alone, with no PIL header-open and no filesystem touch on what is
    otherwise a pure-DB endpoint. It also fixes the tile-shape jitter per-axis
    clamping would cause at frame borders, which is visually exhausting over
    hundreds of items.

    `out_size` is one number because the canvas is always square: `crop_w == crop_h`.
    `ring` is the bbox in crop-local pixels, `(x0, y0, x1, y1)` — the client draws
    it as SVG over the returned image, so the stroke stays a hairline at any
    rendered size, hold-to-hide costs no network, and a style change does not
    invalidate every cached crop.

    Rounding is `floor(v + 0.5)`, never `round()`: Python's `round` is banker's and
    DuckDB's is half-away-from-zero, and `src_box` is snapped to whole source pixels
    so `ring` and the pixels the renderer actually cuts share one origin rather than
    drifting by a sub-pixel.
    """
    x1, y1, x2, y2 = (float(v) for v in bbox[:4])
    # Ultralytics emits boxes unclamped and, rarely, inverted (yolo_seg.py:53);
    # a negative side would make `side` collapse and the crop come back 1x1.
    if x2 < x1:
        x1, x2 = x2, x1
    if y2 < y1:
        y1, y2 = y2, y1
    bw = max(x2 - x1, 1.0)
    bh = max(y2 - y1, 1.0)
    # Padding is a fraction of the LONGER side, so a long thin cow and a square one
    # get the same *relative* context rather than a sliver for the thin one.
    side = max(bw, bh) * (1.0 + 2.0 * max(float(pad), 0.0))
    n = max(1, int(math.floor(side + 0.5)))
    x0 = int(math.floor((x1 + x2) / 2.0 - n / 2.0))
    y0 = int(math.floor((y1 + y2) / 2.0 - n / 2.0))
    scale = min(1.0, float(max_width) / n) if max_width > 0 else 1.0
    out = max(1, int(math.floor(n * scale + 0.5)))
    ring = ((x1 - x0) * scale, (y1 - y0) * scale, (x2 - x0) * scale, (y2 - y0) * scale)
    return (x0, y0, x0 + n, y0 + n), out, ring


def crop_to_frame(points: Sequence[Sequence[float]], bbox: Sequence[float], *,
                  pad: float, max_width: int) -> list[list[float]]:
    """Crop-local px -> full-frame px, the inverse of `crop_geometry`'s `ring`.

    The outline editor edits in the crop's coordinate space (M4a §4.1) because
    that is the space `ring` already established and the client can draw in with
    no math; the STORE is full-frame px, because that is the space `bbox_*`,
    `scene/regions.py` and the YOLO-seg export all live in. Something has to
    convert, and it is this — server-side, once, next to the forward transform it
    inverts, so the two cannot drift apart. A second copy of this arithmetic
    anywhere else is how every saved polygon would silently shear.

    Derived from `ring`'s own line: `ring_x = (x - x0) * scale`, therefore
    `x = x0 + crop_x / scale`. `scale` is recomputed from the same inputs rather
    than passed in, so a caller cannot supply one that disagrees with the crop
    the annotator was actually served."""
    (x0, y0, x1c, _y1c), _out, _ring = crop_geometry(bbox, pad, max_width)
    n = max(1, x1c - x0)
    scale = min(1.0, float(max_width) / n) if max_width > 0 else 1.0
    if scale <= 0:
        raise ValueError("degenerate crop scale")
    return [[x0 + float(px) / scale, y0 + float(py) / scale] for px, py in points]


def frame_to_crop(points: Sequence[Sequence[float]], bbox: Sequence[float], *,
                  pad: float, max_width: int) -> list[list[float]]:
    """Full-frame px -> crop-local px. The exact inverse of `crop_to_frame`.

    Storage is full-frame (it is the space `bbox_*` and the export live in) while
    the editor works crop-local (the space `ring` established). Both directions
    are therefore needed, and both live here, beside `crop_geometry`, so a change
    to the crop's shape moves all three together. A polygon that round-trips
    through this pair must come back where it started — that is what the
    round-trip test pins."""
    (x0, y0, x1c, _y1c), _out, _ring = crop_geometry(bbox, pad, max_width)
    n = max(1, x1c - x0)
    scale = min(1.0, float(max_width) / n) if max_width > 0 else 1.0
    return [[(float(px) - x0) * scale, (float(py) - y0) * scale] for px, py in points]


def frame_size(frame_path: str | Path) -> tuple[int | None, int | None]:
    """The frame's ORIGINAL pixel dimensions, or `(None, None)`.

    Needed because `/api/img/label-frame` serves the frame DOWNSCALED to
    `FRAME_MAX_WIDTH`, while `bbox` and every stored polygon are in original
    full-frame px. To draw either on the peeked frame the client needs the
    original dimensions as its SVG viewBox — the served image is a uniform scale
    of the original, so one viewBox stretched over the rendered <img> aligns
    exactly, whatever the downscale turned out to be. Without this the overlay
    would be off by the scale factor, which is worse than no overlay: it would
    point confidently at the wrong animal.

    PIL's open is lazy — this parses the JPEG header and never decodes pixels —
    so it is the same cost class as the `frame_sig` read the item already does.
    A missing or torn frame is routine (a re-ingest rmtrees the artifacts under a
    live queue) and yields `(None, None)`; the client then draws no overlay
    rather than a misplaced one."""
    from PIL import Image

    try:
        with Image.open(frame_path) as im:
            w, h = im.size
        return int(w), int(h)
    except Exception:
        return None, None


def frame_path_for(config: Config, dataset_id: str | None, camera_id: str,
                   frame_file: str) -> Path:
    """The on-disk JPEG for one frame, rebuilt from config plus validated ids.

    Mirrors `ingest/video.py:60-62,84` including the legacy flat layout kept for
    pre-dataset ingests. This is a deliberate departure from the house rule "the
    path always comes from the DB": a DB lookup here is a full `frames` scan for
    every image an annotator sees, and the frame filename is one of the very few
    places in this codebase where a *total* whitelist is available.

    It only joins. The caller applies §4.5's three whitelists — `_safe_path_id` on
    the dataset, `uploads.valid_camera_id` on the camera, `_safe_frame_file` on the
    filename — and the resolve-under-`artifacts_dir` assertion, BEFORE calling it.
    """
    base = Path(config.paths.artifacts_dir)
    if dataset_id:
        base = base / dataset_id
    return base / "frames" / camera_id / frame_file


def mask_banner(canvas, *, frame_h: int, y0: int, cfg: AnnotationCfg) -> float:
    """Paint the Brinno timestamp band over `canvas`; return the fraction covered.

    `canvas` is any RGB image cut from a frame `frame_h` pixels tall whose top row
    is full-frame row `y0` — the crop's padded square (`y0` = the square's origin,
    which may be negative) or a whole frame (`y0` = 0). The band's first row is
    fixed in FULL-FRAME coordinates and translated into canvas coordinates here,
    which is the only reason one function can serve both views.

    Factored out of `render_crop` when `/api/img/label-frame` was added rather
    than copied into it. A full frame shows MORE of the burned-in clock than any
    crop does, and two copies of this arithmetic is exactly how one view starts
    leaking the wall-clock time while the other stays masked. Time of day IS the
    "Sun exposure" answer (§4.5), so a drift here inflates agreement instead of
    failing visibly — nothing about the output looks wrong.

    Returns the covered fraction of the canvas height so each caller can decide
    what a mostly-banner result means: `render_crop` refuses it outright rather
    than serve an all-grey tile; `render_frame` cannot reach that case, since the
    band is ~4% of a full frame by construction. Returns 0.0 and paints nothing
    when masking is configured off, so the refusal test stays a plain comparison.

    Mutates `canvas` in place and must run BEFORE any downscale: the band's first
    row is derived from the frame's own height, so resizing first moves the line
    out from under it.
    """
    if not cfg.mask_timestamp_banner:
        return 0.0
    cw, ch = canvas.size
    # `start` may be NEGATIVE — these cameras look down and the herd crosses the
    # bottom of the field of view, so a padded crop of a low cow can begin BELOW
    # the band line. The max(0, ...) clamp below is what makes that case paint the
    # whole canvas, which is why the covered fraction is reported rather than
    # swallowed.
    start = int(frame_h * _BANNER_TOP) - y0
    if start >= ch:
        return 0.0
    top = max(0, start)
    canvas.paste(_FILL, (0, top, cw, ch))
    return (ch - top) / ch


def render_crop(frame_path: str | Path, bbox: Sequence[float], *, pad: float,
                max_width: int, cfg: AnnotationCfg) -> tuple[bytes, str | None]:
    """Cut the padded square out of one frame, mask the Brinno banner, encode JPEG.

    Returns `(jpeg_bytes, frame_sig)`. **Empty bytes is the only failure channel
    and always means 404** — a missing or unreadable JPEG is a routine condition
    here (a re-ingest rmtrees the frames out from under a queue the client is still
    holding), never a 500, and never a blank tile.

    The ring is NOT baked in; the client draws it over these pixels from the
    crop-local `ring` the queue supplied. There is one rendering and no `?ring=`
    parameter.

    Banner masking is a methodological requirement, not polish. Brinno burns
    wall-clock time into the bottom ~4% of every frame, and time of day correlates
    directly with the "Sun exposure" answer — a visible banner hands the annotator
    the answer and inflates agreement artificially.
    """
    from PIL import Image

    p = Path(frame_path)
    # Fingerprint from the bytes we are opening anyway (§2.3), rather than a second
    # pass over the file. Missing or unreadable is routine, not exceptional:
    # pipeline.ingest rmtrees artifacts/<dataset_id> while a client may still be
    # holding a queue full of URLs into it.
    try:
        with open(p, "rb") as f:
            head = f.read(labels_db.FRAME_SIG_BYTES)
            size = f.seek(0, 2)
    except OSError:
        return b"", None
    sig = labels_db.frame_sig_of(size, head)
    src, out, _ring = crop_geometry(bbox, pad, max_width)
    x0, y0, x1, y1 = src
    n = x1 - x0
    try:
        with Image.open(p) as opened:
            im = opened.convert("RGB")
            w, h = im.size
            canvas = Image.new("RGB", (n, n), _FILL)
            # Paste only the part of the square that actually exists; everything
            # outside stays neutral, which is what keeps the tile square at the
            # frame edges instead of clamping to a different aspect per item.
            ix0, iy0 = max(0, x0), max(0, y0)
            ix1, iy1 = min(w, x1), min(h, y1)
            if ix1 > ix0 and iy1 > iy0:
                canvas.paste(im.crop((ix0, iy0, ix1, iy1)), (ix0 - x0, iy0 - y0))
    except Exception:  # noqa: BLE001 — a torn/half-written JPEG is a 404, not a 500
        return b"", sig

    # Shared with render_frame so the two views of the same pixels cannot drift
    # apart on where the band starts. Before the resize, deliberately.
    if mask_banner(canvas, frame_h=h, y0=y0, cfg=cfg) > cfg.max_banner_fraction:
        # A crop of a cow below the band line comes back entirely grey once the
        # mask is clamped to the canvas, and the annotator's honest "Not visible"
        # is then recorded as genuine ambiguity about a real animal: fabricated
        # ambiguity contaminating exactly the statistic this feature exists to
        # produce. So refuse outright rather than serve a blank tile.
        return b"", sig

    if out != n:
        canvas = canvas.resize((out, out), Image.LANCZOS)
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), sig


def render_frame(frame_path: str | Path, *, max_width: int,
                 cfg: AnnotationCfg) -> tuple[bytes, str | None]:
    """The WHOLE frame, banner-masked and downscaled, as JPEG — hold-to-peek's image.

    Returns `(jpeg_bytes, frame_sig)` on `render_crop`'s contract exactly:
    **empty bytes is the only failure channel and always means 404**, because a
    frame vanishing mid-session (a re-ingest rmtrees `artifacts/<dataset_id>`) is
    routine here, never a 500 and never a blank image.

    The banner mask is the entire reason this is a rendered route rather than a
    `FileResponse` of the JPEG already on disk. A full frame contains MORE of the
    burned-in Brinno clock strip than any crop of it does, so serving the file
    raw would hand the annotator the wall-clock time — which IS the "Sun
    exposure" answer (§4.5) — every single time they hold the key, and would do
    it invisibly. It goes through `mask_banner`, the one place the band geometry
    lives, so the peek and the crop can never disagree about where the band
    starts.

    No mostly-banner refusal: the band is ~4% of a frame's height by
    construction, so `render_crop`'s all-grey-tile failure mode cannot arise here
    and refusing would only be dead code pretending to be a guard.

    Masking runs at FULL resolution, before the downscale, exactly as in
    `render_crop`: the band's first row comes from the frame's own height, so
    resizing first would move the line out from under the mask.

    Aspect ratio is preserved and the image is only ever made smaller — `w` is a
    ceiling, not a target — so a narrow frame is served at its native size rather
    than being upscaled into blur.
    """
    from PIL import Image

    p = Path(frame_path)
    # One pass for the fingerprint, from the bytes we are opening anyway (§2.3).
    try:
        with open(p, "rb") as f:
            head = f.read(labels_db.FRAME_SIG_BYTES)
            size = f.seek(0, 2)
    except OSError:
        return b"", None
    sig = labels_db.frame_sig_of(size, head)
    try:
        with Image.open(p) as opened:
            # convert() copies, so the canvas outlives the closed file handle and
            # is safe to paste the mask onto.
            canvas = opened.convert("RGB")
    except Exception:  # noqa: BLE001 — a torn/half-written JPEG is a 404, not a 500
        return b"", sig

    w, h = canvas.size
    # y0=0: the canvas IS the frame, so canvas rows and full-frame rows coincide.
    mask_banner(canvas, frame_h=h, y0=0, cfg=cfg)

    if max_width > 0 and w > max_width:
        # floor(v + 0.5), never round(): banker's rounding is not what the rest of
        # this module's geometry uses (see crop_geometry).
        out_h = max(1, int(math.floor(h * (float(max_width) / w) + 0.5)))
        canvas = canvas.resize((max_width, out_h), Image.LANCZOS)
    buf = BytesIO()
    canvas.save(buf, format="JPEG", quality=_JPEG_QUALITY, optimize=True)
    return buf.getvalue(), sig
