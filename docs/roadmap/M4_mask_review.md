# Workstream M4 — In-app Mask Review (validate & repair the segmentation)

Sibling of [M3_labeling.md](M3_labeling.md). M3 asks *what is this animal doing*;
M4 asks *is this outline an animal at all, and is it drawn correctly*. Same
store, same annotators, same no-lease queue discipline — a different unit of
work and a very different canvas.

The Label page gains a toggle: **Classes** (today's per-instance question stack)
and **Masks** (this workstream). Both are annotation surfaces for the same
people, so they share the store, the backup, the progress panel idiom and the
keyboard-first stance. They do not share a queue, a key, or a payload.

---

## 1. What we're building and why

An annotator is shown one **segmented frame** and does two passes over it:

**Pass A — triage** (fast, keyboard, whole frame)
- every existing mask gets a verdict: **ok** / **not a cow** / **imprecise**
- every cow the model *missed* gets a click in its centre (a point, not a shape)

**Pass B — repair** (slow, mouse, only what pass A flagged)
- each *missing* point becomes a polygon the annotator draws
- each *imprecise* mask becomes a polygon the annotator corrects by moving,
  adding and deleting vertices

Pass A is the measurement — false-positive rate, miss rate, and how often the
model is merely sloppy — and it is replicated across annotators so those numbers
carry a confidence interval. Pass B is the ground truth that feeds the
fine-tune, and it only needs one careful hand.

This also retires a dependency. Today the correction loop is
`finetune/export_cvat.py` → a running CVAT server → `finetune/dataset.py`
(see [SETUP_WINDOWS.md](../SETUP_WINDOWS.md)). Pass B produces exactly what
`_mask_to_polygon` produces, so the same `build_dataset` writer can be pointed at
`labels.duckdb` and CVAT drops out of the loop entirely.

---

## 2. The blocker: masks are not persisted

**Nothing in this feature is possible until segmentation masks exist as data.**
This is the single largest item in the workstream and it is upstream of every
line of UI.

### 2.1 Where the mask dies today

`detect/base.py:17` — `Instance.mask` is a bool `HxW` array, annotated
*"not persisted to the DB"*. `pipeline.segment` uses it to compute `area_px`,
`ground_px`, `posture`, draws it into `artifacts/<ds>/overlays/<cam>/<idx>.jpg`
via `render_overlay`, inserts a `detections` row that carries **only the bbox**,
and drops the array.

So the only surviving record of a mask is baked pixels in a JPEG. That is enough
for `SegmentationReview.tsx` to *show* the overlay and nothing else: you cannot
hit-test a mask, cannot verdict one, cannot hide one, cannot correct one.

### 2.2 Persisting it

Add one column, `detections.mask_poly VARCHAR` — JSON `[[x,y], …]`, **full-frame
pixel coordinates**, the largest external contour, `approxPolyDP`-simplified.

- *Full-frame px, not bbox-relative and not normalised*: it is the same space as
  `bbox_*`, the same space `scene/regions.py` polygons live in, and the same
  space `ImageClicker` already edits in. Every other choice costs a conversion at
  every boundary.
- *Largest external contour only*: `finetune/dataset.py:_mask_to_polygon`
  already makes exactly this reduction for the YOLO-seg export, so persisting
  anything richer would be data no consumer reads. Store `mask_parts` (the
  contour count) alongside it, so a fragmented mask is *visible* to the reviewer
  as a plausible cause of "imprecise" rather than a silently truncated shape.
- *`approxPolyDP` at ε ≈ 2 px*: a raw contour is 200–600 points per cow; at ε=2
  it is 25–50. At ~500 B/row this is ~100 MB per 200 k detections — acceptable in
  DuckDB; the raw contour would not be.

Three edits, and **all three are required** or masks vanish on the first clip:

1. `db.init_db` — `ALTER TABLE detections ADD COLUMN IF NOT EXISTS mask_poly VARCHAR`
   (and `mask_parts INTEGER`), in the existing forward-compat block.
2. `db.DET_COLS` — append `mask_poly`, `mask_parts`. This list drives both
   `insert_detections` and `restore_clip`'s column lists.
3. `ALTER TABLE clipped_detections ADD COLUMN IF NOT EXISTS mask_poly VARCHAR`
   (ditto `mask_parts`). `clipped_detections` is created by `CTAS … WHERE 1=0`
   **once**; on every already-deployed DB it exists without the new columns, so
   the CTAS at `db.py:158` will not add them and `clip_camera` would fail — or
   worse, a hand-fixed insert list would restore rows with the masks dropped.

`purge_dataset` needs no change: the column travels with the row.

### 2.3 Backfilling already-processed data

`pipeline.segment` only touches frames where `processed = FALSE`, so existing
datasets stay maskless. A new CLI stage:

```
cownting remask [--dataset <id>] [--camera <id>]
```

It re-runs the segmenter over already-processed frames and **writes `mask_poly`
only**, matching each re-predicted mask to an **existing** `detections` row by
IoU ≥ 0.9 against the stored bbox.

> The `UPDATE`-only, match-by-IoU shape is not a nicety. `bbox_*`, `frame_path`,
> `camera_id`, `dataset_id` and the ordinal are all key material
> (`labels_db.instance_key`). Re-inserting detections would re-mint every
> `instance_key` on the day and orphan every class label M3 has collected, which
> is precisely the damage `reconcile_dataset` exists to repair after a re-ingest.
> A backfill must never be able to cause it.

Unmatched rows keep `mask_poly` NULL and the run reports the match rate. A frame
whose masked fraction is below `mask_review.min_mask_coverage` (default 0.9) is
excluded from the review queue: reviewing a frame where a third of the model's
own masks are missing produces "missing cow" marks that are artefacts of the
backfill, not of the model.

Cost: one GPU pass over the frames, the same order as the original `segment`.

### 2.4 `mask_sig` — the fingerprint of the model output

`frame_sig` (M3 §2.3) fingerprints the *pixels*. A mask review is a judgement
about the *model output on* those pixels, which can change while the pixels do
not — a re-`remask` with new weights is exactly that.

```
mask_sig = blake2b-128 over  ␟.join(sorted( instance_key ␟ quantised polygon ))
```

Every queue item carries it; every submit echoes it; the server recomputes it
from the live `detections` rows and **409s on mismatch** — the analogue of M3's
`taxonomy_stale`. Without it, "this mask is imprecise" silently re-attaches to a
mask drawn by a different model version and the QC numbers become fiction.

This is the one place M4 deliberately departs from M3 §4.3: the seg submit path
**reads the main DB** (an indexed read of one frame's detections) where the label
submit path reads nothing. M3's rule was about never adding a *writer* to the
hot path — DuckDB permits concurrent readers, and `labeling.queue` already reads
here. The alternative, echoing every mask's anchor back for re-hashing, is
circular: it validates the client against itself.

---

## 3. The unit of work is a frame

### 3.1 `frame_key`

```
frame_key = sha256( 'v1' ␟ dataset_id|'' ␟ camera_id ␟ basename(frame_path) )[:32]
```

Byte-identical recipe to `labels_db.instance_key` minus the bbox and the
ordinal, so it reuses `_SEP`, `_basename_sql`, `KEY_VERSION` and `KEY_LEN`, and
gets a SQL twin (`frame_key_sql`) for the queue scan. Same reasoning as M3 §2.1:
`frames` has no stable id either — a clip/restore round-trip and a rebuilt DB
both invalidate anything positional.

Verdicts inside the frame key on `instance_key`, unchanged from M3. That is the
payoff of keeping the key content-derived: *"annotator A said this outline is not
a cow"* and *"annotator B said this animal is in the shade"* join on one column
with no extra plumbing.

### 3.2 What a "missing cow" is keyed on

It has no detection, therefore no `instance_key`. A mark is stored under
`(review_id, add_ordinal)` with its click point and (after pass B) its polygon.

Two annotators independently marking the same missed cow produce two unrelated
rows. **Matching them is an export-time problem, not a write-time one** — nearest
centroid within `mask_review.match_radius_px`, Hungarian-assigned per frame. Any
attempt to merge at write time needs a lease or a lock on the frame, which is the
design M3 §4.2 rejected and for the same reasons.

### 3.3 Replication

Pass A is replicated (`targets_per_frame`, default 2) — it is a measurement.

Pass B is not. It is ground truth, and a second careful tracing of the same cow
costs ten minutes to produce a polygon nobody will pick between. The queue orders
frames with an existing repair last rather than excluding them, and the item
carries `repaired_by` so the UI can say *"someone has already traced this one"*
without a lease. Duplicate repairs are wasted effort, never a correctness
problem; export takes the most recent.

---

## 4. Data model — two new tables in `data/labels.duckdb`

Same store as M3: it is already ATTACHed by `labeling.attached_labels`, already
snapshotted by `labels_backup`, already restored as one unit. A second store
would double every one of those and give the joins nothing.

```sql
-- One row per (frame, annotator, submission). Append-only, mirroring
-- `annotations`: the product is annotator variability, so an overwrite destroys
-- the signal the feature exists to measure.
CREATE TABLE IF NOT EXISTS mask_reviews (
    review_id       BIGINT PRIMARY KEY DEFAULT nextval('seq_mask_review'),
    frame_key       VARCHAR NOT NULL,
    effective_key   VARCHAR NOT NULL,      -- re-attachment, M3 §2.4, frame-level
    key_version     VARCHAR NOT NULL DEFAULT 'v1',
    annotator       VARCHAR NOT NULL,
    version         INTEGER NOT NULL DEFAULT 1,
    superseded_at   TIMESTAMP,
    outcome         VARCHAR NOT NULL DEFAULT 'reviewed',  -- reviewed|skipped|undone
    skip_reason     VARCHAR, flag_note VARCHAR,
    phase           VARCHAR NOT NULL DEFAULT 'triage',    -- triage|repaired
    dataset_id VARCHAR, camera_id VARCHAR,
    frame_path VARCHAR, frame_basename VARCHAR,
    frame_sig  VARCHAR, mask_sig VARCHAR,
    frame_w INTEGER, frame_h INTEGER,
    n_masks_served INTEGER,
    session_id VARCHAR, serve_event_id BIGINT, served_at TIMESTAMP,
    submitted_at TIMESTAMP DEFAULT now(),
    time_on_task_ms BIGINT, client_elapsed_ms BIGINT,
    annotator_role VARCHAR, annotator_real_role VARCHAR,
    acting_preview BOOLEAN DEFAULT FALSE, auth_disabled BOOLEAN DEFAULT FALSE,
    app_version VARCHAR, client_info VARCHAR, viewport VARCHAR,
    UNIQUE (frame_key, annotator, version)
);

-- One row per judgement inside a review. `kind` discriminates; `instance_key` is
-- set for verdicts on existing masks and NULL for additions.
CREATE TABLE IF NOT EXISTS mask_marks (
    review_id     BIGINT  NOT NULL,
    mark_ordinal  INTEGER NOT NULL,
    kind          VARCHAR NOT NULL,     -- verdict|addition
    instance_key  VARCHAR,              -- verdict only
    verdict       VARCHAR,              -- ok|not_a_cow|imprecise
    point_x DOUBLE, point_y DOUBLE,     -- addition: the pass-A click
    polygon       VARCHAR,              -- pass-B geometry, JSON [[x,y],…] full-frame px
    n_vertices    INTEGER,
    area_px       DOUBLE,
    iou_source    DOUBLE,               -- corrected vs. model polygon; how wrong it was
    src_bbox_x1 DOUBLE, src_bbox_y1 DOUBLE, src_bbox_x2 DOUBLE, src_bbox_y2 DOUBLE,
    PRIMARY KEY (review_id, mark_ordinal)
);
```

Two tables rather than four (`verdicts` / `additions` / `edits` / `reviews`):
every query is a filter on `kind`, one insert loop covers the submit, and it is
the `annotations` / `annotation_choices` shape the store already has.

Views, `CREATE OR REPLACE` like the rest:

- `v_current_mask_reviews` — `superseded_at IS NULL AND outcome <> 'undone'`
- `v_frame_coverage` — per `effective_key`, distinct annotators reviewed /
  skipped, `max(submitted_at)`, `bool_or(phase = 'repaired')`. The queue's
  anti-join, exactly as `v_instance_coverage` serves M3's.
- `v_mask_verdicts` — flattened `review ⋈ mark` for the agreement SQL, keyed on
  `instance_key`, so it joins straight to `v_current_answers`.

**The verdict vocabulary is frozen in code, not poweruser-editable.** The class
taxonomy is editable because the UI only has to render a list. Here, the repair
pass *branches* on the verdict — `imprecise` opens an editor seeded with the
model polygon, `addition` opens an empty one, `not_a_cow` opens nothing. A fourth
verdict added at runtime would have no repair semantics and no export meaning.

Config lives in a new `MaskReviewCfg` → `config.mask_review`, keeping the
`LabelCfg` (CVAT) / `AnnotationCfg` (in-app classes) naming discipline:
`targets_per_frame: 2`, `skip_retire: 3`, `batch_size: 4`, `max_batch_size: 20`,
`min_mask_coverage: 0.9`, `match_radius_px: 60`, `simplify_eps_px: 2.0`,
`min_polygon_points: 3`.

---

## 5. Backend

| Route | Auth | Notes |
|---|---|---|
| `GET  /api/seg/queue` | `require_labeler` | `limit`, `exclude[]`, `camera`, `day`, `dataset`, `session_id`. Same self-consuming, no-lease, exclude-paged contract as `/api/label/queue`. |
| `GET  /api/seg/progress` | `require_labeler` | Pool/coverage counts + the QC rates. |
| `GET  /api/img/seg-frame/{camera}/{frame_file}` | `require_labeler` | The raw JPEG. |
| `POST /api/seg/submit` | `require_labeler` | Review + all marks in one body. |
| `POST /api/seg/skip` | `require_labeler` | Frozen reasons, mirroring the label skip. |
| `POST /api/seg/undo` | `require_labeler` | Supersede own last review for a frame. |
| `POST /api/label/events` | — | Reused as-is; `instance_key` carries the `frame_key`. |

New module `cownting/mask_review.py`, mirroring `labeling.py`'s split: it owns
the queue scan, the ATTACH usage and the frame payload; `labels_db` grows the
DDL, `submit_review`, `undo_last_review` and the agreement SQL. Import direction
stays one-way (`mask_review` → `labels_db`).

**The queue scans `frames`, not `detections`** — filtered to `processed`,
non-empty `frame_path`, and having at least one detection with a non-NULL
`mask_poly` at or above `min_mask_coverage`. Coverage comes from
`v_frame_coverage`; ordering is `n_reviewed ASC, day DESC, md5(annotator ‖ key)`,
the same per-annotator permutation M3 uses so two annotators meet in the middle
instead of racing down one list. One `served` event per item, same as M3 §4.2.

**Masks ride in the queue item.** A frame is 5–40 masks × ~30 points ≈ 10 KB of
JSON; prefetching three ahead costs ~40 KB against a full-frame JPEG the client
is about to decode anyway. A second round-trip per frame buys nothing.

```jsonc
{
  "frame_key": "…", "dataset_id": "…", "day": "2025-07-14", "camera_id": "cam3",
  "frame_file": "00001234.jpg", "frame_sig": "…", "mask_sig": "…",
  "frame_url": "/api/img/seg-frame/cam3/00001234.jpg?dataset=…",
  "width": 3840, "height": 2160,
  "masks": [ { "instance_key": "…", "bbox": [x1,y1,x2,y2],
               "polygon": [[x,y], …], "score": 0.91, "parts": 1 } ],
  "n_annotators": 0, "target": 2, "repaired_by": null, "serve_event_id": 8123
}
```

`width`/`height` come from a PIL header open when the item is built — the same
cost class as `frame_sig`, and cheaper than adding columns to `frames` and a
migration to fill them.

`/api/img/seg-frame` is a **new** route rather than `kind=` on `/api/img/frame`,
for the reason M3 §4.5 gives for the crop endpoint: the queue is cross-day by
design and `lib/api.ts`'s `withDs()` stamps the selected day onto any `/api/`
URL. The URL is built server-side and carries its own `dataset`. It applies the
same three whitelists as `img_label_crop` (`_safe_path_id`, `valid_camera_id`,
`_safe_frame_file`) plus the resolve-under-`artifacts_dir` assertion.

It serves the frame **unmasked**. M3 masks the Brinno timestamp bar because time
of day predicts the "Sun exposure" answer; whether an outline traces a cow is not
a function of the clock, so masking here would only hide pixels the annotator may
need at the frame edge. Called out because it is a deliberate divergence, not an
oversight — see §9.

**Submit** re-derives `frame_key` from the echoed anchor in Python (`verify_anchor`'s
frame-level twin, a 400 on mismatch), then reads the frame's live detections to
recompute `mask_sig`; a mismatch is a **409 `masks_stale`**, which the client
handles like `TaxonomyStaleError` — refetch the frame, keep whichever verdicts
still name a live `instance_key`, re-present. Polygons are validated server-side:
≥3 points, finite, inside `[0,w]×[0,h]`, ≤ a point cap, and re-simplified at
`simplify_eps_px` so a 4000-vertex freehand trace cannot land in the store.
`iou_source` is computed here, not client-side — it is a QC statistic and the
client must not be able to flatter it.

---

## 6. Frontend

### 6.1 The toggle

`/label` (classes, unchanged) and `/label/masks` (this page), with a segmented
control in the shared header. **Routes, not in-page state**, for the reason
`LabelClasses` already lives on its own route: the Label page owns every
keystroke, and two hotkey layers in one tree fight over the digits. Routes also
survive a reload and are linkable. Both are open to any signed-in role.

`pages/LabelMasks.tsx` reuses M3's page-level machinery nearly verbatim — the
prefetch buffer, `RECENT_CAP`/`REFILL_AT`, the active-visibility clock, the
`PendingWrite` retry-on-`online`, the undo stack, the five terminal states. That
similarity is worth *extracting*: lift the buffer + write-retry + session
bracketing into `lib/annotationQueue.ts` and have both pages consume it, rather
than shipping a 1200-line copy of `Label.tsx` that drifts from it within a month.

### 6.2 The canvas

New `components/MaskCanvas.tsx`. It cannot be `ImageClicker` with more props:
that component has exactly one "current" line and no notion of hit-testing
*inside* a shape, and this canvas needs N independently selectable, hoverable,
per-verdict-coloured polygons.

What it *should* share is the proven part — zoom-toward-cursor, pan with the
drag threshold, `toNatural`, the counter-scaled vertex markers and the
non-scaling stroke. Extract that into `lib/useImageView.ts` and have both
components use it; two divergent copies of that math is the predictable failure.

Point-in-polygon hit test client-side (even-odd ray cast, smallest-area match
wins on overlap) so hovering is free.

### 6.3 Pass A — triage

- Masks drawn as translucent fills, colour by verdict: unjudged neutral, **ok**
  green, **not a cow** red, **imprecise** amber. Progress is legible as colour
  coverage: an all-coloured frame is a finished frame.
- Hover highlights; `1`/`2`/`3` set the verdict of the hovered (else selected)
  mask; click cycles it. `Tab`/`Shift-Tab` walks masks in reading order so the
  frame is completable without ever leaving the keyboard.
- `A` arms *add-a-cow*; the next click on bare ground drops a numbered marker.
  Clicking a marker removes it. Markers deliberately have no shape yet — a
  centre point is a two-second act and a polygon is not, and the counting
  statistic only needs the point.
- `H` held hides all overlays (M3's ring-hide gesture) — the only honest way to
  answer "is that actually a cow" when a mask covers it.
- `M` marks every unjudged mask **ok** — most frames are mostly right, and
  forcing 30 identical keystrokes trains the annotator to press it without
  looking.
- `Enter` submits when nothing is flagged, otherwise enters pass B.

### 6.4 Pass B — repair

A work list of what pass A flagged: additions first (an empty canvas is easier
cold), then imprecise masks. For each, the surrounding masks stay visible and
dimmed so the annotator can see what is already claimed.

- **Addition** — click to lay vertices, drag to move, double-click to delete,
  `Enter`/double-click near the first vertex to close. The `ImageClicker`
  polyline idiom exactly, so muscle memory from the count-area editor transfers.
- **Imprecise** — the editor opens seeded with the model's polygon. Same drag /
  double-click-delete; clicking on a *segment* inserts a vertex there, which is
  the gesture the request names and which the count-area editor does not yet
  have (it only appends). `R` reverts to the model polygon.
- Live readout: vertex count and IoU against the source, so a "correction" that
  is really a redraw is visible as it happens.
- `Esc` returns to the work list; the frame submits when the list is empty, or
  earlier via an explicit **Submit anyway** that records the remainder as
  `flag_note` — never a silent partial write.

### 6.5 Progress panel

`components/MaskProgress.tsx`, the `LabelProgress` idiom: frames reviewed /
remaining, and the numbers this feature exists to produce — false-positive rate,
miss rate, imprecise rate, mean corrected IoU, all with their `n`. These are
per-model-version by construction, since `mask_sig` changes when the model does.

---

## 7. The output

`cownting seg-export [--dataset …] [--min-annotators 1]` → a YOLO-seg dataset,
reusing `finetune/dataset.py`'s writer with `labels.duckdb` as the source:

- masks judged **ok** by ≥ `min_annotators` → kept as-is from `mask_poly`
- **not a cow** → dropped (and worth a hard-negative list for later)
- **imprecise** with a pass-B polygon → the corrected polygon
- **additions** with a polygon → new instances, after centroid matching (§3.2)
- frames with any unrepaired flag → excluded, since a frame with a known missing
  cow trains the model that the cow is background

This is the same normalised-polygon text format `build_dataset` writes, so
`cownting finetune` consumes it unchanged, and `export_cvat.py` becomes optional.

---

## 8. Phasing and effort

| # | Phase | Effort | Ships as |
|---|---|---|---|
| 0 | `mask_poly` + `mask_parts` persisted in `segment`; `DET_COLS`, both ALTERs, clip/restore test | **M** | new data gets masks |
| 1 | `cownting remask` backfill (IoU-matched `UPDATE`) | **M** | old data gets masks |
| 2 | `labels_db` DDL, `frame_key`, `mask_sig`, `submit_review`, `undo_last_review` | **M** | store |
| 3 | `mask_review.py` queue + the five routes + `/api/img/seg-frame` | **L** | backend complete |
| 4 | `useImageView` extraction + `MaskCanvas` + pass-A triage + the toggle | **L** | usable, triage-only |
| 5 | Pass-B repair editor | **L** | ground truth |
| 6 | Agreement SQL, progress panel, QC rates | **M** | the measurement |
| 7 | `seg-export` + fine-tune loop closed | **M** | CVAT retired |

Phases 0–4 are the minimum that produces anything: triage alone already yields
false-positive and miss rates, which is the number most worth having and the one
that decides whether phase 5 is worth its cost.

**Tests** (mirroring `test_labels_schema.py` / `test_labels_api.py`):
`mask_poly` survives a clip/restore round-trip and a re-`init_db` on an
old-schema DB; `remask` never changes an `instance_key`; `frame_key` agrees
between its Python and SQL producers; the queue self-consumes and retires on
skips; a stale `mask_sig` 409s; polygon validation rejects out-of-frame,
degenerate and oversized input; export drops `not_a_cow` and excludes
unrepaired frames.

---

## 9. Open decisions

1. **Backfill scope.** Is reviewing already-processed footage required, or is
   masks-from-now-on enough? Skipping phase 1 removes a GPU pass and the
   match-rate risk, at the cost of every dataset uploaded so far.
   *Recommendation: build it — the corpus that exists is the corpus worth
   reviewing.*
2. **Is pass B mandatory in the same sitting?** As written, yes: flag it, fix
   it, submit. The alternative is a separate repair queue fed by triage flags,
   which lets triage stay fast and lets one careful annotator specialise —
   at the cost of a second queue and a second set of terminal states.
   *Recommendation: same sitting for v1, since context on why a mask was flagged
   is freshest immediately; revisit if triage throughput suffers.*
3. **Multi-part masks.** Store only the largest contour (matching the export), or
   the full ring list with only the largest editable? *Recommendation: largest
   only, with `mask_parts` recorded so a fragmented mask is visible as such.*
4. **Banner masking** on the seg frame. §5 argues it is unnecessary here; if
   these frames are ever reused for a time-sensitive question, that changes.
5. **Repair replication.** §3.3 assumes one tracing per cow. If corrected-mask
   agreement (mean pairwise IoU between two annotators tracing the same animal)
   is itself a result worth publishing, a deep-overlap slice like M3's 20% would
   need to be carved out here too.
