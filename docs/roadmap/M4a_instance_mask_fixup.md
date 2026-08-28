# Workstream M4a — Instance mask fix-up on the Label page

Branch: `fix-segmentation-masks`. Scoped-down sibling of
[M4_mask_review.md](M4_mask_review.md), embedded in the M3 classification screen
([M3_labeling_ux.md](M3_labeling_ux.md)) rather than a page of its own.

The trigger is an annotator's own judgement mid-classification: *the ring around
this cow looks off*. Today their only move is `F → bad_crop`, which records that
something is wrong and repairs nothing. This workstream gives them a repair
path: a **toggle above the crop** switches the tooling under the photo from
**classification** (the question stack) to **segmentation** — the model's
instance mask appears as a polygon overlay on the cow, and the annotator fixes
it by dragging its nodes.

M4 remains the systematic review (every mask of every frame, replicated,
measured). M4a is opportunistic repair of the one instance already on screen.
It deliberately builds the pieces M4 needs anyway — mask persistence, polygon
storage, polygon validation — so nothing here is throwaway.

---

## 1. What the annotator gets

1. On `/label`, above the crop, a two-state toggle: **Classify | Outline**.
   Classify is today's screen, unchanged.
2. Switching to **Outline** replaces the ring with the model's mask polygon
   drawn over the same crop, nodes rendered as draggable handles. The question
   panel is replaced by the outline tool panel (Save / Revert / instructions).
3. Editing: **drag** a node to move it, **double-click** a node to delete it,
   **click an edge** to insert a node there. **R** reverts to the model's
   polygon. Minimum 3 nodes.
4. **Save outline** stores the corrected polygon (a new versioned row — never an
   overwrite of model output) and flips back to Classify on the same animal.
   **Esc** (or the toggle) leaves without saving. The tool also offers **Not a
   cow — remove**: the instance is recorded as a false positive instead of
   getting a polygon.
5. **The flag flow joins in.** Pressing `F` now draws the model's mask over the
   crop — the annotator sees exactly what the model claimed before deciding —
   and the flag row leads with two repair actions ahead of the five reasons:
   **Fix outline** (jumps to Outline mode) and **Not a cow — remove** (records
   the false-positive verdict and advances). The written-explanation flag path
   stays for everything that is neither: a repair is an *annotation*, and only
   the true escape hatch keeps decision 6's mandatory justification.
6. The classification flow is otherwise untouched: answers, keys, tape all
   behave exactly as before while the toggle rests on Classify.

---

## 2. The blocker, inherited from M4: masks are not persisted

Everything above needs the mask to exist as data, and today it does not:
`detect/base.py` drops `Instance.mask` after `pipeline.segment` computes
`area_px`/`posture` and draws the overlay JPEG. Only the bbox reaches the DB.

**Phase 0 of M4 (§2.2) is a hard prerequisite and is built here, to its spec:**

- `detections.mask_poly VARCHAR` (JSON `[[x,y],…]`, **full-frame px**, largest
  external contour, `approxPolyDP` at ε≈2 px) and `mask_parts INTEGER`.
- All three required edits: `db.init_db` forward-compat ALTER, `db.DET_COLS`,
  and the `clipped_detections` ALTER — see M4 §2.2 for why omitting any one of
  the three silently loses masks on the first clip.
- `cownting remask` backfill (M4 §2.3): UPDATE-only, IoU-matched against the
  stored bbox, never re-minting an `instance_key`. Without it the toggle is
  dead on every already-processed day, which is all of them.

Nothing in M4a changes that design; M4a is simply its first consumer.

---

## 3. Data model — one table in `data/labels.duckdb`

Same store, same ATTACH, same backup unit as M3 (§4 of M4 gives the reasons).

```sql
CREATE TABLE IF NOT EXISTS mask_edits (
    edit_id        BIGINT PRIMARY KEY DEFAULT nextval('seq_mask_edit'),
    instance_key   VARCHAR NOT NULL,        -- labels_db.instance_key, unchanged
    effective_key  VARCHAR NOT NULL,        -- re-attachment, as annotations has
    key_version    VARCHAR NOT NULL DEFAULT 'v1',
    annotator      VARCHAR NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    superseded_at  TIMESTAMP,
    kind           VARCHAR NOT NULL DEFAULT 'polygon',  -- polygon|false_positive
    polygon        VARCHAR,                 -- JSON [[x,y],…], FULL-FRAME px; NULL iff false_positive
    n_vertices     INTEGER,
    area_px        DOUBLE,
    iou_source     DOUBLE,                  -- vs. the model polygon; how wrong it was
    seeded_from    VARCHAR NOT NULL,        -- 'mask' | 'bbox' (no stored mask yet)
    mask_rev       VARCHAR,                 -- hash of the model polygon edited
    -- provenance block, byte-for-byte the annotations idiom:
    dataset_id VARCHAR, camera_id VARCHAR, frame_file VARCHAR,
    bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_x2 DOUBLE, bbox_y2 DOUBLE,
    ordinal INTEGER, frame_sig VARCHAR,
    session_id VARCHAR, serve_event_id BIGINT,
    submitted_at TIMESTAMP DEFAULT now(),
    client_elapsed_ms BIGINT,
    annotator_role VARCHAR, annotator_real_role VARCHAR,
    acting_preview BOOLEAN DEFAULT FALSE, auth_disabled BOOLEAN DEFAULT FALSE,
    app_version VARCHAR, client_info VARCHAR,
    UNIQUE (instance_key, annotator, version)
);
```

Append-only with `version`/`superseded_at`, exactly like `annotations`: a
second save supersedes, never destroys. `v_current_mask_edits` mirrors
`v_current_answers`.

`kind` is the two-way branch the UI actually offers — correct the shape, or
declare there is nothing to outline. It is M4's `ok / not_a_cow / imprecise`
verdict vocabulary minus `ok` (nobody opens an editor to say "fine"), and like
M4's it is **frozen in code**: each kind has export semantics and a distinct UI
path, so a runtime-added kind would have neither.

**Why not M4's `mask_reviews`/`mask_marks` now?** Those are frame-keyed and
carry triage semantics (verdicts, additions, replication targets) this feature
does not have. Building them for a single-instance editor would freeze M4's
schema before M4's own questions (§9) are answered. `mask_edits` is
instance-keyed like everything else M3 stores, and M4's export can read it as a
degenerate `imprecise`-repair with no migration (open decision 1).

Config: `AnnotationCfg` grows `mask_min_points: 3`, `mask_max_points: 200`,
`mask_simplify_eps_px: 2.0` — the same numbers M4 §4 names, one home.

---

## 4. Backend

### 4.1 Queue item

`LabelItem` gains three optional fields, built where `ring` is built:

```jsonc
{ "mask": [[x,y], …] | null,   // CROP-LOCAL px, same space as `ring`
  "mask_rev": "…" | null,      // blake2b-128 of the stored mask_poly string
  "mask_seed": "mask" | "bbox" }
```

Crop-local because `ring` already proved the idiom: the client draws it over
the image with zero math, `viewBox = crop_w × crop_h`, `preserveAspectRatio =
"none"` (see `InstanceCrop.tsx` — the structural-stretch comment). Storage
stays full-frame px (M4 §2.2's argument); the **server** owns the conversion in
both directions. The crop-box geometry (bbox → padded square) must be factored
into one function the crop endpoint and this converter both call — two copies
of that arithmetic is the drift that would silently shear every saved polygon.

An instance with `mask_poly` NULL is served with `mask_seed: "bbox"` and a
4-node rectangle at the ring — the annotator sculpts a mask from the box (see
open decision 2).

### 4.2 Submit

`POST /api/label/mask-fix`, `require_labeler`:

```jsonc
{ "instance_key": "…", "anchor": { …InstanceAnchor, echoed verbatim… },
  "kind": "polygon" | "false_positive",
  "polygon": [[x,y], …] | null, // crop-local px, as served; required iff kind=polygon
  "mask_rev": "…" | null, "seeded_from": "mask" | "bbox",
  "serve_event_id": 1, "session_id": "…", "client_elapsed_ms": 1 }
```

- Anchor re-hash via `labels_db.verify_anchor`, 400 on mismatch — the M3
  contract, unchanged.
- **Staleness:** the server re-reads the live `detections.mask_poly` for this
  instance and re-hashes it; a mismatch with the echoed `mask_rev` is a **409
  `mask_stale`** — a `remask` with new weights ran mid-session, and the
  correction would re-attach to a polygon the annotator never saw. This is
  M4's `mask_sig` (§2.4) at instance grain, and the same deliberate exception
  to "the submit path reads nothing": an indexed read, no writer.
- **Validation, server-side** (M4 §5's list), for `kind: "polygon"`:
  ≥ `mask_min_points`, finite, inside the crop box, ≤ `mask_max_points` after
  re-simplification at `mask_simplify_eps_px`. `iou_source` and `area_px` are
  computed here, in full-frame space, not trusted from the client. For
  `kind: "false_positive"` the polygon must be absent; the anchor and
  `mask_rev` checks still run (a NULL stored mask is allowed — a bbox-only
  detection can be a false positive too).
- Insert into `mask_edits` (version n+1, supersede n), one `mask_fixed` /
  `mask_removed` row in `label_events`.

**A false positive retires the instance from the classification queue** — for
everyone, not just this annotator: there is no cow to ask questions about. The
queue scan gains one anti-join on `v_current_mask_edits WHERE kind =
'false_positive'`; no flag row is written and no explanation is demanded,
because unlike a flag this is a positive act of annotation (M4's `not_a_cow`
verdict, which also carries no prose). A `polygon` edit retires nothing — the
animal still needs its answers.

`GET /api/label/queue` is otherwise unchanged; `/api/label/progress` gains
`my_mask_fixes` and `my_removed` for the side panel.

### 4.3 Export

`finetune/dataset.py`'s writer, when pointed at the labels store (M4 §7): a
current `polygon` edit **overrides** `detections.mask_poly` for that instance
(one COALESCE), and a current `false_positive` edit **drops** the instance —
and feeds the hard-negative list M4 §7 wants.

---

## 5. Frontend

### 5.1 The toggle — in-page state, deliberately not a route

M4 §6.1 puts Classes/Masks on separate routes because they are different queues
over different units of work. M4a is the **same item on the same tape**: the
toggle is per-instance tooling, not a destination, and it resets to Classify on
every advance (a mode that survives onto the next cow is the stuck-hold bug the
Space/H releases already guard against). So: local state in `Label.tsx`,
rendered as a small segmented control in the caption strip above the crop —
the one place the eye already is when the ring looks wrong.

Keyboard ownership stays single-layer, the page's standing rule: while the mode
is Outline, `resolveLabelKey` is consulted with `optionCount 0` (answer letters
inert), Space/H holds keep working, `R` = revert, `Esc` = leave without saving,
`Enter` = save. Arrow keys are inert in Outline — leaving mid-edit by tape
navigation would be a silent discard.

### 5.2 `components/MaskEditor.tsx`

Sits where `InstanceCrop`'s ring SVG sits, same structural stretch (one
absolutely-positioned box, image `object-fit: fill`, SVG
`preserveAspectRatio="none"`, `viewBox` = crop px), so the polygon tracks the
photo at any rendered size for free. The ring and scrim are hidden in Outline —
the mask replaces them.

- Translucent fill + non-scaling stroke; nodes as counter-scaled circles
  (`ImageClicker`'s vertex idiom — this is the count-area editor's muscle
  memory, on purpose, and the manual says so).
- Pointer events on the SVG: hit-test nodes first (radius in *screen* px, not
  crop px, or small crops become untouchable), then edges (nearest segment
  within threshold → insert), then nothing.
- Drag = pointermove with capture; double-click on node = delete (refused at 3
  nodes, with the shake+hint idiom, never a silent no-op); vertices clamp to
  the crop box.
- Dirty tracking: Save disabled until the polygon differs from the seed;
  Revert restores the served polygon.

What it does *not* do: zoom/pan (the crop is already the zoom — hold-Space
still shows the frame), multi-polygon, freehand. Those are M4's canvas
(`MaskCanvas` over the full frame); when M4 lands, the `useImageView`
extraction it plans is where any shared math should live. M4a's editor is small
enough (one polygon, one image box) that extracting first would be speculation.

### 5.3 The tool panel

`QuestionPanel`'s rectangle (same `PANEL_H` contract — nothing below moves) is
replaced in Outline by a `MaskPanel`: one line of instructions, the node count,
live IoU-vs-model readout, and Save / **Not a cow — remove** / Revert / Cancel.
Saving a polygon shows the same `saved ✓` chip, flips to Classify, and does
**not** advance — the animal still needs its questions answered; fixing the
outline was a detour, not an answer. **Not a cow — remove** submits the
false-positive verdict and *does* advance: there are no questions left to ask
about a cow that isn't there.

### 5.4 The flag row grows two repair actions

Opening the flag (`F`) now also draws the model's mask over the crop — the
scrim/ring swap for the mask overlay while `flag.open`, so the annotator judges
the model's claim, not just the crop. The flag row leads with **Fix outline**
(closes the flag, switches the toggle to Outline) and **Not a cow — remove**
(the same submit as §5.3's button) ahead of the five reasons; the reasons keep
their `1`–`5` keys and their mandatory explanation, unchanged. The `no_cow`
flag reason stays for the maskless/degenerate cases, but the hint text in the
row points at Remove — a structured verdict beats prose whenever the mask is
there to judge.

Writes ride `useAnswerQueue` with a new `kind: "mask"` — the optimistic queue,
retry-on-`online`, and the sync dot come for free, and a 409 `mask_stale`
surfaces through the same stale-notice path the taxonomy 409 uses.

### 5.5 Empty state

If the item has no stored mask and bbox-seeding is off (decision 2), the toggle
renders disabled with a title: *"no stored outline for this animal yet — run
remask"*. Never hidden: a control that appears only sometimes reads as flaky.

---

## 6. Phasing

| # | Phase | Effort | Ships as | Status |
|---|---|---|---|---|
| 0 | M4 phase 0 verbatim: `mask_poly`/`mask_parts` persisted; `DET_COLS`; both ALTERs; clip/restore test | **M** | new data gets masks | **todo** |
| 1 | `cownting remask` backfill (M4 phase 1 verbatim) | **M** | old data gets masks | **todo** |
| 2 | `mask_edits` DDL + views; `submit_mask_edit`; `crop_to_frame` beside `crop_geometry` | **M** | store | **done** |
| 3 | `POST /api/label/mask-fix`; validation; queue anti-join on false positives | **M** | backend | **done** |
| 4 | Toggle + `MaskEditor` + `MaskPanel`; queue `kind: "mask"`; bbox seeding | **L** | the feature | **done** |
| 5 | Flag-row integration: mask overlay on `F`, Fix outline / Not a cow — remove | **M** | the triage shortcut | **done** |
| 6 | Export override (COALESCE + drop) + progress counters | **S** | loop closed | **todo** |

**What phases 0–1 still gate.** Everything below the editor is built and
exercised, but with no `detections.mask_poly` there is no model polygon to show:
every instance is `mask_seed: "bbox"`, so the editor opens on a rectangle at the
ring rather than on the model's outline, the flag row draws no amber shape, and
`mask_rev`/`iou_source` are null (the 409 `mask_stale` check is written but
cannot fire — see §4.2). Sculpting from the rectangle and the false-positive
verdict both work today and both store correctly; the *review* half of the
feature arrives with phase 0.

Tests, mirroring `test_labels_schema.py` / `test_labels_api.py`: the phase-0
suite from M4 §8 unchanged; crop-local→full-frame round-trips to sub-pixel
against the crop endpoint's own geometry; submit 400s a bad anchor, a
degenerate polygon (<3, non-finite, out-of-crop, >cap) and a false_positive
that carries one, 409s a stale `mask_rev`; a second save supersedes, never
overwrites; a false positive disappears from the queue for every annotator; a
polygon edit does not; export prefers the edit over `mask_poly` and drops
removed instances.

---

## 7. Open decisions

1. **Store convergence with M4.** `mask_edits` here, `mask_marks` there — one
   reconciliation when M4 lands (its export reads both, or a one-shot migration
   folds edits into repair marks). *Recommendation: accept the debt; it is one
   table with one reader, and it buys M4 unfrozen schema.*
2. **Bbox seeding when `mask_poly` is NULL.** Sculpting a mask from a 4-node
   rectangle is real work but turns the toggle from "sometimes dead" into
   "always a repair path", and `seeded_from` keeps the provenance honest.
   *Recommendation: build it; it is ~0 extra editor code.*
3. **Does a saved fix retire the instance from anyone else's Outline?** As
   written, no — edits are per-annotator versions and the queue is untouched.
   A `repaired_by`-style hint on the item (M4 §3.3) would tell a second
   annotator someone already traced this one. *Recommendation: defer; M4a's
   entry point is opportunistic, not a queue, so collisions are rare.*
4. **Should `no_cow` flags be retired now that Remove exists?** The reason
   stays for instances with nothing judgeable on screen, but two channels for
   one judgement invite drift. *Recommendation: keep both, and let the flag
   row's hint steer toward Remove whenever a mask is being shown; revisit once
   the false-positive counts have a few weeks of data.*
5. **Does a false-positive removal need a second opinion before the queue drops
   the instance for everyone?** As written one annotator's verdict retires it.
   M4's replicated triage would answer this properly. *Recommendation: accept
   single-verdict for v1 — the row is versioned and undoable, and nothing is
   deleted from `detections`.*
