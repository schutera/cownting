# Workstream M6 — Body points in the Label flow

> Plan, 2026-10-15, against `main @ 6f95933`. Replaces the deleted
> `plan/pose-label-flow` branches. This version has no pose model in it. The
> labeler places five points on each cow by hand, in a fixed order.

## 1. Decisions

These were made with the owner on 2026-10-15. They are not open.

| # | Decision |
|---|---|
| 1 | **No model seed.** The labeler places every point. There is no prediction pass, no new job, no new dependency in the image. |
| 2 | **Five points:** nose, top of the shoulders, tail base, one front hoof, one hind hoof. |
| 3 | **Every cow gets the step.** There is no size floor for the step. The labeler can answer **Too small** for the whole cow. |
| 4 | **Guided order.** The panel names the next point. One click places it. One key marks it *can't see*. After the fifth point the step shows all five and **Enter saves them** — the fifth point does not save by itself (owner's review, 2026-10-18). |
| 5 | **Per-point escape.** Each point can be *can't see*. A hidden point is stored as `null`, not dropped. |
| 6 | The step is **step 2 of 4**: outline, body points, sun exposure, behaviour. |
| 7 | Points are stored in **full-frame pixels**, in `labels.duckdb`, append-only, like `mask_edits`. |
| 8 | The queue does **not** change. No new lane, no new sort term, no new `queue_reason`. |

Why five points and not a public 17-point schema: the labels serve two uses. One
is a direct behaviour measurement — head drop (grazing), back line (orientation),
leg drop (standing against lying). Five points give all three. The other is a
future keypoint model; five points are a subset of any larger schema and can be
mapped to it later. Seventeen points cost about four times the clicks and most of
them are hidden on a side-on cow.

## 2. The point vocabulary

Frozen in code, like `MASK_EDIT_KINDS`. Index order is click order and storage
order.

| index | key | shown to the labeler | where to click |
|---|---|---|---|
| 0 | `nose` | Nose | The tip of the muzzle. |
| 1 | `shoulders` | Top of the shoulders | The highest point of the back, above the front legs. (The withers.) |
| 2 | `tail_base` | Tail base | Where the tail joins the body. |
| 3 | `front_hoof` | Front hoof | The front hoof you can see best, where it touches the ground. |
| 4 | `hind_hoof` | Hind hoof | The hind hoof you can see best, where it touches the ground. |

The order is head to tail, then legs. The first three points are on the back
line and are the ones most often visible, so a labeler who stops early still
gives the back line.

Left and right are not recorded. The footage is one oblique view, and the plan
does not ask the labeler to decide which leg is which.

## 3. What the labeler sees

### 3.1 The flow

```
[1/4] Is there a cow in the box?    Enter yes · E fix · X no cow
[2/4] Body points                   click · X can't see · Backspace undo · S too small · Enter save
[3/4] Sun exposure                  Y X C
[4/4] Behaviour                     Y X C V B
```

The body points step uses the same crop, the same panel rectangle and the same
counter as the other steps. Nothing moves on the screen when the step changes.

### 3.2 The panel

The panel has three parts, left to right:

1. **The stick cow.** A small fixed SVG figure of a cow, side view, with the five
   points drawn as dots and numbered 1–5. The next point is highlighted. A placed
   point is filled. A hidden point is drawn as a cross. This is the only place the
   labeler learns where "top of the shoulders" is; the words alone are not
   enough. The figure has a **neck** drawn between the head and the body: without
   one, the shoulder dot sat where the head met the body and could be read as
   "the end of the head" (owner's review, 2026-10-18).
2. **The prompt.** One line: the name of the next point, then the "where to
   click" text from the table. Example: `Top of the shoulders — the highest point
   of the back, above the front legs.`
3. **The keys.** Three tiles, in the same style as the outline step:
   `X · Can't see`, `Backspace · Undo`, `S · Too small`.

### 3.3 The crop

The points are drawn over the crop as dots with their number (1–5), the same
numbers the stick cow shows. Not letters: N, S, T, F, H would put the letters of
the S, F and H keys on the photo. Lines join nose → shoulders → tail base, and
shoulders → front hoof, tail base → hind hoof, when both ends exist. The wheel
zooms through the server's ladder, exactly as the outline editor does; a point
keeps its frame position at every zoom level. Hold H hides the dots. Hold Space
shows the full frame, with the dots drawn over it.

A click outside `point_bounds` (§5.2) or outside the photo is refused with a
shake and a hint, at the click. The server would refuse it too, and the client
queue retries a refused write forever.

### 3.4 Keys

| key | action |
|---|---|
| click on the crop | Place the next point there. |
| `X` | Mark the next point *can't see* and move on. Refused for the fifth point when no point was placed: five "can't see" is not a set of points. The hint names `S` and `F`. |
| `Backspace` | Remove the last point placed or hidden. The prompt goes back one. |
| `S` | *Too small*: the whole cow is too small for points. Ends the step. |
| `←` | Back to the outline step. It never removes a point: the arrows walk the steps, and an arrow that sometimes deletes is a trap. The draft is kept. |
| `→` | Blocked until the step is complete. Shake and hint, like the outline step. |
| `F` | Flag the cow, as today. Leaves the step. |
| `Space`, `H` | Holds, as today. |

`Enter` (or the amber **Save points** button) saves the five and goes on. It
waits until all five are given; before that it shakes and says so. The fifth
point never saves by itself: the labeler sees the whole set first and can still
take a point back. `S` saves at once, because that one key press is the whole
verdict. `→` never saves — the outline step's rule, looking is not judging.

`X` and `S` are safe to use: the answer letters are inert during this step,
exactly as they are during the outline step.

### 3.5 Revisiting

`←` from Sun exposure returns to the body points step. The placed points are
shown. `Backspace` removes the last one and the prompt reopens. `→` leaves only
while the draft is exactly what is stored (`pointSteps.leavable`); after any
change, `Enter` saves again. A revisit that changes nothing writes nothing.

### 3.7 Points-only mode

The ordinary queue never serves a cow back to the labeler who answered it, and a
cow leaves the queue at its answer target. So the cows that already have
answers — the ones where points are worth most, because they pair with the
behaviour answers — would almost never get points. On production on 2026-10-18
that was 3,248 cows.

`/label?mode=points` (the "Points only" link in the page header) serves them:

- **Who is served:** every cow with at least one current answer from anyone, no
  current `point_edits` row from anyone, and no false-positive verdict. One
  labeler's points (or *too small*) take it out — one set per cow is enough
  (owner's decision, 2026-10-18). It is not annotator-scoped.
- **Steps:** the outline (only if this labeler has not judged it) and the points.
  No questions. `Enter` on the points saves them and the next cow comes.
- **Order:** day term, legible first, under a panel first, then the per-labeler
  md5 permutation. No `worth_asking`: that is about the attribute model's
  uncertainty on a question, and no question is asked.
- **No flags.** A flag is stored as an `annotations` row with outcome
  `skipped`, and it supersedes the labeler's own current answer on the cow. In a
  pass over cows they already answered, one `F` would destroy their answer. The
  ways out are `X` on the outline (no cow), `S` (too small), and `→` for a crop
  that cannot be shown (nothing is written; the cow stays in the backlog).
- **Telemetry:** its `served` events carry `detail = 'points'`, and
  `SQL_ABANDONED` leaves them out — a points-only serve never gets an answer.
- **Progress:** `/api/label/progress` returns `points_backlog`, by the same rule
  the queue serves, and the side panel shows "N cows still need body points" in
  this mode.
- The mode is a URL parameter, not a stored preference, and switching it
  remounts the page, so no tape or draft crosses from one mode to the other.

### 3.6 Copy

The step title is **Body points**. The vocabulary table above is the copy. No
other text is added to the page.

## 4. Storage

### 4.1 Table

In `labels.duckdb`, created by `init_labels_db`, after `mask_edits` and before
`label_events`:

```sql
CREATE TABLE IF NOT EXISTS point_edits (
    edit_id        BIGINT PRIMARY KEY DEFAULT nextval('seq_point_edit'),
    instance_key   VARCHAR NOT NULL,
    effective_key  VARCHAR NOT NULL,
    key_version    VARCHAR NOT NULL DEFAULT 'v1',
    annotator      VARCHAR NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    superseded_at  TIMESTAMP,
    kind           VARCHAR NOT NULL DEFAULT 'points',   -- 'points' | 'too_small'
    points         VARCHAR,                             -- JSON, see 4.2
    n_placed       INTEGER,                             -- points that are not null
    -- provenance and telemetry: the same columns as mask_edits
    dataset_id VARCHAR, camera_id VARCHAR, frame_path VARCHAR,
    frame_basename VARCHAR, frame_sig VARCHAR,
    bbox_x1 DOUBLE, bbox_y1 DOUBLE, bbox_x2 DOUBLE, bbox_y2 DOUBLE,
    ordinal INTEGER NOT NULL DEFAULT 0,
    session_id VARCHAR, serve_event_id BIGINT,
    submitted_at TIMESTAMP DEFAULT now(), client_elapsed_ms BIGINT,
    annotator_role VARCHAR, annotator_real_role VARCHAR,
    acting_preview BOOLEAN DEFAULT FALSE, auth_disabled BOOLEAN DEFAULT FALSE,
    app_version VARCHAR, client_info VARCHAR,
    UNIQUE (instance_key, annotator, version)
);
CREATE OR REPLACE VIEW v_current_point_edits AS
    SELECT * FROM point_edits WHERE superseded_at IS NULL;
```

Rules, all copied from `mask_edits`:

- Append-only. A new submit is a new version; the old row gets `superseded_at`.
- Supersede on `effective_key OR instance_key`; version on `instance_key`.
- `effective_key` defaults to `resolve_effective_key(con, instance_key)`.
- A lost UNIQUE race raises `VersionRace`; the route returns 409.
- Index `idx_point_effective ON point_edits (effective_key)`.
- The view is created in the view block, after the ALTER block.

### 4.2 Points JSON

`points` is a JSON list with exactly five entries, in vocabulary order. Each
entry is `[x, y]` in full-frame pixels, or `null` for *can't see*.

```json
[[1180.5, 612.0], [1210.0, 560.5], [1302.0, 571.0], null, [1290.5, 668.0]]
```

- `kind = 'points'`: `points` is required and has five entries. At least one
  entry is not null; a row with five nulls is rejected with 400 — the labeler
  must use *Too small* for that.
- `kind = 'too_small'`: `points` is NULL and `n_placed` is NULL.
- Every coordinate is finite. Every placed point lies inside the widest crop the
  editor can show, with the same one-pixel margin `/api/label/mask-fix` uses.

### 4.3 Code vocabulary in `labels_db.py`

```python
POINT_NAMES: tuple[str, ...] = ("nose", "shoulders", "tail_base", "front_hoof", "hind_hoof")
POINT_EDIT_KINDS: tuple[str, ...] = ("points", "too_small")
```

A test pins `len(POINT_NAMES) == 5` and pins the order. The frontend has the
same five entries with the labeler copy; a test compares the two lists through
`/api/label/taxonomy`, which gains a `points` field carrying the server's list.

### 4.4 Tables that must learn about `point_edits`

- `KEYED_TABLES` in `labels_db.py`: add `("point_edits", "edit_id")`, so the
  reconciler moves point rows after a re-ingest.
- `labels_backup.due()`: the watermark takes the greatest `submitted_at` over
  three tables.
- `labels_backup`: a `point_edits.csv` in the bundle, after `mask_edits.csv`, with
  the same column shape plus `points` and `n_placed`. The README in the bundle
  lists it.
- `SQL_EFFORT_BY_ANNOTATOR`: no change. Effort for this step comes from
  `client_elapsed_ms` on the row and from the `answered` events.

## 5. API

### 5.1 `POST /api/label/points`

Body:

```python
class LabelPointsReq(BaseModel):
    instance_key: str
    anchor: InstanceAnchor
    kind: str = "points"                     # 'points' | 'too_small'
    points: list[list[float] | None] | None  # five entries, full-frame px, or None
    serve_event_id: int | None = None
    session_id: str | None = None
    client_elapsed_ms: int | None = None
```

Checks, in order: `_valid_anchor`; `verify_anchor` (400); `kind` in vocabulary
(400); for `'points'`: five entries, finite, at least one not null, inside the
widest crop (400); for `'too_small'`: `points` is None (400). Then
`labels_db.submit_point_edit`; `VersionRace` → 409; `ValueError` → 400.

Response: `{"ok": true, "edit_id": int, "version": int, "points": [...] | null}`.
The response echoes the stored points so the client patches its item without a
second queue fetch, like `mask-fix` does.

### 5.2 The queue item

Two fields added to the item in `labeling._item`, from one more LEFT JOIN in
`_scan_sql`'s `scoped` CTE, scoped to the annotator like `mymask`:

```python
"points": [...] | None,      # this annotator's current points, full-frame px
"points_done": bool,         # this annotator has a current point_edits row
"point_bounds": [x0, y0, x1, y1],  # where the route accepts a point
```

`point_bounds` is `labeling.widest_crop_box` around the anchor box — the same
function the route checks with. The zoom ladder is cut around the display box,
which after an outline correction is not the anchor box, so the widest zoom can
show pixels the route refuses; the client refuses those clicks instead.

`points_done` is what makes the step survive a reload. It is the same role
`geom_done` plays for the outline step, and it exists for the same reason: a
re-asked step must not overwrite the earlier answer by accident.

The join does not change `n_labeled`, `target`, `mine_done` or the ORDER BY. A
point row is not an answer and does not count toward coverage.

### 5.3 Events

`label_events` gets no new kind. The client sends one `answered` event per
action, with `class_key` = the point name (null for *too small*) and
`detail = {group_key: "points", action: "placed" | "cant_see" | "undo" |
"too_small", ms_since_group_shown, ms_since_item_shown, input_mode, phase}`.
The per-question clock restarts when the step opens, so the first point's time
does not include the outline step. The per-decision timing of M3 UX §6.1 covers
the step with no schema change.

`/api/label/progress` gains `my_points` and `my_too_small`: this annotator's
current verdicts. The side panel prints "N cows with body points · M too small".

## 6. Client

### 6.1 Files

| file | change |
|---|---|
| `lib/types.ts` | `PointName`, `POINT_NAMES`, `LabelPointsReq`, `points` and `points_done` on `LabelItem`. |
| `lib/api.ts` | `submitPoints()`. |
| `lib/useAnswerQueue.ts` | A fourth write kind `"points"`. It coalesces with an earlier unflushed `points` write for the same instance, and with nothing else. The beacon URL list gains `/api/label/points`. |
| `lib/pointSteps.ts` | **New, pure.** The guided-order reducer: `place(state, xy)`, `hide(state)`, `undo(state)`, `nextIndex(state)`, `complete(state)`. No React. This is where the behaviour lives and where the tests are. |
| `components/PointEditor.tsx` | **New.** The SVG overlay on the crop. Props: `item`, `points`, `levels`, `levelIndex`, `onLevelChange`, `onPlace(xy)`, `hidden`. Reuses the viewport rule of `MaskEditor`: viewBox is the level's source square, so a click gives full-frame px with no arithmetic. No dragging in this version. |
| `components/PointsPanel.tsx` | **New.** The stick cow, the prompt, the three key tiles. Same height `PANEL_H`. |
| `components/StickCow.tsx` | **New.** The fixed SVG figure with five named dots. Props: `state`. |
| `pages/Label.tsx` | The step: `pointsByKey` state, `inPoints`, `pointsDone`, the key handling for `X`, `Backspace`, `S`, and the `FlowState` changes below. |
| `pages/Label.backTarget.test.ts` | New cases for the step. |

### 6.2 Flow state

`FlowState` gains `inPoints: boolean` and `pointsDone: boolean`. The item is now
outline → points → questions.

`backTarget`:
1. `outlineOpen` → item or none (unchanged).
2. `step > 0` on a question → `question`.
3. On the first question, `pointsDone` → `points`.
4. On the points step, `geomDone` → `geometry`.
5. On the outline step, or points not passed → item or none.

`forwardTarget`:
1. `outlineOpen` → blocked.
2. `inGeometry` → `points` if `geomDone`, else blocked.
3. `inPoints` → `question` if `pointsDone`, else blocked.
4. Questions: unchanged.

Every case has a test. The "mirror" tests are extended: from the points step,
both arrows offer a move once it is passed.

### 6.3 Keyboard, in the points step

Handled in the same block as the outline step's `Enter`/`E`/`X`, before the
answer letters:

- `X` → `hide`. `Backspace` → `undo`. `S` → submit `too_small`.
- A click on the crop → `place`. The editor calls `onPlace` with full-frame px.
- After `place` or `hide` makes the state complete, the page submits `points`
  and moves to step 3. No key is needed.
- Any answer letter → shake and hint: `place the body points first — X if you
  can't see one, S if the cow is too small`.

### 6.4 Reset per item

`pointsDraft` resets on every advance, with the other per-item state in the
`presented` effect. A draft carried to the next cow would be points placed on
one animal and saved against another.

## 7. Steps

Each step is one commit with its tests. Nothing in a step depends on a later
step. Stop after any step and the app still works.

| # | step | files | tests |
|---|---|---|---|
| 1 | **Store.** `POINT_NAMES`, `POINT_EDIT_KINDS`, the table, the view, the index, `submit_point_edit`, `KEYED_TABLES`. | `labels_db.py` | `test_labels_schema.py`: DDL idempotent and in the right block; submit, supersede, version; `too_small` rejects points; five nulls rejected; race → `VersionRace`; alias → live key; reconcile moves a point row. |
| 2 | **Route and item.** `LabelPointsReq`, `POST /api/label/points`, `points`/`points_done` on the item, `points` on the taxonomy response. | `api.py`, `labeling.py` | `test_labels_api.py`: round trip; each 400; 409 on race; `points_done` true after a submit and false for another annotator; the queue order is unchanged by a point row. |
| 3 | **Backup.** `due()` watermark, `point_edits.csv`, README line. | `labels_backup.py` | `test_labels_backup.py`: a day of only point work is due; the CSV is in the bundle. |
| 4 | **Reducer.** `pointSteps.ts`. | `frontend/src/lib` | `pointSteps.test.ts`: place, hide, undo, complete, undo after complete, no sixth point. |
| 5 | **Editor and panel.** `PointEditor`, `PointsPanel`, `StickCow`, types, api, queue write kind. No page wiring yet. | `frontend/src` | `useAnswerQueue` tests: a `points` write coalesces only with `points`. `tsc` passes. |
| 6 | **The step in the page.** Flow state, keys, submit, reset, revisit. | `Label.tsx` | `Label.backTarget.test.ts` new cases. Manual run through ten cows on the dev DB. |
| 7 | **Progress.** One line in the Label progress panel: cows with body points. | `LabelProgress.tsx`, `/api/label/progress` | existing progress test gains the field. |

Estimated size: steps 1–3 about 400 lines of Python with tests; steps 4–7 about
600 lines of TypeScript with tests. Each commit is reviewable in one sitting.

## 8. Not in this version

- A pose model, a prediction job, seeds for the dots.
- Dragging a placed dot. Use `Backspace` and click again.
- Left and right legs. More than five points.
- Any change to `posture` or to the behaviour heuristics.
- An export beyond the backup CSV. A training-set builder comes with the first
  model that needs one.
- A pose step for cows already answered before this ships. The queue hides a cow
  this annotator has answered, so the point corpus starts with the next cow
  served after deploy.

## 9. Risks

- **Session speed.** Five clicks per cow, every cow. Measure the step's median
  time from the `answered` events in the first week. If it is above the two
  questions together, the owner decides on a size floor; the config knob is one
  line and the data to set it is already recorded.
- **Hoof choice.** "The hoof you can see best" is a judgement. Two labelers can
  pick different hooves. This is acceptable for the behaviour signals (leg drop
  uses whichever hoof is given) and is recorded as is. If a model later needs
  left/right, the schema grows; the stored rows stay valid as "one front hoof".
- **The stick cow.** If the figure is unclear, labelers click the wrong part and
  the data is wrong without any error. Review the first fifty cows by eye before
  the labelers continue.
