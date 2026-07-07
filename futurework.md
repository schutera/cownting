# Future work

## Individual cow ReID + tracking over ReID

**Status:** planned. The per-camera *temporal* tracker (greedy frame-to-frame
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
  the planned detector **fine-tune** (Stage 1b), not from tracking.
- Higher-fidelity motion would need a higher frame rate than the time-lapse
  provides; ReID sidesteps that by keying on identity, not motion.

## Pose estimation for behaviour

**Status:** planned. Add a **keypoint/pose model on top of the instance
segmentation masks** to read cow behaviour, replacing today's coarse posture
proxy (mask elongation via `minAreaRect`, currently disabled — unreliable from a
single oblique view).

**Approach.**
- Run a cow **keypoint estimator** (skeleton: head, neck/withers, spine, hips,
  legs/hooves, tail) on each detected instance — crop by the seg mask/bbox and
  predict keypoints (e.g. an animal-pose backbone / a fine-tuned top-down pose
  head; the mask gives a clean, background-suppressed crop).
- Derive **behaviour** from the pose + its change over time: **grazing**
  (head-down, neck angle low), **standing** vs **lying** (body height / hip-hock
  geometry), **ruminating/resting**, and — with ReID-linked sequences — **gait /
  lameness** and activity bouts.
- Populates the reserved `posture`/behaviour fields properly; gate behind the
  existing `flags.pose_enabled`.

**Notes / prereqs.**
- Pose is far more robust than the current elongation proxy, but the **fisheye +
  oblique view** distorts limb geometry — treat keypoint angles as
  view-dependent, and normalise them per camera (e.g. learn the distortion from
  the pose data itself) rather than assuming a metric camera model.
- Behaviour *dynamics* (grazing bouts, gait) want identity-consistent sequences
  → best paired with the **ReID** work above (behaviour per individual over
  time), though instantaneous per-frame posture works without it.
- Training data: bootstrap keypoints like the Stage 1b mask loop (predict →
  correct in CVAT → fine-tune).

## Solar-panel segmentation (shade & agrivoltaics)

**Status:** partially implemented. Phase 1 — static **per-camera panel ground
footprints** — is **done**: footprints are traced per camera, a cow is
`under_panel` when its ground-contact point falls inside a footprint (image-space,
calibration-free, with a configurable `shade.margin_px` band for
boundary/uncertain), and a `pct_sheltering` KPI + a shelter-vs-time-of-day trend
are surfaced. What remains future is panel/shadow **segmentation** and the
**sun-dependent moving shade map** below.

**Still future — panel/shadow segmentation.**
- Add a **panel** (and optionally **shadow**) class to the segmentation model
  (extend the fine-tuned YOLO-seg, or a dedicated model). Panels are near-static
  in ground position and only **rotate** (trackers), so a panel can be segmented
  once per camera and its tilt tracked over the day. This would **auto-extract the
  footprint corners** now traced by hand.

**Still future — sun-dependent moving shade map.**
- The static footprint is only a **proxy**: the actual shadow moves through the day
  as the sun tracks and the panels rotate, so `under_panel` (footprint occupancy)
  is not the same as *in shade*. Compute **shade** either directly (segment ground
  shadows) or geometrically (panel tilt + sun position from date/time/geo) → a
  **time-varying shade map**.
- Fills the reserved per-detection **`in_shade`** flag: label each localized cow as
  in-shade vs sun (distinct from footprint occupancy), and quantify shade-seeking
  behaviour over the day.
- Feeds the behaviour analysis (pose + shade + position → "resting in shade under
  panel row N at noon").

**Notes.** Gate the moving-shade stage behind the existing `flags`/`shade.enabled`.
Since Cownting is now calibration-free, the shade map lives in **image space** too
— compute shadow footprints per camera (segmented or geometric) and test cow
ground points against them, exactly like the panel footprints.

## Other open items
- **Cow-size depth cue** — apparent size → rough range along the viewing ray, an
  independent depth estimate (`area_px`/bbox already stored, so revisitable on
  existing data). Could add a coarse near/far sense to count areas without
  reintroducing full metric calibration.
- **Metric world coordinates (dropped, revisitable)** — the earlier orthophoto
  homography / fisheye + sloped-terrain calibration was **removed** in favour of
  hand-traced image-space **count areas**, which proved more robust than the
  fragile polynomial warp (cubic extrapolation blew up on the wide cameras). If a
  future need demands true metric positions (e.g. distances, densities per m²),
  this is where a proper multi-view / structure-from-motion calibration would slot
  back in — as an *addition*, not a prerequisite for counting.
