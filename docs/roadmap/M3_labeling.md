# Workstream M3 — In-app Labeling (multi-annotator instance annotation)

> Standalone design for the **Label** page: a keyboard-first tool that shows one
> cow instance at a time and collects multi-class answers from several annotators
> over the same instances, so inter-rater agreement is measurable.
>
> **Naming, once:** a **group** is a question ("Sun exposure"); a **class** is an
> answer inside it ("Shaded"). The user's word "superclass" == group. This
> feature is unrelated to `config.label` / `LabelCfg`, which is the Stage-1b
> CVAT/FiftyOne round-trip driven by `label-select` / `label-export`. Two
> different things one word apart; every new symbol here is named `labels_*`
> or `annotation*`, never bare `label`.

---

## 1. What we're building and why

A new top-nav entry **Label**, positioned directly after **Data**. It serves one
detection at a time as a padded crop with the target animal ringed, asks two
required single-select questions, and records the answer with enough provenance
and telemetry to report on the labeling effort afterwards.

Three requirements shape everything below:

1. **New footage must enter the queue by itself.** So the queue is *derived* —
   a scan of `detections` anti-joined against the label store — not a
   materialised table. Detections are written from three places (`pipeline.segment`,
   `db.restore_clip`'s raw SQL, the CLI), so a materialised queue would need
   three hooks and would still go stale after `purge_dataset`.
2. **Several annotators must label the same instances.** So there is no leasing,
   no claiming, and no `UNIQUE (instance_key, annotator)`. Coverage targets
   default to **2** annotations per instance, not 1 — a target of 1 would make
   the first submission remove the instance from everyone else's queue and quietly
   defeat the entire point of the feature.
3. **Labels must outlive the detections they describe.** `pipeline.ingest` calls
   `purge_dataset` + `rmtree` on a re-upload, `api.delete_camera` purges one
   camera, `api.delete_dataset` moves a day into the archive DB. So labels live
   in their own DuckDB file, `data/labels.duckdb`, which nothing in the ingest
   path ever opens, and every annotation carries denormalised provenance.

That third requirement forces the hard problem, which gets its own section.

**Module ownership is fixed here and nowhere else.** Exactly one module declares
each thing:

| Module | Owns |
|---|---|
| `cownting/labels_db.py` | the DDL, `instance_key` (Python **and** SQL), taxonomy CRUD, annotation/event writes, reconciliation, agreement SQL |
| `cownting/labeling.py` | queue SQL + sampling policy, crop geometry + rendering, the ATTACH context manager, connection helpers |
| `cownting/labels_backup.py` | the weekly Discord job and its `backup_runs` table |
| `cownting/api.py` | thin HTTP routes only — no SQL, no key arithmetic |

Any function not listed in this document does not exist. `labeling.py` imports
`labels_db`; the reverse import is forbidden.

---

## 2. The stable instance key

### 2.1 Why `detection_id` cannot be it

`detections.detection_id` is `BIGINT DEFAULT nextval('seq_det')`, and `DET_COLS`
(`db.py:21-30`) **excludes it**. `clip_camera` moves rows out through
`clipped_detections` using `DET_COLS`, and `restore_clip` inserts them back the
same way — so a restore mints a *fresh* id for the same physical animal. The
source comment says so verbatim. On top of that, `purge_dataset` + re-ingest
destroys the ids outright, and a main DB restored from backup restarts `seq_det`
at 1, which would point an old label at a *different* cow.

A label keyed on `detection_id` is therefore not merely fragile: after an
undo-clip it silently re-attaches to the wrong animal, with full confidence and
no error anywhere. That is the failure mode this section exists to prevent.

### 2.2 The key

```
instance_key = substr(sha256(
    'v1' ⌷ coalesce(dataset_id,'') ⌷ camera_id ⌷ basename(frame_path)
         ⌷ q(bbox_x1) ⌷ q(bbox_y1) ⌷ q(bbox_x2) ⌷ q(bbox_y2) ⌷ ordinal
), 1, 32)
```

where `⌷` is `chr(31)` (ASCII unit separator — impossible in a dataset id, a
camera id under `CAMERA_ID_RE`, or a frame basename) and `q(v) = floor(v + 0.5)`.

Four deliberate choices:

**Every input is a `DET_COLS` column.** `dataset_id`, `camera_id`, `frame_path`
and `bbox_*` all round-trip through `clipped_detections` bit-identically, so a
clip → undo-clip cycle leaves the key unchanged while `detection_id` is re-minted.
No sequence value enters the key, so a rebuilt main DB cannot rename a cow.

**`frame_path` is reduced to its basename.** `frame_path` is
`str(artifacts_dir/<ds>/frames/<cam>/<idx:08d>.jpg)` (`ingest/video.py:84`) —
CWD-relative, OS-native separators. The same physical frame is a backslash string
on the Windows dev box and a slash string in the container; `pipeline.py:320-328`
records a shipped bug from exactly that. Everything the basename discards is
either already in the key (`dataset_id`, `camera_id`) or deployment config
(`artifacts_dir`), and `(dataset_id, camera_id, basename)` is bijective with
`(dataset, camera, frame_idx)`.

**Quantisation is `floor(v + 0.5)`, never `round()`.** Python's `round()` is
banker's rounding (`round(0.5) == 0`); DuckDB's is half-away-from-zero
(`round(0.5) == 1`). The queue mints keys in SQL and the submit path verifies
them in Python, so a `round()` on either side would reject roughly half of all
half-pixel coordinates with a confusing 400 while the queue kept working.
`floor()` also behaves identically on the negative coordinates Ultralytics emits
unclamped (`yolo_seg.py:53`).

**`ordinal` disambiguates same-bbox rows, and is itself content-derived.**
`detections` has no PK, no UNIQUE and nothing forbidding two rows in one frame
with the same quantised box, so without a tiebreak the key is not unique and
`count(DISTINCT annotator)` would count one annotator's single answer as coverage
of two detections. The ordinal is

```sql
dense_rank() OVER (
  PARTITION BY coalesce(dataset_id,''), camera_id, frame_path, qx1, qy1, qx2, qy2
  ORDER BY score DESC NULLS LAST, area_px DESC NULLS LAST,
           ground_px_x NULLS LAST, ground_px_y NULLS LAST, ts NULLS LAST
) - 1
```

`dense_rank`, **not** `row_number`. Rows tied on every ordering column are
byte-identical in content, and DuckDB's parallel scan may emit them in either
order — `row_number` would hand them arbitrary, non-reproducible ordinals, so
after a de-duplicating re-ingest a surviving row could present as ordinal 0 while
its only label was written under ordinal 1. `dense_rank` gives content-identical
rows the *same* ordinal and therefore the same key, which is also semantically
right: two boxes identical to the pixel produce a byte-identical crop that an
annotator physically cannot tell apart. All ordering columns are in `DET_COLS`
and none is rewritten after insert (`db.py:351-398` only touches `posture`,
`region_id`, `under_panel`, `panel_id`), so the ordinal survives clip/restore.

**Exactly two producers exist** — `labels_db.instance_key()` (Python) and
`labels_db.instance_key_sql(alias)` (a DuckDB expression) — and
`tests/test_labels_schema.py::test_python_and_sql_keys_agree` pins them to each
other. Divergence between them is the highest-severity silent failure in this
feature; that test is not skippable and must be green before any frontend work
starts. The client cannot compute the ordinal from its anchor alone, so it echoes
the ordinal the queue served, and `verify_anchor` re-hashes the full anchor
including it.

### 2.3 The pixel fingerprint

A content-derived key is derived over *metadata*, not over the image.
`frame_path` is deterministic in `(dataset_id, camera_id, frame_idx)`
(`video.py:60-84`), so re-uploading **different** footage for the same day
reproduces identical path strings over completely different pixels — and an exact
key match would then be a wrong-cow match made with full confidence.

Every annotation therefore also stores `frame_sig`: `blake2b-128` over
`file size ⌷ first 64 KiB` of the JPEG. `O(1)` on a 4K frame, and enough to
separate two unrelated captures. It is computed once by the crop endpoint (which
already stats and opens the file) and threaded through the queue payload to the
submit body, so the write path does no extra IO.

`frame_sig` is `NULL` whenever the image was unreadable or already gone at serve
time — normal, because `pipeline.ingest` rmtrees `artifacts/<dataset_id>` before
re-indexing. NULL is an explicit third state, not a silent pass: comparison uses
`IS NOT DISTINCT FROM` so NULL never accidentally satisfies or fails the check,
and the reconciler reports it as `attached_unverified`.

### 2.4 Re-attachment: `effective_key` is the only key anyone reads

The key survives clip/restore and archival by construction. It does **not**
survive a re-ingest with a changed detector, which shifts bboxes by a few pixels.
That is handled by an explicit, non-destructive reconciliation pass —
`labels_db.reconcile_dataset(lcon, mcon, dataset_id, run_id, actor)`.

`annotations` carries two columns:

- `instance_key` — what the annotator's submission hashed to. **Never rewritten.**
  It is the audit trail.
- `effective_key` — the key that exists in `detections` *now*. Set equal to
  `instance_key` at insert; updated **only** by the reconciler, alongside an
  `instance_key_aliases` row recording the move.

**Every consumer joins, groups and anti-joins on `effective_key`.** The queue's
anti-join, `v_current_answers`, `v_instance_coverage` and the agreement SQL all
read `effective_key`; none reads `instance_key`. This is the fix for the
otherwise-fatal case where the alias table is written but never resolved: without
it, a re-ingest splits the corpus into two disjoint key spaces, the queue re-serves
an entire already-labelled day as fresh work, and two annotators' answers about
the same cow stop pairing — so agreement for that day silently drops to zero with
a "healthy" reconciliation report on screen. Materialising the column rather than
resolving through a view also keeps the hot anti-join a single equality.

The state machine, per annotation, in order:

| Order | Test | State | Effect |
|---|---|---|---|
| 1 | recomputed key exists in `detections`, `frame_sig` matches | `attached` | none |
| 1b | …exists, `frame_sig` is NULL on either side | `attached_unverified` | none, counted separately |
| 1c | …exists, `frame_sig` **differs** | `hijacked` | **no** attach; reported |
| 2 | best IoU ≥ 0.70 with runner-up ≤ 0.50 over the same `(dataset_id, camera_id, frame_basename)` | `aliased` | alias row + `effective_key` update |
| 2b | as 2, but the target key is claimed by more than one source key | `ambiguous_merge` | **no** attach; reported |
| 3 | key present in `cownting_archive.duckdb` | `archived` | none; still renderable |
| 4 | the key exists only in `clipped_detections` | `clipped` | none; re-attaches on restore |
| 5 | otherwise | `orphan` | none |

Three properties are load-bearing:

**The alias mapping is injective per run.** Step 2b exists because a looser NMS
setting on re-ingest can merge two adjacent cows into one box; evaluated per
annotation, the runner-up guard passes for both source keys and both alias onto
the merged target. Once resolved that presents as two annotators giving different
answers about "the same" instance — a fabricated disagreement that drags kappa
down and conflates two animals irrecoverably. So after computing candidates the
reconciler rejects any `new_key` claimed by more than one `old_key`, leaves those
annotations as orphans with state `ambiguous_merge`, and records the collision.
`instance_key_aliases` has `PRIMARY KEY (old_key, new_key)` plus a
`UNIQUE (new_key, dataset_id)` guard so the invariant is enforced by the store,
not only by the code.

**Aliases fan out from the original key, never chain.** `old_key` is always the
annotation's own `instance_key`, so repeated re-ingests are idempotent and
resolution never needs a recursive CTE.

**The dataset-scoped candidate search has a fallback.** `cownting migrate` stamps
a derived `dataset_id` onto rows that had NULL, which changes every key on the
legacy partition. A candidate query scoped by the annotation's own (NULL)
`dataset_id` would find nothing and declare every label an orphan — while the
queue, keying off live detections, re-serves the whole set as new work. So when
the dataset-scoped candidate set is empty, step 2 retries scoped by
`(camera_id, frame_basename)` alone. `cownting migrate` additionally calls
`labels_db.rekey_after_migrate()`, which writes aliases directly for the affected
partition rather than relying on IoU.

**Who runs it.** Three triggers, all owned by this workstream:

1. `uploads._run` and `uploads._run_add_camera` call it at the end, beside
   `_camera_health` and advisory like it — a reconciliation failure must never
   fail an upload.
2. `cownting labels reconcile [--dataset X]` for manual runs.
3. `db.restore_clip` needs nothing: the key is unchanged.

And **before** the destructive path runs: `pipeline.ingest` on an already-labelled
day prints, and `/api/uploads` returns in its confirmation payload, the number of
annotations about to be put through reconciliation. `purge_dataset` + `rmtree` is
already the most destructive button in the app and has no decodability guard; it
should not also be the one that silently strands annotator hours.

---

## 3. Data model — `data/labels.duckdb`

`cownting/labels_db.py` owns all of this. `init_labels_db(con)` mirrors
`auth.init_auth` and follows `db.init_db`'s ordering discipline exactly:
sequences → `CREATE TABLE IF NOT EXISTS` → the forward-compat
`ALTER … ADD COLUMN IF NOT EXISTS` block → indexes → `CREATE OR REPLACE VIEW` →
idempotent seed. It is called once from `create_app`'s `_boot` block on its own
short-lived connection, and by every CLI entry point (the CLI can legitimately
run before the app has ever booted).

**No foreign keys anywhere**, matching the rest of the repo and avoiding DuckDB's
FK restrictions, which fight the soft-archive flow. **Connections are always
read-write**, including from the report and backup paths: DuckDB refuses a second
connection to one file with a different mode in one process, and that error text
matches none of `db.connect`'s retry substrings, so a `read_only` open surfaces as
an un-retried 500 (`api.py:189-196` documents this for the main DB).

### 3.1 DDL

```sql
CREATE SEQUENCE IF NOT EXISTS seq_annotation     START 1;
CREATE SEQUENCE IF NOT EXISTS seq_label_event    START 1;
CREATE SEQUENCE IF NOT EXISTS seq_taxonomy_audit START 1;
CREATE SEQUENCE IF NOT EXISTS seq_backup_run     START 1;

-- A GROUP is a question. Poweruser-editable at runtime; never hard-deleted.
CREATE TABLE IF NOT EXISTS label_groups (
    group_key    VARCHAR PRIMARY KEY,          -- stable slug, IMMUTABLE
    name         VARCHAR NOT NULL,
    description  VARCHAR,                      -- long form behind the (i) icon
    sort_order   INTEGER NOT NULL DEFAULT 100, -- ALSO the hotkey row index
    multi_select BOOLEAN NOT NULL DEFAULT FALSE,
    required     BOOLEAN NOT NULL DEFAULT TRUE,
    active       BOOLEAN NOT NULL DEFAULT TRUE,-- soft archive; never DELETE
    archived_at  TIMESTAMP,
    created_by   VARCHAR, created_at TIMESTAMP DEFAULT now(),
    updated_by   VARCHAR, updated_at TIMESTAMP
);

-- A CLASS is an option inside a group.
CREATE TABLE IF NOT EXISTS label_classes (
    class_key    VARCHAR PRIMARY KEY,          -- '<group_key>.<slug>', globally unique
    group_key    VARCHAR NOT NULL,
    name         VARCHAR NOT NULL,
    description  VARCHAR NOT NULL,             -- required: see §5.4
    sort_order   INTEGER NOT NULL DEFAULT 100,
    is_escape    BOOLEAN NOT NULL DEFAULT FALSE, -- the 'Cannot tell' hatch
    active       BOOLEAN NOT NULL DEFAULT TRUE,
    archived_at  TIMESTAMP,
    created_by   VARCHAR, created_at TIMESTAMP DEFAULT now(),
    updated_by   VARCHAR, updated_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS taxonomy_audit (
    audit_id    BIGINT PRIMARY KEY DEFAULT nextval('seq_taxonomy_audit'),
    at          TIMESTAMP DEFAULT now(),
    actor       VARCHAR,     -- real account username, or 'system' for the seed
    actor_role  VARCHAR,
    action      VARCHAR,     -- seed|create_group|update_group|archive_group|restore_group
                             -- |create_class|update_class|archive_class|restore_class|move
    target_kind VARCHAR,     -- 'group' | 'class'
    target_key  VARCHAR,
    before_json VARCHAR,
    after_json  VARCHAR,
    revision    BIGINT       -- taxonomy_revision AFTER this change
);

-- taxonomy_revision, schema_version, backup watermark.
CREATE TABLE IF NOT EXISTS label_meta (
    key VARCHAR PRIMARY KEY, value VARCHAR, updated_at TIMESTAMP DEFAULT now()
);

-- One row per (instance, annotator, submission). APPEND-ONLY.
CREATE TABLE IF NOT EXISTS annotations (
    annotation_id   BIGINT PRIMARY KEY DEFAULT nextval('seq_annotation'),
    -- identity ---------------------------------------------------------------
    instance_key    VARCHAR NOT NULL,   -- as submitted; NEVER rewritten
    effective_key   VARCHAR NOT NULL,   -- what joins to detections NOW (§2.4)
    key_version     VARCHAR NOT NULL DEFAULT 'v1',
    annotator       VARCHAR NOT NULL,   -- REAL account, or 'local' when auth is off
    version         INTEGER NOT NULL DEFAULT 1,
    superseded_at   TIMESTAMP,          -- NULL = the current answer
    -- outcome ----------------------------------------------------------------
    outcome         VARCHAR NOT NULL DEFAULT 'labeled', -- labeled|skipped|undone
    skip_reason     VARCHAR,            -- bad_crop|no_cow|multiple_cows|occluded|other
    flag_note       VARCHAR,            -- free text, length-capped at the API boundary
    -- denormalised provenance: the label stays self-describing after the
    -- detection row is purged, archived or clipped away ------------------------
    dataset_id      VARCHAR,
    camera_id       VARCHAR,
    frame_path      VARCHAR,            -- as stored (OS-native); renders the crop
    frame_basename  VARCHAR,            -- portable; the key's frame component
    frame_sig       VARCHAR,            -- pixel fingerprint at serve time (§2.3)
    ts              TIMESTAMP,          -- capture time; reporting only, never shown
    bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_x2 DOUBLE, bbox_y2 DOUBLE,
    ordinal         INTEGER NOT NULL DEFAULT 0,
    det_score       DOUBLE,
    -- telemetry --------------------------------------------------------------
    session_id      VARCHAR,            -- client labeling session (uuid4 hex)
    serve_event_id  BIGINT,             -- label_events row of kind 'served'
    served_at       TIMESTAMP,          -- server-measured, from that event
    submitted_at    TIMESTAMP DEFAULT now(),
    time_on_task_ms BIGINT,             -- server: submitted_at - served_at
    client_elapsed_ms BIGINT,           -- client active time (tab-away detector)
    input_mode      VARCHAR,            -- 'key' | 'mouse'
    annotator_role      VARCHAR,        -- EFFECTIVE role at submit
    annotator_real_role VARCHAR,        -- the account's real role
    acting_preview  BOOLEAN DEFAULT FALSE,
    auth_disabled   BOOLEAN DEFAULT FALSE,
    app_version     VARCHAR,
    taxonomy_revision BIGINT,           -- which taxonomy the annotator was served
    client_info     VARCHAR,            -- User-Agent, truncated to 200 chars
    viewport        VARCHAR,
    queue_reason    VARCHAR,            -- new|redundancy|relabel
    UNIQUE (instance_key, annotator, version)
);

-- The selected classes. One row each, so multi-select needs no schema change.
CREATE TABLE IF NOT EXISTS annotation_choices (
    annotation_id BIGINT  NOT NULL,
    class_key     VARCHAR NOT NULL,
    group_key     VARCHAR NOT NULL,  -- denormalised: reports never join taxonomy
    class_name    VARCHAR,           -- display name AT LABEL TIME (survives a rename)
    ordinal       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (annotation_id, class_key)
);

-- Append-only effort telemetry. 'served' is the non-forgeable time-on-task clock.
CREATE TABLE IF NOT EXISTS label_events (
    event_id     BIGINT PRIMARY KEY DEFAULT nextval('seq_label_event'),
    at           TIMESTAMP DEFAULT now(),
    session_id   VARCHAR,
    annotator    VARCHAR,
    kind         VARCHAR NOT NULL,  -- session_start|served|submitted|skipped|undo
                                    -- |relabel|info_opened|session_end
    instance_key VARCHAR,
    class_key    VARCHAR,           -- info_opened: WHICH description was read
    detail       VARCHAR
);

-- Re-attachment after a re-ingest. Non-destructive (§2.4).
CREATE TABLE IF NOT EXISTS instance_key_aliases (
    old_key        VARCHAR NOT NULL,
    new_key        VARCHAR NOT NULL,
    reason         VARCHAR,           -- reingest_iou | migrate | manual
    iou            DOUBLE,
    dataset_id     VARCHAR,
    camera_id      VARCHAR,
    frame_basename VARCHAR,
    created_at     TIMESTAMP DEFAULT now(),
    created_by     VARCHAR,
    PRIMARY KEY (old_key, new_key),
    UNIQUE (new_key, dataset_id)       -- enforces the injective mapping (§2.4)
);

CREATE TABLE IF NOT EXISTS reconciliations (
    run_id       VARCHAR NOT NULL,
    at           TIMESTAMP DEFAULT now(),
    dataset_id   VARCHAR,
    instance_key VARCHAR NOT NULL,
    state        VARCHAR NOT NULL,  -- attached|attached_unverified|aliased|hijacked
                                    -- |ambiguous_merge|clipped|archived|orphan
    new_key      VARCHAR,
    iou          DOUBLE,
    detail       VARCHAR,
    PRIMARY KEY (run_id, instance_key)
);

-- Owned by labels_backup.ensure_backup_tables(); lives here so the watermark
-- travels inside the very backup it describes.
CREATE TABLE IF NOT EXISTS backup_runs (
    run_id BIGINT DEFAULT nextval('seq_backup_run'),
    started_at TIMESTAMP, finished_at TIMESTAMP,
    status VARCHAR,          -- running | done | failed
    trigger VARCHAR,         -- schedule | cli | api | forced
    holder VARCHAR,          -- host:pid that claimed the run
    watermark_from TIMESTAMP, watermark_to TIMESTAMP,
    annotations BIGINT, new_annotations BIGINT,
    zip_path VARCHAR, zip_bytes BIGINT, discord VARCHAR, error VARCHAR
);
```

Indexes (PK/UNIQUE already build ART indexes; these cover the hot paths):

```sql
CREATE INDEX IF NOT EXISTS idx_ann_effective ON annotations (effective_key);
CREATE INDEX IF NOT EXISTS idx_ann_annotator ON annotations (annotator);
CREATE INDEX IF NOT EXISTS idx_ann_submitted ON annotations (submitted_at);
CREATE INDEX IF NOT EXISTS idx_ann_dataset   ON annotations (dataset_id);
CREATE INDEX IF NOT EXISTS idx_choice_ann    ON annotation_choices (annotation_id);
CREATE INDEX IF NOT EXISTS idx_choice_class  ON annotation_choices (class_key);
CREATE INDEX IF NOT EXISTS idx_event_at      ON label_events (at);
CREATE INDEX IF NOT EXISTS idx_alias_old     ON instance_key_aliases (old_key);
```

### 3.2 Views

`CREATE OR REPLACE`, not `IF NOT EXISTS`: a view is derived, so a code change to
its body must take effect on the next boot rather than being pinned to whatever
shipped first.

```sql
CREATE OR REPLACE VIEW v_current_annotations AS
SELECT * FROM annotations WHERE superseded_at IS NULL AND outcome <> 'undone';

CREATE OR REPLACE VIEW v_current_answers AS
SELECT a.annotation_id, a.effective_key, a.instance_key, a.annotator, a.version,
       a.dataset_id, a.camera_id, a.ts, a.submitted_at, a.time_on_task_ms,
       a.acting_preview, a.auth_disabled, a.taxonomy_revision,
       c.group_key, c.class_key, c.class_name
FROM annotations a JOIN annotation_choices c USING (annotation_id)
WHERE a.superseded_at IS NULL AND a.outcome = 'labeled';

-- What the queue and the coverage report read. Labeled and skipped are counted
-- SEPARATELY: a skip is a judgement about the instance, not coverage of it.
-- Counting a skip as coverage would retire exactly the ambiguous instances where
-- inter-rater variability is most informative, biasing the corpus toward easy
-- cases and inflating the reported kappa.
CREATE OR REPLACE VIEW v_instance_coverage AS
SELECT effective_key,
       count(DISTINCT annotator) FILTER (WHERE outcome = 'labeled') AS n_annotators_labeled,
       count(DISTINCT annotator) FILTER (WHERE outcome = 'skipped') AS n_annotators_skipped,
       count(*) FILTER (WHERE outcome = 'labeled') AS n_labeled,
       count(*) FILTER (WHERE outcome = 'skipped') AS n_skipped,
       max(submitted_at) AS last_submitted_at
FROM annotations WHERE superseded_at IS NULL GROUP BY effective_key;

-- Audit-only: shows where an alias moved a label. Not on any hot path.
CREATE OR REPLACE VIEW v_effective_key AS
SELECT annotation_id, instance_key AS label_key, effective_key,
       instance_key <> effective_key AS reattached
FROM annotations;
```

### 3.3 Write semantics

`submit_annotation(con, *, instance_key, effective_key, annotator, outcome,
choices, provenance, telemetry) -> int` appends **version + 1** and stamps
`superseded_at` on the annotator's previous current row. It never overwrites.

The product of this feature *is* annotator variability; an overwrite would destroy
the signal it exists to measure. An annotator changing their mind after a class
description is sharpened is a measurement (intra-rater reliability, taxonomy-churn
effect), not a correction. Append-only also makes the store safe to back up
incrementally by `submitted_at`, which the weekly job needs, and removes
read-modify-write from the hot write path.

Concurrency is optimistic: two racing submits both compute `version = n+1`, one
loses the `UNIQUE (instance_key, annotator, version)` race, `labels_db` raises
`ValueError`, and `api.py` maps it to **409** at the existing boundary. The whole
function runs in an explicit `BEGIN`/`COMMIT`/`ROLLBACK` — a half-written
annotation with no choice rows would look like an empty answer forever — so it
must only ever be called on a fresh per-request labels connection, never nested.

**Skips are annotations** with `outcome='skipped'` and a `skip_reason`, not a
separate table and not a 400. A skip carries the same provenance, telemetry and
uniqueness rule as a label; `multiple_cows` in particular is a direct signal that
the crop padding or the detector merged two animals. Agreement queries filter
`outcome='labeled'`; queue-health reports read `outcome='skipped'`.

**Undo is a supersede, not a delete.** `undo_last(con, annotator, instance_key)`
stamps `superseded_at` on that annotator's current row and marks it
`outcome='undone'`, writing a `label_events` row of `kind='undo'`. `annotation_id`
is never a client-supplied mutation key: it is a dense `nextval` sequence, so a
`DELETE /labels/{id}` route would let any plain user guess and destroy another
annotator's submission, and it would contradict the append-only rule that the rest
of the store depends on. There is no delete route anywhere in this feature.

**Identity is split.** The `annotator` column is always the *real*
`session['user']['username']`; the *gate* always uses `effective_role(request)`.
If the gate read the stored role, an admin previewing `user` could still edit the
taxonomy, defeating the preview. If identity followed the preview, that admin's
labels would masquerade as another annotator and poison agreement. `acting_preview`
and `auth_disabled` are stamped so reporting can filter both explicitly rather
than discovering the problem later. With auth off every row reads `annotator='local'`
and agreement is undefined by construction — `/api/label/progress` says so.

### 3.4 Seeded taxonomy

The seed inserts a group or class only when its **key has never existed**, and
never updates an existing row. Presence-of-key, not `active = TRUE`, so a class a
poweruser archived stays archived across every reboot and a description they
sharpened after watching annotators disagree is never reverted by a container
restart. Seeding writes a `taxonomy_audit` row with `actor='system'` and bumps
`taxonomy_revision`. The escape hatch for a deliberate upstream refresh is
`cownting labels reseed --force`, which writes an audit row and bumps the revision
so before/after agreement stays comparable.

Every group carries a **Cannot tell**, so an annotator is never forced to guess —
a forced guess is noise that looks like disagreement.

**Group `sun_exposure` — "Sun exposure"** (single-select, required, sort 10)

> Where the RINGED animal's body is relative to shade at this instant. Judge the
> body, not the head or the legs, and judge only the ringed animal — other cows in
> the crop are context. This is the question the whole agrivoltaics study turns on:
> do the panels actually get used as shade?

- `sun_exposure.shaded` — **Shaded** — Roughly two thirds or more of the visible
  body sits inside a shadow — a panel row, a building, a tree, or the herd itself.
  A cow standing in panel shade with only its head out in the sun is still Shaded:
  the body decides. Panel shadows have a hard, straight edge; tree shadows are soft
  and ragged; both count. A cow fully under a panel, visible only as an outline
  against dark ground, is Shaded. Do NOT use this when the whole scene is overcast
  and nothing casts a shadow — "everything is dull" is not the same as "this animal
  chose shade"; that is Cannot tell.
- `sun_exposure.direct_sun` — **Direct sun** — Most of the visible body is in
  unobstructed sunlight: the ground around it is bright and the animal casts its own
  visible shadow. A cow half under a panel with its hindquarters out counts as Direct
  sun only if the sunlit share is clearly the larger one — at a near 50/50 split
  choose Cannot tell instead of guessing, because those are exactly the frames where
  annotators disagree and we would rather measure that honestly. Thin dappled light
  through a gap between panels is still Direct sun if the animal's own shadow is sharp.
- `sun_exposure.not_visible` — **Not visible** — The animal is in the frame — that
  is why it was detected — but you cannot see enough BODY to place it: it is cut off
  by the frame edge, hidden behind a panel leg or another animal, or the crop is so
  dark or so blown out that ground brightness carries no information. This is for a
  physical or optical obstruction. If you can see the body fine but still cannot
  decide sun vs shade, that is Cannot tell, not this.
- `sun_exposure.cannot_tell` — **Cannot tell** *(escape)* — You can see the animal,
  but the sun/shade call is not decidable: overcast light with no shadows anywhere, a
  near-even split between lit and shaded body, dusk or dawn where the whole scene is
  uniformly dim, or a dark patch you cannot attribute (panel shadow, or wet ground?).
  This is a CORRECT answer, not a failure — never guess to avoid it. A high Cannot
  tell rate on one camera or one hour is itself a finding, and it is the only way to
  tell a genuinely ambiguous scene apart from an unsure annotator.

**Group `behaviour` — "Behaviour"** (single-select, required, sort 20)

> What the RINGED animal is doing in this single frame. You are looking at a
> timelapse still, not video, so judge geometry — head height, leg line, what the
> muzzle is touching — never inferred motion. Pick the one class that fits the
> completed posture; do not label an intention.

- `behaviour.feeding` — **Feeding** — The muzzle is at or in the sward and the
  posture reads as intake: head clearly below shoulder height, neck extended forward
  and low, nose at ground level, body normally standing. You cannot see chewing in one
  frame, so judge geometry. The trap: head down is NOT automatically feeding. A cow
  nosing a panel leg, licking a post or drinking is Head probing. A cow lying with its
  head at grass level while ruminating is Lying. Grass immediately beside a panel leg
  is still Feeding — what matters is what the muzzle is ON, not what is nearby.
- `behaviour.lying` — **Lying** — The body is on the ground: the belly line meets
  the ground and the legs are folded under or stretched to the side. From these camera
  angles the reliable cue is the gap under the barrel — a standing cow shows daylight
  beneath it, a lying cow reads as a compact blob with no leg gap. Head position is
  irrelevant: head up and head flat are both Lying. A cow mid-transition (front knees
  down, hindquarters still up) is Standing — commit only to a completed posture.
- `behaviour.standing` — **Standing** — On all four feet, head at or above shoulder
  height, not eating: idling, ruminating upright, watching, or walking. This is the
  default for an upright animal that is not clearly doing one of the other things.
  Walking belongs here on purpose — a timelapse frame gives no reliable motion cue, so
  we do not ask you to invent one. Getting up and lying down also belong here.
- `behaviour.head_probing` — **Head probing** — Head down or extended, but
  INVESTIGATING rather than taking in feed: nosing panel legs, mounting rails, cabling
  or fence line; licking or rubbing on infrastructure; sniffing bare ground; drinking.
  This class exists because head-down-near-hardware is the behaviour that damages a
  solar field, and it is visually easy to confuse with Feeding. The single
  discriminator is what the muzzle is at: grass or feed means Feeding; hardware, bare
  ground or water means Head probing. If you cannot resolve which, use Cannot tell.
- `behaviour.cannot_tell` — **Cannot tell** *(escape)* — The pose is not decidable
  from this crop: occluded by another animal or a panel, cut off at the frame edge,
  motion-blurred, too small or too dark to resolve a leg line, or facing the camera
  head-on so the head/body geometry is foreshortened away. Also use it when the animal
  is perfectly visible but Feeding vs Head probing is a coin flip. Choose this rather
  than guessing — the Cannot tell rate per camera is one of the numbers we report.

### 3.5 Agreement SQL

Lives in `labels_db` as named constants over `v_current_answers`, so superseded
versions and skips are already excluded. `agreement(con, group_key)` and
`fleiss_kappa(con, group_key)` **read `label_groups.multi_select` and dispatch**:
single-select goes to the exact-match pairwise/Cohen/Fleiss queries, multi-select
goes to `SQL_MULTISELECT_JACCARD`. Routing a multi-select group through the
exact-match SQL inflates `n_common` (the self-join emits one row per selected
class, not per annotator) and double-counts a rater in Fleiss' `n_ij`, so it
returns a plausible wrong number rather than an error — the helpers therefore
raise `ValueError` rather than fall through if a caller forces the wrong query.

All of them apply the **same** exclusion predicate, `NOT acting_preview AND NOT
auth_disabled`, so the headline numbers are computed over one population.

Constants: `SQL_PAIRWISE_AGREEMENT`, `SQL_COHENS_KAPPA`, `SQL_FLEISS_KAPPA`
(with the per-item normalisation that tolerates a variable rater count — items are
labeled opportunistically here, so the classic equal-n form does not apply),
`SQL_MULTISELECT_JACCARD`, `SQL_EFFORT_BY_ANNOTATOR`, `SQL_INFO_ICON_PRESSURE`,
`SQL_ABANDONED`. A sensitivity variant excluding `is_escape` classes exists for
"Cannot tell as missing data"; see §9.

### 3.6 Config

```python
class AnnotationCfg(BaseModel):
    """The in-app Label page. DISTINCT from LabelCfg (the CVAT round-trip)."""
    targets_per_instance: int = 2   # NOT 1: see §4.2
    overlap_fraction: float = 0.20
    overlap_targets: int = 3
    skip_retire: int = 3
    batch_size: int = 8
    max_batch_size: int = 50
    crop_pad: float = 0.35          # fraction of the LONGER bbox side
    crop_max_width: int = 768
    mask_timestamp_banner: bool = True
    max_banner_fraction: float = 0.6   # above this the crop is refused, not blanked
    max_note_chars: int = 500

class BackupCfg(BaseModel):
    enabled: bool = False           # opt in deliberately: see §6.5
    every_days: int = 7
    keep: int = 8
    max_upload_bytes: int = 9_500_000
```

`PathsCfg` gains exactly two fields, declared **here and only here**:

```python
labels_db_path: str = "data/labels.duckdb"   # annotations; a separate file so
                                             # purge/archive cannot destroy them
backups_dir:    str = "data/backups"         # weekly zips in <backups_dir>/labels/
```

`Config` gains `annotation: AnnotationCfg` and `backup: BackupCfg`. Both blocks
and both paths go into `config/cownting.example.yaml` **and**
`config/cownting.prod.yaml` — the example is the template people copy.

---

## 4. Backend

### 4.1 The frozen route table

Nothing outside this table exists. Routes live in `create_app` under a
`# ---- label` banner placed **before** the static-frontend block (`api.py:1084`),
because the SPA catch-all shadows anything after it.

| Method | Path | Gate | Body / params |
|---|---|---|---|
| GET | `/api/label/taxonomy` | any session | — |
| GET | `/api/label/queue` | any session | `limit`, `exclude`, `camera`, `day`, `mine`, `order` |
| GET | `/api/label/progress` | any session | `dataset`, `camera` |
| GET | `/api/label/mine` | any session | `limit`, `before` |
| GET | `/api/img/label-crop/{camera}/{frame_file}` | any session | `dataset`, `x1..y2`, `pad`, `w` |
| POST | `/api/label/submit` | `require_labeler` | `LabelSubmitReq` |
| POST | `/api/label/skip` | `require_labeler` | `LabelSkipReq` |
| POST | `/api/label/undo` | `require_labeler` | `{instance_key}` |
| POST | `/api/label/events` | `require_labeler` | `{session_id, kind, instance_key?, class_key?}` |
| POST | `/api/label/groups` | `require_poweruser` | `LabelGroupReq` |
| PATCH | `/api/label/groups/{group_key}` | `require_poweruser` | `LabelGroupPatchReq` |
| POST | `/api/label/groups/{group_key}/move` | `require_poweruser` | `{dir: "up"\|"down"}` |
| POST | `/api/label/groups/{group_key}/classes` | `require_poweruser` | `LabelClassReq` |
| PATCH | `/api/label/classes/{class_key}` | `require_poweruser` | `LabelClassPatchReq` |
| POST | `/api/label/classes/{class_key}/move` | `require_poweruser` | `{dir}` |
| GET | `/api/labels/backup/status` | `require_poweruser` | — |
| POST | `/api/labels/backup/run` | `require_poweruser` | `{force?: bool}` |

**There are no DELETE routes.** Archiving is `PATCH … {"active": false}` — one
field, one polarity, server and client agree. Restoring is `{"active": true}`.
A hard delete would orphan every stored answer and silently change what the
historical data means; the API never makes that easy.

Pydantic bodies (module scope, beside `AreasReq`/`ClipReq`):

```python
class InstanceAnchor(BaseModel):
    dataset_id: str | None = None
    camera_id: str
    frame_file: str          # basename, "00000450.jpg"
    bbox: list[float]        # [x1, y1, x2, y2] full-frame px
    ordinal: int = 0
    ts: str | None = None
    frame_sig: str | None = None

class LabelSubmitReq(BaseModel):
    instance_key: str
    anchor: InstanceAnchor
    answers: dict[str, str | list[str]]   # group_key -> class_key(s)
    taxonomy_revision: int
    serve_event_id: int | None = None
    session_id: str | None = None
    client_elapsed_ms: int | None = None
    input_mode: str | None = None         # 'key' | 'mouse'
    note: str | None = None

class LabelSkipReq(BaseModel):
    instance_key: str
    anchor: InstanceAnchor
    reason: str                            # SKIP_REASONS
    serve_event_id: int | None = None
    session_id: str | None = None
    client_elapsed_ms: int | None = None
    note: str | None = None
```

Every client-reported string is length-capped at the boundary (`note` at
`max_note_chars`, `client_info` at 200, `viewport` at 32). `require_labeler`
admits every known role, so a viewer session can write rows; unbounded strings
would let a scripted client bloat the store that later gets zipped under a 10 MB
Discord cap.

### 4.2 Sampling policy

The queue is a per-request scan of `detections`, keyed by `instance_key_sql`,
anti-joined against the ATTACHed label store, returned as a **batch**
(`batch_size` default 8).

```sql
WHERE annotations_labeled < target
  AND skips < skip_retire
  AND (mine='todo' -> I have no current row on this effective_key)
ORDER BY annotations_labeled ASC,      -- coverage first
         day DESC NULLS LAST,          -- order=fresh (order=spread drops this)
         md5(? || instance_key)        -- stable per-annotator permutation
LIMIT ?
```

`target = overlap_targets` when `substr(instance_key,1,4) < hex_threshold(overlap_fraction)`,
else `targets_per_instance`. Membership in the overlap subset is derived from the
key itself, so every annotator independently agrees on the same subset with zero
coordination.

`targets_per_instance` defaults to **2, not 1**. With a target of 1, the moment
any annotator submits, the instance leaves every other annotator's queue forever —
which directly contradicts the requirement that multiple users label the same
instances, and would leave only the 20% overlap slice with any redundancy at all.
Two is the floor at which agreement is measurable everywhere; the 20% slice at 3
lets Fleiss' kappa run with a meaningful rater count. This is a study-design knob
and it is in the YAML.

`annotations_labeled` counts `outcome='labeled'` only. Skips feed the separate
`skip_retire` counter, so an instance one annotator found unjudgeable is still
served to the next, and only retires after `skip_retire` distinct annotators
decline it.

**No leasing, no reservation, no claiming.** Two annotators may be served the same
instance simultaneously; the cost is one extra independent annotation, which is
the data we want. A lease table would make `GET /api/label/queue` a writer on the
main DB, putting DuckDB's single-writer lock on the hottest read path, and it
would be unrecoverable-by-design across the restarts this deployment does on every
`docker compose up -d --build`. It also means the GET is idempotent, so React 19
StrictMode's double-invoked effect is harmless by construction.

**Pagination** is `limit` bounded by `max_batch_size` plus an `exclude` list of
keys already in the client's buffer (capped at 200), and **no** offset or cursor.
The ordering key includes `annotations_labeled`, which other annotators change
while you work, so any positional cursor would skip or repeat items. The queue is
self-consuming: anything you label or skip is removed by the anti-join, so
re-fetching always advances. `count(*) OVER ()` after the WHERE gives the exact
`matching` count for free in the same scan.

`dataset` defaults to the whole DB and deliberately does **not** go through
`resolve_ds` — labeling is cross-day by design, and `export_csv` is the existing
precedent (`api.py:1076`). Every response echoes the applied scope in a `filters`
block, which is the defence against `lib/api.ts`'s `withDs()` stamping the
selected day onto a cross-day queue.

**ATTACH direction is fixed: labels onto main, never the reverse.**
`localize_worker` holds a writer on the main DB for a whole localize pass and is
fired by clip, restore, delete-camera and every areas save. Attaching main into a
labels connection would put every label read *and write* behind that; attaching
labels into main confines the exposure to reads. `labeling.attached_labels(c, config)`
is a context manager that schema-inits the store through its own short-lived
connection first, retries the bare `ATTACH` with `db.connect`'s bounded backoff
(the file-handle clash is routine here, not exotic — the store is held read-write
by every concurrent submit), and DETACHes in `finally` only if the ATTACH landed.
A leaked alias makes the *next* ATTACH on that connection fail, which surfaces as
a random 500 on an unrelated request.

**Served events.** For each item it returns, the queue writes a `label_events`
row of `kind='served'` **into labels.duckdb** and puts its `event_id` on the item
as `serve_event_id`. This is a write, but it is a write to the *labels* file, so
the main-DB read path the no-leasing decision protects is untouched. It buys three
things nothing else can: a non-forgeable server-side `time_on_task_ms`
(`submitted_at - served_at`), measurable abandonment (served events with no
matching annotation), and a server/client delta that detects tab-away — 40 minutes
of server elapsed against 6 seconds of client-active time is not 40 minutes of
attention. `POST /api/label/events` carries `info_opened` and session boundaries,
which is the cheapest available signal that a class definition is ambiguous.

### 4.3 The write path

`POST /api/label/submit` **never opens the main DB.** The client echoes the anchor
the queue served; the server re-hashes it in Python via
`labeling.verify_anchor(instance_key, anchor)` and 400s a mismatch. Three wins:
zero contention with `localize_worker` on the hottest mutation; a stored row whose
key and anchor can never disagree, because the key *is* the hash of the anchor;
and no dependency on a detection row that may already have been purged or
archived. A label is the annotator's claim about an image — whether a matching
detection exists right now is a separate, lazily-answered question.

**Validation is against the taxonomy the annotator was served, not the current
one.** Two rules:

- `class_key` validity is checked against **ever-existing** keys, active or not.
  A poweruser archiving a class must not 400 an answer already on screen.
- If `body.taxonomy_revision != current revision`, the server returns **409** with
  `{"code": "taxonomy_stale", "revision": N}`. The client re-fetches the taxonomy,
  keeps whichever selections still resolve, and re-presents the item.

Validating required-group completeness against *current* state while accepting a
stale revision is the trap: a poweruser adding a required group would 400 every
in-flight submit permanently, and the frontend's offline-retry would hammer a 4xx
forever with no path forward but a reload. Returning 409 on any revision skew
makes the situation explicit and recoverable, and costs one refetch on the rare
occasion a taxonomy edit lands mid-session.

`taxonomy_revision` is stamped on every annotation, so a later report can ask
"did agreement improve after we rewrote the Feeding description?".

### 4.4 Auth gating

A new third gate beside `require_poweruser` / `require_admin`:

```python
def require_labeler(request: Request):
    """Labeling is the one mutation a plain `user` may perform — that is the
    entire point of the Label page — so this admits every KNOWN role. It is a
    real gate, not a bypass: it re-derives effective_role(), so an admin
    previewing a role is treated as that role, and it 403s an unknown or absent
    one. A no-op when auth is disabled, like the other two."""
    if not auth_on:
        return
    if effective_role(request) not in auth_mod.ROLES:
        raise HTTPException(403, "login required to label")
```

`tests/test_auth.py::test_every_mutating_route_is_gated` scans the live route
table; its accepted set widens to `{require_poweruser, require_admin,
require_labeler}` with an explanatory comment. Adding the label routes to
`SELF_GATING` instead would remove them from the scan permanently and silently
exempt every future label mutation — `SELF_GATING` means "gates internally",
which login/logout/act-as genuinely do and a label route does not.

`current_user(request)` is the new helper the codebase lacks. It returns the
**real** username plus `role`, `acting_role`, and touches `request.session` only
behind `auth_on` — `SessionMiddleware` is mounted only inside `if auth_on:`
(`api.py:162`), and `tests/test_api.py` runs with `AuthCfg(enabled=False)`.

### 4.5 The crop endpoint

`GET /api/img/label-crop/{camera}/{frame_file}?dataset=&x1=&y1=&x2=&y2=&pad=&w=`

The URL is **built server-side** by `labeling.crop_url(...)` and handed to the
client on each queue item as `crop_url`. The frontend never constructs it: it
would otherwise route through `withDs()`, which stamps the currently-selected day
onto any `/api/` URL and would 404 every item from another day.

**Fixed-square canvas.** `labeling.crop_geometry(bbox, pad, max_width)` is a pure
function returning `(src_box, out_size, ring)`:

```
bw, bh = max(x2-x1, 1), max(y2-y1, 1)
side   = max(bw, bh) * (1 + 2*pad)
src    = (cx - side/2, cy - side/2, cx + side/2, cy + side/2)   # may exit the frame
scale  = min(1.0, max_width / side)
out    = round(side * scale)                                    # square, always
ring   = ((x1 - src.x0) * scale, (y1 - src.y0) * scale, ...)    # crop-local px
```

Regions of `src` outside the frame are neutral-filled rather than clamped away.
This is what makes the geometry computable **without knowing the frame's
dimensions** — so the queue can emit `crop_w`, `crop_h` and `ring` from the bbox
alone, with no PIL header-open per item and no filesystem touch on a pure-DB
endpoint. It also fixes the tile-shape jitter that per-axis clamping would cause
at frame borders, which is visually fatiguing over hundreds of items.

**The ring is NOT baked in.** The endpoint returns a clean crop; the client draws
the ring and the even-odd spotlight scrim as SVG over it, in the crop-local pixels
the queue supplied. That is what makes `H` (hold to hide the ring, to judge
occlusion on unobstructed pixels) possible at zero network cost, keeps the stroke
a hairline at any rendered size, and means a style change does not invalidate
every cached crop. `?ring=1` is not a parameter; there is one rendering.

**Banner masking stays server-side**, and is a methodological requirement, not
polish. Brinno burns wall-clock time into the bottom ~4% of every frame
(`quality.py:39`'s `_BANNER_CROP = 0.96`; `ingest/capture_time.py` OCRs it), and
time of day correlates directly with the "Sun exposure" answer — a visible banner
hands the annotator the answer and inflates agreement artificially. The mask is
applied in full-frame coordinates to the rows at or below `H * 0.96` that fall
inside `src`:

```python
start = band_top - src_y0            # may be negative
if start < region.shape[0]:
    region[max(0, start):, :] = FILL
filled = (region.shape[0] - max(0, start)) / region.shape[0]
if filled > cfg.max_banner_fraction:
    return b""                       # -> 404, never a blank tile
```

The `max(0, …)` guard alone is not enough. These cameras look down and the herd
crosses the bottom of the field of view, so a padded crop of a low cow can begin
*below* the band line — `start` negative, `max(0, start) == 0`, and the naive
version paints the entire tile grey. The annotator is then shown a featureless
rectangle and their honest "Not visible" / "Cannot tell" is recorded as genuine
ambiguity about a real animal: fabricated ambiguity contaminating exactly the
statistic this feature exists to produce. So the fraction is measured and a
mostly-banner crop is refused outright, which the client already has a terminal
state for.

**Path safety.** The on-disk path is rebuilt from `config.paths.artifacts_dir`
plus already-validated ids and never joined from caller strings, mirroring
`ingest/video.py:60-62,84` including the legacy flat layout when `dataset_id` is
absent. Three whitelists — `_safe_path_id(dataset)`, `uploads.valid_camera_id(camera)`,
and a new `_safe_frame_file` matching `^[0-9]{1,12}\.jpg$` — plus a
resolve-under-`artifacts_dir` assertion copied from the SPA catch-all. This is a
deliberate departure from the house rule "the path always comes from the DB": a DB
lookup here is a full `frames` scan for every image an annotator sees, and the
frame filename is one of the few places in this codebase where a *total* whitelist
is available. `_safe_frame_file` must never be loosened to accept a general
filename; treat any change to it as a security review, not a feature.

It is **not** an extension of `/api/img/frame`'s `kind=` parameter: that endpoint
silently falls back to the raw full frame for an unrecognised kind
(`api.py:862-863`) with HTTP 200, so a typo would show an uncropped, unringed
image and the annotator would confidently label the wrong animal.

**Caching.** Every existing image response is a `FileResponse`, which supplies its
own validators; a computed `Response` gets none, so an annotator paging back and
forth would re-decode a full-resolution JPEG on every keystroke, and the client's
`new Image()` prefetch would be a wasted request per item. So: a strong ETag over
`(resolved path, mtime_ns, size, bbox, pad, out_size, RENDER_VERSION)`, a 304 fast
path on `If-None-Match`, and `Cache-Control: private, max-age=3600`. `private` is
load-bearing — the image is session-gated and must never be stored by Caddy or any
shared proxy. Not `immutable`: a re-ingest rewrites the JPEG at the same path, and
`mtime_ns` in the ETag material is what invalidates it.

The endpoint also computes `frame_sig` while the file is open and returns it in an
`X-Frame-Sig` header; the queue independently puts it on the item, so the submit
body carries it without a second read.

**The queue does not group consecutive items by frame,** even though several cows
usually share one frame and grouping would cut decodes ~10×. Showing an annotator
cow 2 immediately after cow 1 from the same frame correlates their judgements —
once you have decided a frame is sunny, the next cow in it is answered by
anchoring, not by looking. For a feature whose purpose is measuring variability,
that contaminates the primary measurement. It is also why there is no
decoded-frame LRU: the per-annotator shuffle deliberately destroys frame locality.

---

## 5. Frontend

### 5.1 Routing and nav

`App.tsx`: `<NavLink to="/label">Label</NavLink>` immediately after Data and
before Manual, so DOM order matches the requested visual order.

```tsx
<Route path="/label" element={<Label />} />
<Route path="/label/classes" element={<PowerUserOnly><LabelClasses /></PowerUserOnly>} />
```

`/label` gets **no** guard wrapper: `AuthProvider` only mounts the routed tree
once a session exists, so a bare `<Route>` already means "any signed-in role" —
the house idiom. Poweruser affordances *on* the Label page (the "Manage classes"
link) are hidden with `canManageData(user)`, never disabled.

The taxonomy editor gets its own route rather than an in-page toggle because the
labeling page owns every keystroke; a text input mounted in the same tree steals
`1`, `Q`, `S`, `Enter` and turns the global hotkey layer into a mode machine on
the hottest path in the app, for a task performed once a quarter. It also matches
`/data → /data/:dataset/cameras`.

The nav is a fixed `flex items-center gap-5` row with no collapse; adding a sixth
link pushes it past comfortable width. `BugReportButton`'s text goes
`hidden sm:inline` as relief. A properly responsive header is out of scope and
now overdue.

### 5.2 Files

| File | Role |
|---|---|
| `frontend/src/pages/Label.tsx` | the labeling screen: buffer, hotkeys, submit/skip/undo, terminal states |
| `frontend/src/pages/LabelClasses.tsx` | poweruser taxonomy editor |
| `frontend/src/components/InstanceCrop.tsx` | `<img>` + SVG ring and even-odd spotlight scrim |
| `frontend/src/components/LabelGroup.tsx` | `LabelGroupList` + `Option` rows with inline info disclosure |
| `frontend/src/components/LabelProgress.tsx` | effort stats + the permanent key legend + preferences |
| `frontend/src/lib/labelKeys.ts` | the derived key ladder — single source of truth |
| `frontend/src/components/ui.tsx` | `+ Kbd`, `+ InfoIcon`, `+ INPUT_CLS` |
| `frontend/src/lib/types.ts` | `LabelClass`, `LabelGroup`, `Taxonomy`, `LabelItem`, `LabelQueue`, `LabelStats`, request/response types |
| `frontend/src/lib/api.ts` | `jRaw()` extraction + the `label` section |
| `frontend/src/index.css` | `+ --color-danger: #b4523f` |
| `frontend/src/pages/Manual.tsx` | new section 5, "Label cows" |

`lib/api.ts` gets one refactor: the shared fetch body moves into a module-private
`jRaw()` and `j()` becomes `jRaw(withDs(url), init)`. Every existing call site is
byte-identical. All label calls use `jRaw` — `withDs()` would silently filter the
cross-day queue to the selected day. This refactor **ships as its own commit ahead
of the feature** (§8, S0): it sits on the code path behind every call on
Dashboard, Data, CountArea, Admin and CameraManage, there is no frontend test
runner to catch a regression in it, and it must be revertable independently of a
stalled label branch.

### 5.3 The queue item contract

The frozen JSON shape the queue returns and `LabelItem` mirrors:

```ts
interface LabelItem {
  instance_key: string;
  dataset_id: string | null;
  day: string | null;          // ISO date; NOT the clock time
  camera_id: string;
  frame_file: string;
  bbox: [number, number, number, number];   // full-frame px, echoed on submit
  ordinal: number;
  score: number | null;
  frame_sig: string | null;
  crop_url: string;            // server-built; drop straight into <img src>
  crop_w: number; crop_h: number;
  ring: [number, number, number, number];   // crop-local px
  n_annotators: number;        // how many already labeled it; never WHAT they said
  target: number;
  overlap: boolean;
  serve_event_id: number;
}
```

The caption shows **day + camera, never the clock time** — same reason the banner
is masked. Traceability is served by `instance_key`, which the flag payload
carries and which needs no human-readable clock.

There is no `frame_w`/`frame_h` and no full-frame view in v1. The `C` context
toggle needs the frame's native dimensions to place a ring in full-frame space;
that arrives in v1.1 with `frames.frame_w` / `frames.frame_h` (`ALTER TABLE …
ADD COLUMN IF NOT EXISTS`, the `db.py:116-125` idiom, written at ingest and
backfilled lazily). The `H` hold-to-hide-ring affordance covers most of what `C`
was for and works today.

### 5.4 The question stack and info disclosure

Options are visually-hidden **native** `<input type="radio">` inside styled
`<label>` rows — grouping, `aria-checked`, arrow-key navigation and screen-reader
announcement for free, versus ~15 lines of roving-tabindex code to get wrong.
`Chip` is not reused: it renders a pill, and it renders a bare `<button>` with no
`type`, no `disabled` and no aria passthrough (`ui.tsx:145-167`).

**Info descriptions expand inline beneath the option row**, not in a tooltip or
popover. A popover covers the crop — the exact pixels the definition must be
compared against. Tooltips are hover-only, so they are dead on touch and invisible
to a keyboard, and they vanish mid-read. Inline expansion never occludes the
image, is natively expressible with `aria-expanded`/`aria-controls`, and its
open/closed state persists in `localStorage` (`cownting.label.defs`) — definitions
are training wheels a new annotator leaves open for their first fifty items and
collapses once fluent. `I` toggles them all. Opening one POSTs an `info_opened`
event, which is what makes `SQL_INFO_ICON_PRESSURE` return anything.

A class description is **required** server-side, so `AddClass` disables its button
until both name and description are non-empty. An option with no written
definition is the single largest source of annotator disagreement, which is the
whole reason the (i) icon exists.

### 5.5 Keyboard map

`lib/labelKeys.ts` derives the map from the live taxonomy — powerusers can add
groups and classes, so a literal map breaks the moment the taxonomy grows.
Reserved action keys are filtered out of a fixed row ladder
(`1234567890` / `qwertyuiop` / `asdfghjkl` / `zxcvbnm`); group *i* takes row *i*,
class *j* takes character *j*. With the shipped taxonomy:

| | Keys |
|---|---|
| Sun exposure | `1` `2` `3` `4` |
| Behaviour | `Q` `W` `E` `R` `T` |
| save | `Enter` |
| skip | `S` |
| flag | `F` |
| undo last | `U` |
| toggle definitions | `I` |
| hold to hide ring | `H` |
| clear answers | `Backspace` |
| all keys | `?` |
| close overlay | `Esc` |

Two adjacent physical rows under one hand, spatially mirroring the two stacked
groups on screen. The same module feeds the `<Kbd>` badge on each option row, the
permanent legend under the progress panel, the `?` sheet, the Manual, and the
editor's live reorder preview, so they cannot drift.

`isTypingTarget()` excludes `radio`/`checkbox`/`button` inputs from the
don't-fire-hotkeys guard. The options **are** `<input>` elements, so a naive
`tagName === "INPUT"` test kills every hotkey the instant an annotator clicks one
option with the mouse — the most likely first interaction, and a bug that would
present as "the shortcuts randomly stop working".

Enter on an incomplete item raises a `role="alert"` nudge naming the unanswered
group and its keys; it is never a silent no-op, because a silent no-op means the
annotator presses it twice, assumes it worked and moves on. Auto-submit-on-complete
is a persisted preference, **default off**, defensible only because `U` restores
the item with its selections intact.

`U` supersedes the annotator's own last submission (`POST /api/label/undo`),
pushes the item back to the head of the buffer, **and re-applies the previous
selections** — so `U` means "fix the one option I fumbled", not "answer both
questions again". A mis-key is the dominant error mode in a keyboard-first flow.

### 5.6 Terminal states

Five distinct states, because "nothing to label" has different causes and
different next actions: caught-up; no footage processed at all (`stats.pool_total === 0`,
with a `<Link to="/data">`); image missing or refused on disk (offers a one-key
flag); submit failed while offline (item stays on screen, retried on the `online`
event); taxonomy empty (points powerusers at `/label/classes`).

A 409 `taxonomy_stale` is handled separately from a network failure: refetch the
taxonomy, keep the still-valid selections, re-present, tell the annotator the
questions changed. A persistent 4xx is never retried in a loop.

The crop card does **not** re-run `animate-fade-slide-in` per item — a 600 ms
fade firing every ~5 s for an hour is nauseating and delays the pixels the
annotator is waiting on. The page-level mount animation stays.

### 5.7 Taxonomy editor

`LabelClasses.tsx` — poweruser only, `PowerUserOnly` on the route. Every mutation
returns the whole taxonomy (the `Admin.tsx` idiom), so there is no optimistic
state to reconcile. Archive/restore is `PATCH {active}` — the same field name and
polarity the server declares. Reordering is up/down buttons calling `…/move`, not
drag-and-drop: keyboard-reachable, screen-reader announceable, and it keeps the
app dependency-free. Each group shows a live key preview, since reordering changes
annotators' muscle memory — and a group past the ladder is labelled mouse-only.

The page states plainly that nothing is ever deleted and that archiving preserves
historical answers.

---

## 6. Backup

`cownting/labels_backup.py`. `data/labels.duckdb` is the one piece of state on
this box that cannot be regenerated — frames, detections and overlays all come
back from a re-ingest; annotator hours do not — and because it deliberately sits
outside the main DB, it is also outside the `tar czf data/` ritual operators
actually remember.

### 6.1 Scheduler

An in-process daemon thread started from `create_app`'s boot block beside
`uploads_mod.recover_jobs(config)`, mirroring `localize_worker.py`'s shape
(module `_LOCK` + `_state` + `status()` + spawn-failure rollback). Chosen over
host cron and a compose sidecar because it has zero cross-process DuckDB lock
contention, its files land owned by uid 10001 by construction so `entrypoint.sh`
never has to heal them, and it ships in the image so a `git pull &&
docker compose up -d --build` deploy carries it with no host-side setup.

It ticks every 15 minutes and asks the DB *"has a week passed since the last
successful run, and did anything land since its watermark?"* — never
`sleep(7*86400)`, which would reset on every redeploy and therefore never fire on
a box that is redeployed weekly. `_FIRST_TICK_S = 120` is load-bearing: it is
longer than any test run, so the ~20 apps `tests/test_api.py` and
`tests/test_auth.py` build in temp dirs never fire a backup, and the sleeping
thread holds no file handle. Lowering it resurrects the Windows
`TemporaryDirectory`-cleanup crash documented at `tests/test_auth.py:209-217`.

### 6.2 Claiming, and the failure taxonomy

A run is claimed by a compare-and-set row in `backup_runs` inside an explicit
transaction. DuckDB grants one writer at a time across processes, so the loser's
transaction sees the winner's committed `running` row — a real cross-process
mutex covering `--workers N`, a manual CLI run racing the thread, and a second
container. A claim older than one hour is reclaimed and the abandoned row closed
out as failed, so a SIGKILL mid-run cannot wedge the job forever.

Failures are split into two classes, and the distinction matters more than it
looks:

- **Contention** — the store is held by another process, `db.connect` exhausts its
  ~9 s retry budget, or the claim is refused. Result: `status='skipped'`,
  `reason='store busy'`, **no row written**, no cooldown armed, exit code 0.
- **Genuine failure** — disk full, torn snapshot, Discord unreachable. Result: a
  `failed` row, a `[cownting.alert] LABEL-BACKUP` line, and a 6-hour cooldown.

Without that split, the documented `docker compose exec … labels backup --force`
becomes a foot-gun: an operator who wires it into a nightly host cron converts a
transient lock error into a *permanently disabled* weekly backup, because every
nightly failure resets the 6-hour cooldown and the in-app scheduler's 15-minute
tick never finds a green window. `POST /api/labels/backup/run` exists so an
operator without shell access can trigger a run inside the process that already
holds the file, which is the contention-free path.

A staleness alarm closes the loop: when
`max(finished_at) WHERE status='done'` exceeds `2 * every_days`, every tick prints
`[cownting.alert] LABEL-BACKUP stale`, which `alerts/watch.py` already tails.

### 6.3 Snapshot and bundle

`snapshot_db` does `CHECKPOINT` → `ATTACH` → `COPY FROM DATABASE` → `CHECKPOINT snap`,
on a read-write connection. Never `shutil.copy`: DuckDB keeps unflushed pages in a
`.duckdb.wal` sidecar, so copying the `.duckdb` alone while a label POST is in
flight yields a torn file that only fails on restore — the worst possible moment
to find out. `COPY FROM DATABASE` runs inside the engine against the current
committed MVCC snapshot: writers are never blocked and the result is
transactionally whole.

The zip is built from an **enumerated** file list in a staging dir created with
`tempfile.mkdtemp(dir=out_dir)` (same filesystem, so the final `os.replace` stays
atomic), written to `.part` first. Never a directory walk: `data/.session_secret`
(chmod 0600, the cookie signing key) lives two directories up, and a glob-based
backup that swept it into a zip posted to a Discord channel would hand out the
ability to forge any user's login cookie.

Members: `labels.duckdb`, `annotations.csv`, `taxonomy.json`, `MANIFEST.json`,
`README.md`.

**`annotations.csv` is written against the real schema** — one row per
`(annotation, choice)`, long format, which is what an inter-rater computation
wants:

```sql
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
```

`LEFT JOIN` on `annotation_choices` so skips (which have no choices) still appear
as rows. `_manifest()` builds its per-group histogram with `.get()`-style column
guards and skips any aggregate whose column is absent, so a future schema change
degrades the manifest rather than raising a `KeyError` that would escape
`build_bundle`, stamp the run failed, hold the watermark, and silently disable the
job forever behind a 6-hour cooldown.

### 6.4 Discord, oversize, retention

stdlib `urllib.request` with a hand-rolled ~15-line multipart encoder. `requests`
is absent and `pyproject` scopes `httpx` test-only; `alerts/watch.py` already
establishes urllib + explicit User-Agent + timeout as the house Discord shape. Not
`curl` via subprocess — the webhook URL would land in the process argv table,
visible to `docker top`. The poster is an injected callable so retry/oversize
logic is unit-testable with no network.

The webhook comes from `COWNTING_DISCORD_WEBHOOK` at the point of use, never from
Config or YAML (the `COWNTING_SECRET` rule). Unset or blank is a **supported
state**: the job still zips and rotates locally and **does** advance the
watermark. A URL that does not match `discord(app)?.com/api/webhooks/…` is refused
without logging its value. `_redact()` scrubs the URL and its token from every log
line, exception string and DB column — `urllib` puts the full URL into
`HTTPError.url`, and `backup_runs.error` ends up inside the very zip that gets
posted to a channel.

Oversize walks a ladder against a 9.5 MB budget (Discord's unboosted cap is 10 MiB
and it rejects rather than truncating): full zip → CSV-only zip (the `.duckdb` is
the bulky page-aligned part; the analytically useful CSV stays small far longer) →
a summary message naming the retained on-disk path. All three advance the
watermark, because re-running would produce the same oversize zip; the operator is
told in-channel and on a `[cownting.alert] LABEL-BACKUP oversize` line.

Retention keeps `keep` zips in `data/backups/labels/`, pruned **by name** (names
are UTC timestamps and sort chronologically; a zip restored onto the box carries
an unrelated mtime that would evict the wrong file). A prune failure never fails a
run — a root-owned stray from a `docker compose exec` without `-u cownting` raises
`PermissionError` here, and losing a rotation beats losing the backup that just
succeeded.

### 6.5 Rollout safety

`backup.enabled` defaults to **False**. The operator turns it on deliberately,
after reading the DEPLOY.md section. Two reasons: the first run has a NULL
watermark and therefore backs up and posts *everything*, and the prod host already
has a Discord webhook at `/opt/cownting/alerts/webhook.url` feeding the login-alerts
channel — an operator reaching for "the webhook I already have" would post the full
annotation store, annotator usernames and per-annotator timings included, into an
operational channel. DEPLOY.md says explicitly that the zip contains annotator
identities and that `COWNTING_DISCORD_WEBHOOK` must point at a dedicated channel.

### 6.6 CLI

```
cownting labels backup     [--force] [--keep N] [--no-discord]
cownting labels export-csv OUT.csv
cownting labels status     [--limit N]
cownting labels reconcile  [--dataset X] [--dry-run]
cownting labels reseed     --force
```

A `labels_app = typer.Typer(...)` sub-app added after the `user` sub-app. The
export command is `export-csv`, not `export`, so nothing here is one hyphen away
from the Stage-1b `label-export`. `labels status` prints *whether* a webhook is
configured, never the URL.

### 6.7 Docs

- `DEPLOY.md`: `data/labels.duckdb` and `data/backups/labels/` rows in the
  "What's in the box" table; the `data/` tar gains `--exclude='data/backups'` so
  weekly zips are not re-tarred into every full backup; a "Label backups" section
  covering how it works, how to configure it, the hand-run commands (with
  `-u cownting` and why), what a failure means, and restoring.
- **A mixed-vintage restore paragraph**, which is the one operational trap this
  design creates: if `labels.duckdb` is restored from a recent Discord zip while
  `cownting.duckdb` comes from an older `data/` tar, `frame_sig` disagrees
  wholesale and the next reconciliation reports a wall of `hijacked`. That is the
  *correct* alarm and the labels are fine — but on first sight it looks like
  catastrophic corruption, and an operator with no explanation would delete
  `data/labels.duckdb`, destroying the only unregenerable data on the box.
- `README.md`: a Label section that explicitly contrasts it with Stage 1b —
  "the in-app annotation tool is unrelated to `label-select` / `label-export`".
- `config/cownting.example.yaml` **and** `config/cownting.prod.yaml`: the
  `annotation:` and `backup:` blocks and both new `paths:` entries.
- `.env.example`: `COWNTING_DISCORD_WEBHOOK=` with the blank-is-a-no-op note.
- `.gitignore`: `data/backups/`.
- `Manual.tsx` section 5, "Label cows", rendering the *real* `<Kbd>` and
  `<InfoIcon>` components (add both to its import list) so a binding change
  changes the manual with it.

---

## 7. Testing

Repo conventions: no pytest, a module-level `check(name, cond, detail)`, a
`main()` that runs each test function and `sys.exit(1)` on any failure, runnable
both as `python -m tests.<mod>` and `python tests/<mod>.py`. `tests/__main__.py`
auto-discovers `test_*.py`, so no registration is needed. `cownting serve` runs
the whole suite as a pre-boot gate, so everything here must be fast and hermetic.

**`tests/test_labels_schema.py`** — the key contract and the store's semantics.

- `test_key_is_path_and_platform_stable` — the same logical frame keys identically
  from a Windows backslash path, a POSIX path, and a relocated `artifacts_dir`;
  the key is 32 lowercase hex chars.
- `test_key_quantisation_and_guards` — sub-pixel jitter keys the same, a
  whole-pixel move does not, `0.5` rounds **up** (not to even), an empty
  `frame_path` (`ingest.save_frames: false`) is refused with a message naming the
  cause.
- `test_python_and_sql_keys_agree` — **the load-bearing one.** Seeds detections
  including a NULL `dataset_id`, negative bbox coords and `.5` coordinates, then
  asserts `instance_key_sql` over the table equals `instance_key()` in Python for
  every row.
- `test_ordinal_is_dense_and_total` — two rows with the same quantised bbox but
  different scores get ordinals 0 and 1; two rows identical on every ordering
  column get the **same** ordinal and therefore the same key; the assignment is
  stable across repeated executions of the same query.
- `test_key_survives_clip_and_restore` — asserts `detection_id` **is** re-minted
  by `restore_clip` and the `instance_key` **is not**.
- `test_schema_is_idempotent_and_seeded` — three boots do not duplicate the seed;
  an archived class stays archived; both groups exist, single-select and required;
  each has an escape class; every class has a real description.
- `test_relabel_appends_and_double_submit_is_refused` — v2 supersedes v1, two
  annotators coexist on one instance, the UNIQUE blocks a silent double submit,
  `agreement()` runs and sees the disagreeing pair.
- `test_archived_class_still_resolves` — submit an answer, archive its class,
  assert the answer still appears in `v_current_answers` with its **snapshotted**
  `class_name`. This is the stated reason soft delete was chosen and nothing else
  checks it.
- `test_skip_does_not_count_as_coverage` — one skip on an instance with target 1
  leaves `n_annotators_labeled = 0`, so it is still servable to a second annotator.
- `test_undo_supersedes_and_keeps_history` — after undo the row is present with
  `outcome='undone'` and `superseded_at` set, and `v_current_answers` no longer
  returns it.
- `test_multiselect_dispatch` — `agreement()` on a multi-select group returns
  Jaccard, and forcing the exact-match query raises `ValueError`.

**`tests/test_labels_reconcile.py`** — the state machine, which is the part with
no other safety net.

- `test_exact_attach_and_unverified` — matching `frame_sig` → `attached`; NULL on
  either side → `attached_unverified`, counted separately, never silently attached.
- `test_hijacked_on_changed_sig` — same key, different `frame_sig` → `hijacked`,
  and `effective_key` is **not** moved.
- `test_iou_alias_and_no_requeue` — label an instance, shift every bbox by 3 px,
  re-ingest, reconcile; assert (a) an alias row exists, (b) `effective_key` is
  updated, (c) the queue does **not** re-serve the instance, and (d)
  `v_current_answers` still pairs the two annotators on one row. The whole
  `effective_key` design fails silently without (c) and (d).
- `test_ambiguous_merge_is_refused` — two source keys whose best candidate is the
  same merged box are left as orphans with `state='ambiguous_merge'`, and the
  `UNIQUE (new_key, dataset_id)` constraint holds.
- `test_null_dataset_migration` — labels made on the NULL-dataset partition
  survive `cownting migrate` via the `(camera_id, frame_basename)` fallback.
- `test_purge_reingest_orphan` — a genuinely unmatched label becomes an orphan
  with full provenance intact, still counted by `SQL_EFFORT_BY_ANNOTATOR`.
- `test_reconcile_is_idempotent` — running it twice changes nothing and creates no
  chained aliases.

**`tests/test_labels_api.py`** — the HTTP contract, on a hermetic synthetic DB
(`AuthCfg(enabled=False)`, the `tests/test_api.py` shape).

- `test_queue_shape` — items carry every field in §5.3; `crop_url` starts with
  `/api/img/label-crop/`; `matching`, `policy` and `filters` are present; the
  queue spans **both** seeded days (proving it does not go through `resolve_ds`).
- `test_queue_key_roundtrips_to_submit` — the key each item carries re-derives
  from its own anchor in Python, and a submit of that anchor returns 200. This is
  the end-to-end version of the SQL/Python pin.
- `test_submit_rejects_a_forged_anchor` — a bbox shifted by 17 px → 400.
- `test_stale_taxonomy_revision_is_409` — archive a class mid-session, submit with
  the old revision → 409 `taxonomy_stale`, **not** 400, and a submit naming the
  archived class with the current revision still succeeds (known keys, not active
  keys).
- `test_queue_policy` — coverage-first ordering; never re-served to the same
  annotator; a skip removes it from mine but leaves it for others; `limit` honoured.
- `test_multi_annotator_default` — with `targets_per_instance=2`, an instance
  labeled by annotator A is still served to annotator B. The requirement fails
  silently without this.
- `test_serve_event_and_time_on_task` — a queue fetch writes a `served` event;
  submitting with its `serve_event_id` populates `served_at` and a positive
  `time_on_task_ms`.
- `test_undo_is_scoped_to_me` — annotator B cannot supersede annotator A's row.
- `test_crop_endpoint_safety_and_caching` — traversal in camera/dataset → 400;
  a non-frame filename → 400; a missing JPEG → **404, never 500**; a real JPEG →
  200 `image/jpeg` with a `private` cache header and an ETag that round-trips to
  304; the returned crop is square and `crop_w == crop_h`.
- `test_banner_mask_does_not_blank_the_tile` — a bbox with `y1 > 0.96*H` either
  yields a crop with real pixels above the fill or a 404; it never returns an
  all-grey tile. Assert on the pixel variance of the decoded response.

**`tests/test_labels_backup.py`** — seeds through **`labels_db.init_labels_db`**
and `submit_annotation`, never hand-written `CREATE TABLE`s. A backup test that
invents its own schema validates the export against a store that will not exist.

- `test_gate_and_bundle` — an empty store is not due; the first run fires and
  posts once; zip members are exactly the five bundle files; nothing resembling
  `.session_secret` is swept in; a second run with nothing new is skipped.
- `test_csv_contains_the_answers` — every `outcome='labeled'` annotation has at
  least one non-null `class_key` row in the CSV, and skips appear with a
  `skip_reason`. This is the check that would have caught the export writing a
  weekly "backup" containing no labels.
- `test_snapshot_is_a_real_db` — snapshot succeeds with a live read-write
  connection attached; the extracted `labels.duckdb` opens and is complete; the
  watermark travels inside it.
- `test_oversize_ladder_and_retention` — falls back rather than failing; the mode
  is recorded; the full zip is retained; retention keeps exactly N, by name.
- `test_failure_holds_the_watermark_and_redacts` — a Discord outage → failed, the
  webhook and its token appear in neither the returned error nor the stored one,
  and the watermark does not advance.
- `test_contention_is_skipped_not_failed` — a claim refused by a held lease
  returns `skipped` and writes **no** `failed` row, so no cooldown is armed.
- `test_unset_webhook_is_a_clean_noop` — runs, `discord='skipped'`, watermark
  **does** advance; a non-Discord URL is refused.

**Edits to existing suites.**

- `tests/test_auth.py`: add the six taxonomy routes to `POWERUSER_SURFACE` using
  the **frozen** paths (`/api/label/groups/{k}/classes`, `/api/label/classes/{k}`);
  add a `LABELER_SURFACE` and `test_labeler_gate` asserting a plain `user` passes
  the gate on submit/skip/undo/events (a bad anchor then 400s, never 403), can read
  the taxonomy, cannot edit it, that an admin previewing `user` can label but still
  403s on taxonomy writes, and that anonymous gets 401; widen the accepted gate set
  in `test_every_mutating_route_is_gated` with the comment explaining why.
- `tests/test_api.py`: one contract-smoke line per new GET in
  `test_endpoints_answer_and_shape`. The hermetic DB has detections but no JPEGs,
  so the crop 404s — that is the shape.

**Not covered by an automated test:** `lib/labelKeys.ts` is pure and eminently
testable but there is no frontend runner. `npm run typecheck` is the only gate on
that side, and the frozen `LabelItem` interface in `types.ts` is what makes it
useful.

---

## 8. Rollout

Ordered, each step independently shippable and independently revertable. The whole
thing is deliberately not one branch: the earlier steps carry a `jRaw` refactor
that sits under every existing page, and a stalled label branch must not hold it
hostage.

**S0 — `jRaw` extraction (frontend only, no feature).** Split `j()` into
`jRaw()` + `j() = jRaw(withDs(url))`. Zero behaviour change, `npm run typecheck`
green, ship and deploy alone. Reverting the label feature later must not revert
this.

**S1 — the store, headless.** `cownting/labels_db.py` (DDL, key, seed, taxonomy
CRUD, `submit_annotation`, agreement SQL), `PathsCfg.labels_db_path`,
`AnnotationCfg`, the boot-block init in `create_app`, `tests/test_labels_schema.py`.
Touches production only by creating an empty `data/labels.duckdb` at boot. Nothing
is reachable from the UI. **`test_python_and_sql_keys_agree` must be green before
S2 begins** — everything downstream is built on that key.

**S2 — read-only backend.** `cownting/labeling.py` (queue SQL, `attached_labels`,
`crop_geometry`, `render_crop`, `crop_url`), `GET /api/label/taxonomy`,
`GET /api/label/queue`, `GET /api/img/label-crop/…`, `GET /api/label/progress`.
Still no writes, so nothing can be corrupted. Verify by hand against a real day on
the prod data: fetch a queue, open a `crop_url`, confirm the ring geometry matches
the returned `ring` and that a bottom-of-frame cow does not come back grey.

**S3 — writes, and the first usable version.** `require_labeler`, `current_user`,
`POST /api/label/submit` / `skip` / `undo` / `events`, served events,
`tests/test_labels_api.py`, `tests/test_auth.py` edits. Frontend: `Label.tsx`,
`InstanceCrop.tsx`, `LabelGroup.tsx`, `LabelProgress.tsx`, `labelKeys.ts`, the nav
entry, `Kbd`/`InfoIcon`/`INPUT_CLS`, `--color-danger`, the `types.ts` and `api.ts`
label sections.

**This is v1 and it is genuinely usable**: the seeded two-group taxonomy, info
icons, the keyboard flow, skips and flags, multi-annotator coverage at target 2,
and effort telemetry. It satisfies every stated requirement except runtime
taxonomy editing.

**S4 — poweruser taxonomy editing.** The six taxonomy routes, `taxonomy_audit`,
revision bumping, 409 `taxonomy_stale` handling on both sides, `LabelClasses.tsx`,
the `/label/classes` route, the "Manage classes" link, `Manual.tsx` section 5,
`README.md`.

**S5 — reconciliation.** `reconcile_dataset` implemented, `instance_key_aliases`,
`reconciliations`, `effective_key` maintenance, the advisory calls at the end of
`uploads._run` / `_run_add_camera`, the pre-purge annotation-count warning,
`cownting labels reconcile`, `tests/test_labels_reconcile.py`. Until S5 lands, a
re-ingest of a labelled day strands its labels — so **S5 must land before any
production day that has been labelled is ever re-uploaded**, and until then the
pre-purge warning (which is cheap and can ship in S3) is the safety net.

**S6 — backup.** `labels_backup.py`, `BackupCfg` (default off), `backups_dir`,
the boot-block `start_scheduler`, the CLI sub-app, the two backup routes,
`tests/test_labels_backup.py`, the DEPLOY.md section including the mixed-vintage
restore paragraph, `.env.example`, `.gitignore`.

**S7 — deferred.** `frames.frame_w`/`frame_h` + the `C` full-frame toggle;
agreement numbers surfaced in-app rather than only in the CSV; a
retired-instances ("nobody could judge these") view; auto-submit default review.

### Touching the running production server

The prod box runs `uvicorn` behind Caddy in a container, deployed by
`git pull && docker compose up -d --build`, with `./data` bind-mounted and
`config/` mounted `:ro`.

What actually changes on the server:

1. **A new file appears**, `data/labels.duckdb`, created by the boot block and
   owned by uid 10001 because it is created *inside* the container after the
   privilege drop. `entrypoint.sh`'s self-heal covers it if it is ever created
   otherwise. No migration, no downtime.
2. **`config/cownting.prod.yaml` gains `paths.labels_db_path`, `paths.backups_dir`,
   an `annotation:` block and a `backup:` block.** It is bind-mounted `:ro`, so it
   is editable on the host without a rebuild — but every new key has a default in
   `config.py`, so a deploy that forgets to edit the YAML still boots.
3. **`.env` gains `COWNTING_DISCORD_WEBHOOK`** (S6 only). Do **not** add it to
   compose's `environment:` block with `${VAR:?}` — that would make an unset
   webhook a hard boot failure, the opposite of the required clean no-op.
   `env_file: .env` already injects it and `setpriv` runs without `--clear-env`,
   so it survives the privilege drop.
4. **Nothing in the main DB changes** until S7's optional `frames.frame_w` column,
   which is an `ALTER … ADD COLUMN IF NOT EXISTS` in `init_db` — additive, and
   `init_db` already runs on every connection open.

Deploy order per step: run `python -m tests` locally (the same gate `cownting serve`
runs), push, `git pull && docker compose up -d --build`, then
`docker compose logs -f cownting` for one boot cycle. The riskiest single moment is
S1's first boot, because that is when `init_labels_db` runs inside `create_app` for
the first time — a DDL error there takes the **whole app** down, not just the new
page, so S1 ships alone and is verified on a scratch copy of `data/` before it goes
near prod.

Rollback is `git checkout <previous> && docker compose up -d --build` for every
step. `data/labels.duckdb` is never read by any pre-existing code path, so a
rollback leaves it inert on disk rather than breaking anything.

---

## 9. Open questions

1. **Is "Cannot tell" a category or missing data for the headline kappa?** The
   schema supports both (`is_escape` plus both SQL variants), but the *reported*
   number has to be one of them. Recommendation: report both and lead with
   "Cannot tell as a category" — an annotator declining to guess is real
   information about the frame, not a gap. This is a research decision, not an
   engineering one.
2. **What is the redundancy target?** `targets_per_instance = 2` with a 20% slice
   at 3 is the shipped default and it is defensible, but the real question is how
   much labelling effort you are willing to spend before kappa is meaningful, and
   whether breadth (more instances once) or depth (fewer instances, more raters)
   serves the study better. Changing it is a YAML edit.
3. **Should `order=spread` be the default instead of `order=fresh`?** `fresh`
   drains the newest day first, which makes a new upload observably appear at the
   head of the queue. If the study wants a stratified sample across the whole
   season, `spread` is right. Whoever owns the study design decides.
4. **Should admin act-as-preview labels count toward coverage targets?** They are
   real rows by a real annotator, so today they do, meaning a previewing admin
   clicking through a few cards consumes annotation budget. `acting_preview` makes
   excluding them a one-clause change — but then an admin genuinely labelling while
   previewing gets their work discounted. Inclination: count them, let reporting
   filter.
5. **Should retired instances be surfaced?** After `skip_retire` distinct
   annotators skip an instance it stops being served. A cluster of unjudgeable
   instances usually means bad crops or false-positive detections, which is a
   detector-quality signal with no home in this design.
6. **Does the Label page need a `label_enabled` flag on `/api/site`,** next to
   `posture_enabled` / `pose_enabled`? This design assumes always-on, since every
   role can reach it and the queue degrades honestly to an empty list. A flag is
   one line if the deployment wants to hide it during rollout.
7. **Which Discord channel?** The prod host already has a webhook feeding the
   login-alerts channel. A dedicated `#cownting-backups` webhook is free and keeps
   annotator identities out of the operational channel; confirm before S6 goes live.
