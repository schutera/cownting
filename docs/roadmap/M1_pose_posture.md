# Workstream M1 — Pose → Posture (real keypoints supersede mask-elongation)

> Standalone design for the M1 workstream summarized in `ROADMAP.md`. **This doc
> updates two ROADMAP decisions** given the "commercial / license-clean" product
> requirement: the pose **backend** (was SuperAnimal→YOLO11-pose, both
> license-problematic → now MMPose AP-10K / ViTPose++, Apache-2.0) and it pins
> **where** pose runs (inline in the GPU-side `segment` stage, reusing YOLO-seg
> instances). Everything else in the ROADMAP M1 summary stands.

## 1. Goal & what it supersedes

Fill `detections.posture` with a **pose-derived** standing / lying / unknown
label instead of the current mask-silhouette elongation proxy. The proxy
(`posture_from_mask`, `detect/geometry.py:48-52`) thresholds the min-area-rect
aspect ratio at `lying_elongation=1.9` (`config.py:59`). On this oblique fisheye
footage it is systematically fooled: a **standing** cow viewed side-on is
foreshortened into an elongated silhouette and mislabels as **lying**. Real leg
geometry (hip/hock/hoof vertical extent) disambiguates the two directly — which
is precisely what `detect/geometry.py:1-6` already flags as "meant to be replaced
by the optional pose stage later."

**Contract-stable swap.** `posture` is free-text `VARCHAR` (`db.py:79`) and every
consumer uses string equality (`db.py:336-337,387-388,449-450,491-492`;
`features.py:26-28`). Shipping binary `standing`/`lying` from pose therefore needs
**zero** schema / API / frontend change. `grazing` is an opt-in extended
vocabulary landed later with all consumer diffs; `lame` is deferred (needs
temporal gait + within-camera tracking).

## 2. Model choice — MMPose AP-10K (Apache-2.0)

There is **no plug-and-play pretrained cattle-pose weight file**. The selection
axis for a *commercial, distributed* product is dominated by license:

| Candidate | Kpts | Cattle pretrained? | License | Verdict |
|---|---|---|---|---|
| **MMPose AP-10K** (ViTPose++ / HRNet) | 17 (incl. Bovidae) | Partial, fine-tunes clean | **Apache-2.0** (data CC-BY-4.0) | **CHOSEN** |
| DLC SuperAnimal-Quadruped | 39 | Yes (side-view, zero-shot) | Weights **non-commercial** | Rejected: can't ship |
| YOLO11-pose fine-tuned | 17 (human base) | No (fine-tune only) | **AGPL-3.0** / paid | Rejected: copyleft/network |
| SLEAP | user-def | No | Clear-BSD non-commercial | Rejected: can't ship, no zero-shot |

**Decision:** **MMPose AP-10K**, backbone **ViTPose++** (SOTA on AP-10K/APT-36K)
or **HRNet-w32** (lighter). AP-10K's 17-keypoint schema is leg-rich and its
training families **include Bovidae (cattle)**, so it runs near-zero-shot and
fine-tunes cleanly on our own frames if the oblique view needs it. Prefer the
**HuggingFace `transformers` ViTPose** route to avoid the full `mmcv` dependency
tree if integration weight matters; fall back to native MMPose if a needed
checkpoint isn't mirrored there.

**Sources:** AP-10K `arxiv.org/abs/2108.12617` (Apache-2.0 code, CC-BY-4.0 data);
ViTPose++ `arxiv.org/abs/2212.04246`; MMPose model zoo (2D animal keypoint).

**Bootstrap ladder (mirrors Grounded-SAM2 → YOLO-seg):** if AP-10K zero-shot is
weak on our viewpoint, use it (or SuperAnimal, *internally only, never shipped*)
as a **labeler** to bootstrap annotations, then fine-tune the AP-10K/ViTPose head
on our frames for production. Keep `flags.pose_enabled=false` until weights clear
a quality gate.

## 3. Where it runs — inline in `segment`, reusing YOLO-seg instances

AP-10K/ViTPose is **top-down**: `detector → per-animal bbox → crop → keypoints`.
**YOLO-seg is already that detector**, so pose skips detection entirely and
consumes what `segment` already produced. This answers the "can we reuse the
YOLO-seg instances?" question: **yes — at both the bbox and the mask level.**

**Why inline, not a separate stage.** F2's hosting split keeps `localize`
**model-free on the serve box** (`pipeline.localize` is invoked from `/api/*`). A
pose model must run on the **GPU box**, which is the `segment` stage. So pose
lands **inside the segment path**, batched per frame, before rows are written at
`pipeline.py:83-92`. This also wins the mask: the in-memory `Instance` still
carries `bbox`, `mask`, and the crop (`detect/base.py:12-17`), so we can
**zero out non-instance pixels with the YOLO-seg mask before pose** — essential
under cow-on-cow occlusion, where a loose bbox would otherwise latch onto a
neighbor. (A serve-side, DB-only stage would have only `bbox` — mask is not
persisted, `base.py:17` — so it would lose exactly this defense. Inline avoids
that.)

Flow: `segment()` runs YOLO-seg → for each `Instance`, mask-zero the crop → batch
all crops for the frame through the pose model → `posture_from_keypoints` →
`inst.posture`. The rest of `segment` (row build, overlay, `mark_processed`) is
unchanged. `localize` and the serve box never load a pose model; the bundle
carries the same `posture` string.

## 4. Keypoint vocabulary

Canonical **~8-joint** internal vocabulary (a stable subset of AP-10K's 17,
chosen for the standing/lying signal — back line + one representative leg chain
per side):

```
nose, withers(shoulder), hip(root_of_tail),
front_hoof_L, front_hoof_R, hind_hoof_L, hind_hoof_R, hock
```

A backend adapter maps the model's native indices → this vocabulary, so swapping
ViTPose↔HRNet↔a fine-tuned head never touches the classifier. Lives in the new
`cownting/detect/pose.py` alongside the `PoseEstimator` protocol.

## 5. `posture_from_keypoints` — the derivation

New function in `detect/geometry.py`, sibling to `posture_from_mask`. **Two
robust, scale-invariant, calibration-free signals** (no world coordinates — pure
image geometry, consistent with the whole count-areas pivot):

1. **Vertical leg extension ratio**
   `h_ratio = (hoof_y − back_y) / body_length`
   where `back_y` = median y of withers+hip, `hoof_y` = median y of visible
   hooves, `body_length` = ‖withers − hip‖ (the scale normalizer). Standing cows
   extend hooves well below the back line → large `h_ratio`; lying cows collapse
   hooves toward back level → small `h_ratio`.

2. **Hip/hock height above hoof** as a cross-check for foreshortened poses.

```python
def posture_from_pose(kpts, conf, cfg) -> Optional[str]:
    # cfg: min_kpt_conf, min_legs_visible, stand_ratio, lie_ratio
    legs = [i for i in HOOF_IDX if conf[i] >= cfg.min_kpt_conf]
    if len(legs) < cfg.min_legs_visible or back_conf < cfg.min_kpt_conf:
        return None                      # -> unknown (occluded / too few joints)
    h = (median_hoof_y - back_y) / body_length
    if h >= cfg.stand_ratio: return "standing"
    if h <= cfg.lie_ratio:   return "lying"
    return None                          # ambiguous band -> fallback (see §6)
```

**unknown** is a first-class outcome: too few high-confidence leg joints, missing
back line, or an h_ratio in the ambiguous band all yield `None`. This is the
right behavior on distant/occluded cows and keeps precision honest.

## 6. Elongation stays as a confidence-gated fallback

Do **not** hard-remove `posture_from_mask`. When pose returns `None`
(occlusion/ambiguity) fall back to the elongation proxy rather than dropping the
cow to `unknown` — otherwise the dashboard regresses on exactly the hard
oblique/occluded cases pose can't resolve. Order per instance:

```
posture = posture_from_pose(...) or posture_from_mask(...)   # gated on flags.pose_enabled
```

With `flags.pose_enabled=false` (default, `config.py:94`) the pose call is skipped
entirely and today's elongation path is byte-for-byte unchanged.

## 7. Data model — no schema change

- `Instance` gains `keypoints: Optional[np.ndarray] = None` (and confidences),
  mirroring the `mask` precedent (`base.py:17`) — **in-memory only, not
  persisted**.
- `posture` VARCHAR is reused as-is; the pose-derived string overwrites the
  proxy value. **Zero DDL, zero `DET_COLS` change** (`db.py:19-28`).
- **Persist keypoints only if QA later needs them:** add a nullable
  `detections.keypoints` JSON column via the existing
  `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` pattern (`db.py:89-95`). Deferred —
  not needed for the standing/lying label.
- Dashboard already pivots `posture` (`features.py:26-28`); no frontend work for
  the label. Keypoint overlay in `detect/overlay.py` is an optional extra.

## 8. Config & wiring

- New `PoseCfg` sub-model (backend `vitpose`/`hrnet`, weights path, device,
  `min_kpt_conf`, `min_legs_visible`, `stand_ratio`, `lie_ratio`), added to
  `Config` with a `default_factory`, following the `PostureCfg`/`DetectCfg`
  pattern (`config.py:40-59,116-127`).
- Gate: the **already-declared** `flags.pose_enabled` (`config.py:94`), currently
  read nowhere.
- **Reconciliation (from ROADMAP):** the `build_segmenter` signature grows a pose
  arg — `build_segmenter(detect, posture, pose, flags)` (`detect/__init__.py:8`)
  — and this change **must merge with F2/DU3's `process_upload` segment path** so
  the GPU-side job constructs the pose estimator too.

## 9. Temporal smoothing (follow-on, needs tracking)

Time-lapse sequences let a short **majority vote** over consecutive frames beat
single-frame posture by up to ~33% under occlusion (lameness-BiLSTM / cow-skeleton
literature). Blocked on within-camera tracking (`flags.within_camera_tracking`,
also scaffolded off). Land single-frame first; add voting when tracking exists.

## 10. Validation plan

1. Enable `pose_enabled` on a held-out day; render keypoint overlays for eyeball
   QA on standing vs lying cases the elongation proxy currently flips.
2. Hand-label posture on ~100 crops spanning near/far, open/occluded, and compare
   pose vs elongation vs ground truth (confusion matrix). Gate: pose must beat
   elongation on the foreshortened-standing class without regressing `unknown`
   rate materially.
3. Sanity-check the `standing/lying/unknown` split against the current
   81.6/18.4% baseline; a large swing flags a threshold or mask-zeroing bug.

## 10b. Threshold calibration (2026-07-17, 20 gated cows)

Ran the strict-gated pose over a sample and hand-verified the 20 cows that
passed, correlating each with its `leg_ratio = (hoof_y−spine_y)/bbox_h` and
`head_drop = (head_y−spine_y)/bbox_h`. Findings that set the shipped defaults:

- **No lying cows in the footage** (active daytime pasture) → the lying boundary
  is uncalibratable here. `stand_lie_ratio` set to **0.0** so upright cows read
  `standing` rather than false-`lying`. Reliable lying detection is **deferred**
  to lying-labelled data + a fine-tune; a real lying cow currently reads
  `standing`/`unknown`. This is the concrete motivation for the labeling round.
- **`leg_ratio` is viewpoint-dominated, not posture-dominated**: standing cows
  range 0.03 (frontal/rear, foreshortened) to 0.52 (broadside). It cannot
  separate standing from lying on its own — the old 0.40 default was mislabeling
  standing cows as lying. A pose fine-tune (accurate leg keypoints) or an
  aspect-ratio guard is needed before lying is trustworthy.
- **`head_drop` cleanly separates grazing**: clean grazers cluster at
  +0.18…+0.30, head-up cows are ≤ 0. `graze_head_drop` lowered 0.30 → **0.15**.

## 11. Risks / open questions

- **Zero-shot AP-10K quality on fisheye/oblique** unknown → keep the fine-tune
  ladder (§2) ready; keep `pose_enabled=false` until the quality gate passes.
- **Threshold calibration** (`stand_ratio`/`lie_ratio`) is per-camera-height
  sensitive; may need per-camera values like count areas already are.
- **Batching cost** — top-down runs one forward pass per cow; a busy frame with
  many cows is the throughput pole. Batch all crops per frame; measure on the GPU
  box before enabling in the auto-process path.
- **ViTPose via `transformers` vs full MMPose** — confirm the exact AP-10K
  checkpoint is mirrored on HF before committing to the light dependency path.

## 12. Task checklist

- [ ] `cownting/detect/pose.py`: `PoseEstimator` protocol + `build_pose_estimator`, ViTPose/HRNet backend, native→canonical adapter, mask-zeroed batched crop inference.
- [ ] `PoseCfg` in `config.py` + `Config` field; wire `flags.pose_enabled`.
- [ ] `Instance.keypoints` field in `detect/base.py`.
- [ ] `posture_from_pose` in `detect/geometry.py`; fallback chaining in the segment path.
- [ ] `build_segmenter` signature + call sites (`detect/__init__.py`, `pipeline.py:67`); reconcile with F2/DU3 `process_upload`.
- [ ] Optional keypoint overlay in `detect/overlay.py`.
- [ ] Validation notebook/script (§10); acquire & quality-gate weights before flipping `pose_enabled`.
- [ ] (Deferred) `detections.keypoints` JSON column; (deferred) temporal voting; (deferred) `grazing` vocab.
