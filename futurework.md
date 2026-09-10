# Future work

Active priorities first, then deferred items. When something here ships, move it
to [release.md](release.md) with the date and keep this file to what's still open.

---

## Active priorities

### Pose estimation for behaviour

**Status:** in progress. M1 (zero-shot ViTPose++ on mask-zeroed crops, flag-off)
is built and logged in [release.md](release.md). This section covers turning that
scaffold into a trustworthy behaviour signal that **replaces** today's coarse
posture proxy (mask elongation via `minAreaRect` — unreliable from a single
oblique view).

**Approach.**
- Run a cow **keypoint estimator** (skeleton: head, neck/withers, spine, hips,
  legs/hooves, tail) on each detected instance — crop by the seg mask/bbox and
  predict keypoints (animal-pose backbone / a fine-tuned top-down pose head; the
  mask gives a clean, background-suppressed crop).
- Derive **behaviour** from the pose + its change over time: **grazing**
  (head-down, neck angle low), **standing** vs **lying** (body height / hip-hock
  geometry), **ruminating/resting**, and — with ReID-linked sequences — **gait /
  lameness** and activity bouts.
- Populate the reserved `posture`/behaviour fields properly and, once tuned,
  **remove the elongation ratio heuristic**. Gate behind the existing
  `flags.pose_enabled`.

**Open work from M1.**
- **Tune thresholds** — the zero-shot leg-ratio mapping mislabels (e.g. grazing →
  lying). Needs calibration against real footage before it can override the proxy.
- **Wire pose into the standing/resting heuristic** as the primary signal, then
  retire the bbox-ratio proxy.

**Notes / prereqs.**
- Pose is far more robust than the elongation proxy, but the **fisheye + oblique
  view** distorts limb geometry — treat keypoint angles as view-dependent and
  normalise them per camera (e.g. learn the distortion from the pose data itself)
  rather than assuming a metric camera model.
- Behaviour *dynamics* (grazing bouts, gait) want identity-consistent sequences
  → best paired with **ReID** (deferred, below); instantaneous per-frame posture
  works without it.
- Training data: bootstrap keypoints like the Stage 1b mask loop (predict →
  correct in CVAT → fine-tune).

### Solar-panel segmentation (shade & agrivoltaics)

**Status:** partially implemented. Phase 1 — static **per-camera panel ground
footprints** and the `pct_sheltering` KPI — is done (see [release.md](release.md)).
What remains is panel/shadow **segmentation** and the **sun-dependent moving
shade map**.

**Panel/shadow segmentation.**
- Add a **panel** (and optionally **shadow**) class to the segmentation model
  (extend the fine-tuned YOLO-seg, or a dedicated model). Panels are near-static
  in ground position and only **rotate** (trackers), so a panel can be segmented
  once per camera and its tilt tracked over the day. This would **auto-extract
  the footprint corners** now traced by hand.
- (From the first feedback round: add shadow segmentation to the picture, not
  only cow-instance segmentation.)

**Sun-dependent moving shade map.**
- The static footprint is only a **proxy**: the actual shadow moves through the
  day as the sun tracks and the panels rotate, so `under_panel` (footprint
  occupancy) is not the same as *in shade*. Compute **shade** either directly
  (segment ground shadows) or geometrically (panel tilt + sun position from
  date/time/geo) → a **time-varying shade map**.
- Fills the reserved per-detection **`in_shade`** flag: label each localized cow
  as in-shade vs sun (distinct from footprint occupancy), and quantify
  shade-seeking behaviour over the day.
- Feeds the behaviour analysis (pose + shade + position → "resting in shade under
  panel row N at noon").

**Notes.** Gate the moving-shade stage behind the existing `flags`/`shade.enabled`.
Since Cownting is calibration-free, the shade map lives in **image space** too —
compute shadow footprints per camera (segmented or geometric) and test cow ground
points against them, exactly like the panel footprints.

### Show the classification on the image, not just the mask

**Status:** open. Wanted once [M5](docs/roadmap/M5_instance_attrs.md)'s
predictions are trusted enough to look at.

The overlay currently says WHERE each animal is. It should also say what the
model thinks it is — a small sun / cloud beside each instance for sun exposure,
and the behaviour class beside that.

**Reuse what the taxonomy already has.** `label_classes.icon` holds a name from
`CLASS_ICONS` (never markup, never an asset path), `ClassIcon.tsx` renders it,
and the taxonomy editor and the answer tiles both already draw from it. So a
poweruser who adds a class and picks its icon gets that icon on the overlay for
free, and the icon on the image is by construction the icon on the tile the
annotator pressed. Nothing new to invent, and one vocabulary rather than two.

**Draw them client-side as SVG, not burned into the PNG.** There are two overlay
paths today: `detect/overlay.py` burns masks into an image at segment time, and
`InstanceCrop.tsx` draws the ring/polygon as SVG over the served pixels. The
ring's own comment gives the argument for the second: the stroke stays a
hairline at any rendered size, hiding it costs no network, and a style change
does not invalidate every cached image. Burning icons in would additionally mean
that re-scoring a day (a new model rev — routine, see M5) requires re-rendering
every overlay PNG for it, which turns a cheap DB update into a GPU-less
re-render of 7,261 frames.

**The trap worth designing around: confidence.** `attr_*_p` sits beside every
prediction precisely so a 0.47 `standing` is not presentable as a 0.98 `lying`,
and an icon is the most confident-looking widget available — it reads as a fact.
Options, cheapest first: don't draw below a threshold; draw it at reduced opacity
scaled to the probability; or draw it hollow below a threshold and filled above.
Whatever is chosen, an UNSCORED instance must be visually distinct from a
confidently-classified one, and both must be distinct from a low-confidence
guess. Three states, not two.

Note the gate is deliberately absent from this list. A detection the gate
rejects should simply not be drawn at all when `gate_enabled` — it is not a cow,
so it has no attributes to display, and giving it an icon of its own would put
it back on screen through the side door.

### Stop serving unjudgeable crops

**Status:** open. Blocks nothing, but it is the cheapest available quality win on
the annotation corpus and [M5](docs/roadmap/M5_instance_attrs.md) is waiting on
it.

**The symptom.** Annotators are spending their time on crops nobody could
answer. As of 2026-09-09, against 543 labeled instances:

- `low_resolution` is the flag raised **556 times** — more than every other flag
  combined (`multiple_cows` 23, `occluded` 7, `no_cow` 4, `other` 1, `bad_crop`
  1). Alexandra alone flagged 571 instances against 412 she could answer, so
  more than half of what the queue handed her was unanswerable.
- `not_visible` — "too little of the body is visible" — is **136** of the sun
  answers and **64** of the behaviour answers.

**Target: `not_visible` below 10% of answers on each question.** That is the
number to hold the fix to.

**What the queue does today.** Nothing clever, deliberately:
`ORDER BY md5(annotator || instance_key)` — a stable pseudo-random permutation,
per annotator, over every instance not yet at its coverage target. Two
annotators therefore walk the same pool in different orders and meet in the
middle instead of racing down one list. It is uniform sampling and it is honest,
but it means the queue never asks whether an instance is *worth* asking about,
and it is why the class balance is what it is (feeding 161, head_probing 12).

**Approach.** Refuse to serve a detection whose crop cannot carry an answer,
using signals already on the row — `area_px` above all, and the bbox's short side
in SOURCE pixels, before `crop_max_width` upscales it into a convincing-looking
but information-free tile. Tune the floor against the `low_resolution` flags
already collected; they are, conveniently, a labeled dataset for exactly this
decision. `skip_retire` already retires an instance after 3 flags — this stops
it being served a first time.

> **Implementation hazard, and it is a silent one.** The size floor must go in
> the OUTER filter (on `scoped`), never in `_scan_sql`'s innermost CTE. That CTE
> may only filter on *partition* columns of the ordinal window — `dataset_id`,
> `camera_id`, `frame_path` — because those drop whole partitions and cannot
> renumber the survivors. `area_px` and `score` are not partition columns:
> filtering on them there renumbers `ordinal`, which changes `instance_key`,
> which orphans every label already written for the rows that shift.
> `labeling._scan_sql`'s docstring says this; it is repeated here because this
> is the change most likely to reach for that spot.

**Now that a model exists, there is a second, larger version of this.** M5 scores
every detection, so the queue could prioritise instances the model is UNSURE
about (`attr_*_p` near the decision boundary) or where it predicts a starved
class — turning annotation into active learning rather than uniform sampling.
That is a bigger change and it carries its own bias: a corpus selected by model
uncertainty is no longer a random sample, so it can sharpen the model while
making the label store useless as a measure of what the herd actually does. If
it is built, the uniformly-sampled slice has to be kept and marked, or the
agreement statistics and any prevalence estimate quietly stop meaning what they
say.

**Why it matters beyond annotator hours.** Every unjudgeable crop that reaches
the queue costs an answer slot *and* teaches M5's classifier that `not_visible`
is a common outcome. Fixing the sampling changes that head's class balance, so
M5 gets retrained after this ships — retrained, not redesigned.

---

## Deferred (not current priorities)

### Individual cow ReID + tracking over ReID

**Status:** deferred. The per-camera *temporal* tracker (greedy frame-to-frame
association) was prototyped and **removed** — it doesn't make sense at this
capture cadence.

**Why frame-to-frame tracking fails here.** The Brinno cameras are time-lapse at
**~1 frame/minute**. A walking cow (~1 m/s) can move tens of metres — often
*further than the spacing between cows* — between consecutive frames. Nearest-
neighbour association then swaps identities constantly, especially in the dense
clusters under the panels. So `track_id` from motion association is not a
trustworthy individual, and per-step motion (moving/stationary) is unreliable.

**The right approach: appearance-based ReID.** Instead of associating on
position, associate on *identity*:

1. **Per-cow embedding** — train/fine-tune an appearance descriptor (coat
   pattern / markings; e.g. a metric-learning head on the seg crops, or a
   livestock-ReID backbone). Holstein-type coat patterns are individually
   discriminative.
2. **Assign a global cow id** by matching embeddings across frames *and* across
   cameras (fills the reserved `global_id`), independent of frame rate — two
   sightings 10 minutes apart still match if they're the same animal.
3. **Track over ReID** — a "track" becomes the time-ordered sequence of a
   global id's sightings; dwell, bouts, and movement are then derived from
   *identity-consistent* observations, robust to the 1/min cadence.
4. **Payoffs** unlocked: reliable per-individual dwell / "time under panels",
   herd size by unique-id count (not per-frame detections), cross-camera fusion
   of the same animal, and honest false-positive rejection (a crop that matches
   no consistent identity is likely spurious).

**Prereqs / notes.**
- Reserved DB columns already exist for this: `track_id`, `global_id`, `motion`
  (and the `flags.global_reid` / `flags.within_camera_tracking` gates). *(They
  currently hold stale values from the removed prototype — ignore; a ReID stage
  would overwrite them.)*
- Robustness *today* (without ReID) comes from **temporal aggregation** — the
  count-area windowing already accumulates a trailing window of frames — and from
  the detector **fine-tune** (Stage 1b), not from tracking.
- Higher-fidelity motion would need a higher frame rate than the time-lapse
  provides; ReID sidesteps that by keying on identity, not motion.
- Unblocks the behaviour *dynamics* half of the pose work above.

### Depth & metric world coordinates

**Cow-size depth cue.** Apparent size → rough range along the viewing ray, an
independent depth estimate (`area_px`/bbox already stored, so revisitable on
existing data). Could add a coarse near/far sense to count areas without
reintroducing full metric calibration.

**Metric world coordinates (dropped, revisitable).** The earlier orthophoto
homography / fisheye + sloped-terrain calibration was **removed** in favour of
hand-traced image-space **count areas**, which proved more robust than the
fragile polynomial warp (cubic extrapolation blew up on the wide cameras). If a
future need demands true metric positions (e.g. distances, densities per m²),
this is where a proper multi-view / structure-from-motion calibration would slot
back in — as an *addition*, not a prerequisite for counting.
