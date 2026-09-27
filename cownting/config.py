"""Typed configuration loaded from YAML (pydantic v2)."""
from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import List, Literal, Optional, Tuple

import yaml
from pydantic import BaseModel, Field


class CameraCfg(BaseModel):
    id: str
    video: str                      # path to a prerecorded video file
    start: Optional[str] = None     # ISO8601 timestamp of frame 0; None -> file mtime


class DatasetCfg(BaseModel):
    """Identifies the data-package (a day's multi-camera shoot) an ingest produces.

    All optional: leaving it blank derives the id/day/label from the earliest
    camera `start`. Override `id` for a same-day re-shoot that must not replace
    the first one; a matching `id` on re-ingest replaces (idempotent)."""
    id: Optional[str] = None        # surrogate key; default = the capture day slug 'YYYY-MM-DD'
    day: Optional[str] = None       # ISO date 'YYYY-MM-DD'; default = earliest cam.start's date
    label: Optional[str] = None     # human label; default = a friendly 'Mon DD, YYYY'


class IngestCfg(BaseModel):
    target_fps: float = 1.0
    time_bin_seconds: float = 2.0
    save_frames: bool = True
    # Time-lapse support. When set, each *sampled* frame advances real capture
    # time by this many seconds (a Brinno grabs 1 frame/minute -> 60.0), so the
    # per-frame timestamp is start + frame_idx * frame_interval_seconds. When
    # None (default), timestamps use real-time playback: start + frame_idx / video_fps.
    frame_interval_seconds: Optional[float] = None


class DetectCfg(BaseModel):
    backend: Literal["yolo", "grounded_sam2"] = "yolo"
    device: str = "auto"
    conf: float = 0.25
    imgsz: int = 1280            # inference resolution; critical for small/distant cows
    # yolo
    yolo_weights: str = "yolo11x-seg.pt"
    class_names: List[str] = Field(default_factory=lambda: ["cow"])
    # grounded_sam2
    dino_model_id: str = "IDEA-Research/grounding-dino-tiny"
    sam2_cfg: str = "configs/sam2.1/sam2.1_hiera_s.yaml"
    sam2_checkpoint: str = "checkpoints/sam2.1_hiera_small.pt"
    prompt: str = "cow."
    box_threshold: float = 0.3
    text_threshold: float = 0.25


class PostureCfg(BaseModel):
    enabled: bool = True
    lying_elongation: float = 1.9


class PoseCfg(BaseModel):
    """Stage-1c pose: real keypoints supersede the mask-elongation posture proxy.

    Zero-shot ViTPose++ (AP-10K animal expert, Apache-2.0) run on YOLO-seg
    instances. Gated by `flags.pose_enabled`; when off, nothing here is touched
    and posture stays the elongation proxy. See docs/roadmap/M1_pose_posture.md."""
    backend: Literal["vitpose"] = "vitpose"
    checkpoint: str = "usyd-community/vitpose-plus-base"  # HF ViTPose++ MoE
    dataset_index: int = 3            # MoE expert head: 3 = AP-10K (17 animal kpts)
    device: str = "auto"
    use_mask: bool = True            # zero non-instance pixels in each crop before pose
    crop_pad: float = 0.15           # bbox padding fraction before cropping
    # --- quality gate -> 'unknown' when the pose is garbage ---
    # ViTPose is confidently wrong on tiny/oblique cows, so confidence alone can't
    # flag bad poses; these geometric gates do. Tune per-camera.
    # Deliberately STRICT: only large, clean, anatomically-plausible poses get a
    # call; everything else is 'unknown'. High precision on what we do label, and
    # a visibly high unknown rate that flags the need for a fine-tuned pose model.
    min_kpt_conf: float = 0.35       # a keypoint counts only above this confidence
    min_confident_kpts: int = 10     # need this many confident joints overall, else 'unknown'
    min_legs_visible: int = 3        # need this many confident hooves (of 4), else 'unknown'
    min_bbox_h_px: float = 200.0     # cows smaller than this -> pose unreliable -> 'unknown'
    max_oob_frac: float = 0.35       # >this share of confident joints outside the (padded) box -> 'unknown'
    # --- class boundaries (normalized by bbox height) ---
    # Calibrated 2026-07-17 on 20 hand-verified gated cows (see the roadmap note).
    # grazing: head dropped toward the feet, (head_y - spine_y)/bbox_h >= this.
    # Clean grazers cluster at +0.18..+0.30; head-up cows are <= 0. 0.15 splits them.
    graze_head_drop: float = 0.15
    # standing vs lying: leg drop (hoof_y - spine_y)/bbox_h >= this -> standing.
    # NOTE: leg_ratio is viewpoint-dominated (frontal/rear standing cows read as
    # low as 0.03, broadside up to 0.52), and the validation footage has NO lying
    # cows, so the lying boundary is uncalibrated. Set to 0.0 so upright cows read
    # 'standing' rather than false-'lying'; reliable lying detection is DEFERRED
    # until lying-labelled data + a fine-tune exist (a real lying cow currently
    # reads 'standing' or 'unknown' — the known gap fine-tuning closes).
    stand_lie_ratio: float = 0.0


class LabelCfg(BaseModel):
    """Stage 1b: bootstrap-label selection + CVAT annotation round-trip."""
    workspace: str = "data/labeling"         # selections + export staging live here
    n_select: int = 150                      # frames to hand-correct
    seed: int = 0                            # deterministic stratified sample
    # Grounded-SAM2 is the bootstrap labeler (see docs/SETUP_WINDOWS.md for install).
    bootstrap_backend: Literal["grounded_sam2", "yolo"] = "grounded_sam2"
    # FiftyOne <-> CVAT round-trip
    cvat_url: str = "http://localhost:8080"
    anno_key: str = "cownting_stage1b"       # FiftyOne annotation-run key (push + pull)
    label_field: str = "cows"                # instance field annotators edit in CVAT


class AnnotationCfg(BaseModel):
    """The in-app Label page (docs/roadmap/M3_labeling.md).

    DISTINCT from LabelCfg above, which is the Stage-1b CVAT round-trip. One word
    apart, two unrelated features — everything here is `annotation`, never `label`.
    """
    targets_per_instance: int = 2   # NOT 1: at 1 the first submission removes the
                                    # instance from every other annotator's queue,
                                    # which defeats measuring annotator variability.
    overlap_fraction: float = 0.20  # share of instances given a deeper target
    overlap_targets: int = 3        # ...so Fleiss' kappa has a real rater count
    skip_retire: int = 3            # distinct skips before an instance stops being served
    batch_size: int = 8
    max_batch_size: int = 50
    crop_pad: float = 0.35          # context padding, fraction of the LONGER bbox side
    crop_max_width: int = 768
    mask_timestamp_banner: bool = True   # the Brinno clock strip leaks time-of-day
    max_banner_fraction: float = 0.6     # above this the crop is refused, not blanked
    max_note_chars: int = 500

    # --- What is worth asking about (futurework: "Stop serving unjudgeable
    # crops"). Measured on 1,164 served instances, 2026-09-10.
    #
    # Only 43.8% of what the queue served produced a SUBSTANTIVE answer. The rest
    # was 563 `low_resolution` flags plus 54 answers of not_visible/cannot_tell —
    # the annotator spent the same twenty seconds and nobody learned anything.
    # P(substantive answer) by the animal's longer side in SOURCE pixels:
    #
    #     <25px 13.6%   25-35 26.3%   35-45 29.8%
    #     45-60 35.1%   60-90 46.4%   90+   76.0%
    #
    # `min_instance_px` is a hard floor: below it a crop essentially cannot carry
    # an answer, and it costs 5% of the pool to stop asking. It is deliberately
    # LOW rather than set where yield peaks — yield rises monotonically with the
    # floor (76% at 90px) and the temptation is to cut there, but a corpus of
    # only near animals teaches the model nothing about distant ones, which are
    # exactly the ones the detector is worst at. That would buy a clean training
    # set by making it unrepresentative of the problem.
    #
    # `clear_instance_px` instead PRIORITISES: at or above it a crop is
    # comfortably legible and is served first. Smaller-but-not-hopeless
    # instances are not banished, they queue behind — and because the pool is
    # ~40x larger than everything labelled to date, "behind" would mean "never".
    # Hence `small_share`: that fraction of sub-clear instances is promoted into
    # the front of the queue anyway, so the corpus keeps learning about distant
    # animals while most annotator time goes where it pays. Membership is
    # derived from the instance key, like the deep-overlap subset, so every
    # annotator agrees on the promoted set with no coordination and two of them
    # can still pair on the same small instance.
    min_instance_px: int = 25       # hard floor; never served below this
    clear_instance_px: int = 60     # at/above -> served first
    small_share: float = 0.15       # of sub-clear instances, promoted anyway

    # ASK ABOUT WHAT THE MODEL CANNOT ANSWER (futurework.md, "the second, larger
    # version"). Once M5 has scored the corpus, every detection carries the
    # model's own confidence, so the queue can stop sampling uniformly and spend
    # annotator time where a label actually teaches something: the crops it
    # abstained on (M5 §9.3 — `attr_behaviour` NULL with a rev) and the ones it
    # called with thin support. `uncertain_below` is that cut, applied to
    # `attr_behaviour_p`, which an abstained row keeps precisely so this decision
    # has a number to read. An UNSCORED row has no probability, reads as 0, and
    # is therefore asked about — which is what makes this a no-op on a corpus the
    # model has never touched: everything is equally worth asking, and the order
    # falls through to the same per-annotator permutation as before.
    #
    # `uniform_share` is the price of admission, and it is not optional. A corpus
    # selected by model uncertainty is no longer a random sample of the herd, so
    # prevalence ("how much of the day do they spend in shade") and the agreement
    # statistics computed over it quietly stop meaning what they say. This
    # fraction is therefore held OUT of the uncertainty rule and kept as a marked
    # uniform sample. Membership is `md5('uni' || instance_key)` — like the
    # deep-overlap and small-crop subsets — so it needs no table, every annotator
    # agrees on it without coordination, and, the part that matters, it stays
    # recomputable from the key forever: an analysis run next year can recover
    # exactly the unbiased slice even though `queue_reason` records only what was
    # true at serve time.
    uncertain_below: float = 0.75   # ask when the model's posture confidence is under this
    uniform_share: float = 0.25     # held out of the rule, as a marked random sample

    # --- What the first round of active learning actually bought (2026-09-25) ---
    # `queue_reason` is recorded on every annotation, so the selector could be
    # audited against the model it fed. 497 labels in, judged against the 5-fold
    # CV of the model trained on them:
    #
    #   abstained      yields 22% not_visible vs uniform's 6% -- 3.7x, and
    #                  not_visible is the ONLY class that clearly improved
    #                  (+0.047, 3.6x the fold spread). The rule works.
    #   BUT it spends 53% of its budget on `feeding`, already the most abundant
    #                  class, and finds no more `standing` than random sampling
    #                  does (20% vs 20%) -- and `standing` did not improve at all
    #                  (-0.001). 28 of its 39 skips are `low_resolution`.
    #
    # The cause is that `uncertain_below` reads MAX PROBABILITY, which cannot
    # tell "torn between standing and feeding" (informative -- it is the dominant
    # confusion, 177 of the behaviour errors) from "flat over four classes on an
    # unreadable smudge" (a skip). The three knobs below add the signals the max
    # cannot express.

    # A crop whose cow/not-cow probability sits near the gate's own threshold is
    # the one that would move the gate. Nothing looked at `attr_cow_p` before, and
    # the gate's recall duly sat still (0.885 -> 0.895) while 44 not-cows kept
    # passing as cows. Half-width of the band around `instance_attrs.cow_threshold`.
    gate_band: float = 0.15

    # Classes the queue should go looking for, by the model's OWN prediction.
    # Not "rare in the corpus" -- rare in the TRAINING SET and weak in validation:
    # standing (n=535, F1 0.735) against feeding's n=969. Empty disables the rule.
    #
    # `behaviour.not_visible` is deliberately NOT here, though it is the weakest
    # class of all (F1 0.473, n=256). It cannot be: `infer.score_detections`
    # picks the written class with `max(subst_i, ...)` over the SUBSTANTIVE
    # classes only, and writes NULL when it would have chosen not_visible -- so
    # `attr_behaviour` is never that value (production: 0 rows of 46,486) and
    # listing it here would match nothing. Those crops are precisely the
    # abstentions, which the weak-posture selector already catches.
    seek_classes: list[str] = ["behaviour.standing"]

    # Serve cows standing under a solar panel before anything else of equal
    # legibility. This is the scientifically distinctive subset of the corpus --
    # agrivoltaic shade use is the one question this deployment can answer that a
    # barn dataset cannot -- and it is tiny: `under_panel` is TRUE on a few
    # hundred of ~46k detections, because panel polygons have been drawn for one
    # day and three cameras. Promoting them costs the rest of the queue almost
    # nothing and makes every panel polygon that lands afterwards immediately
    # productive.
    #
    # It rides ABOVE `worth_asking` in the sort but BELOW `clear`: an illegible
    # crop under a panel is still illegible, and the legibility floor applies to
    # this selector exactly as it does to the other three.
    under_panel_first: bool = True

    # Of the crops matching `seek_classes`, the share actually promoted.
    # Confidently-predicted crops of a common posture are an enormous pool --
    # far larger than the abstentions or the gate-boundary cases -- so an
    # uncapped rule would crowd the front tier with the one signal that carries
    # no uncertainty at all. Membership is a hash of the instance key, like
    # every other share here, so it is stable and needs no table.
    seek_share: float = 0.25

    # Abstention promotes a crop only if it is at least this legible. The model
    # abstains for two different reasons -- "this posture is genuinely ambiguous"
    # (worth asking) and "this is 30 pixels of brown" (worth nothing, and the
    # annotator skips it). Only the crop size separates them before serving.
    # Defaults to clear_instance_px when None.
    abstain_min_px: int | None = None


class BackupCfg(BaseModel):
    """Weekly zip of the label store, posted to a Discord webhook.

    Off by default: it is opt-in per deployment, and the webhook URL comes from
    COWNTING_DISCORD_WEBHOOK in the environment, never from this YAML."""
    enabled: bool = False
    every_days: int = 7
    keep: int = 8
    # Discord's attachment cap is 8 MiB (8,388,608) on an unboosted guild and
    # 10 MiB on some endpoints. Sit below the FLOOR, not the ceiling: the old
    # 9,500,000 had headroom against 10 MiB only, so a zip in the 8-9.5 MB
    # window passed our own ladder, got POSTed, and came back 413.
    max_upload_bytes: int = 8_000_000

    # --- Label-count milestones -------------------------------------------
    # A ping every time the labeled-instance count crosses a multiple of
    # `milestone_step`: 500, then 1000, then 1500. Deliberately NOT gated on
    # `enabled` above — that switch governs whether annotator names and timings
    # leave the box in a zip, and a milestone is one integer. A deployment that
    # wants the nudge without shipping the corpus off-box gets it.
    #
    # On by default for the same reason: with no webhook configured it prints one
    # line and posts nothing, which is a supported state rather than a failure.
    milestones: bool = True
    milestone_step: int = 500


class InstanceAttrsCfg(BaseModel):
    """M5: the small classifier over instance crops
    (docs/roadmap/M5_instance_attrs.md).

    Distinct from FinetuneCfg below, which fine-tunes the SEGMENTER on corrected
    masks via CVAT. This model consumes the Label page's class answers instead,
    runs on the crop rather than the frame, and predicts attributes rather than
    geometry.
    """
    # Crop geometry is deliberately NOT configured here: it comes from
    # `annotation` (crop_pad, crop_max_width, mask_timestamp_banner) so training
    # crops are necessarily the same pixels the annotator judged. A second,
    # drifting copy of those numbers is the exact bug that arrangement forbids.
    dataset_dir: str = "data/instance_attrs/dataset"
    weights: str = "data/models/instance_attrs.pt"

    # Relabelling at DATASET-BUILD time — the store itself is never rewritten.
    # The label store is append-only on purpose (annotator variability is the
    # product), so a class merge is expressed here, as a mapping applied when
    # the training set is built, and the original answers stay in the store and
    # in every backup. Two forms, applied in this order:
    #   merge_classes  class_key -> class_key, for every answer of that class
    #   relabels       a CSV (instance_key, group_key, class_key) of per-instance
    #                  overrides — used for the 2026-09-17 head_probing merge,
    #                  where each instance was assigned to feeding or standing
    #                  by the two shipped checkpoints' vote; the audit trail
    #                  (both models' probabilities, the annotator) is beside it.
    # Either may name a class that is archived in the taxonomy; the TARGET must
    # be active or the answer is dropped, the same as any other archived class.
    merge_classes: dict = Field(default_factory=dict)
    relabels: str | None = None
    # Classes to leave OUT of the model entirely: removed from the head's
    # vocabulary, their answers dropped (the instance still trains the other
    # heads). The taxonomy is untouched — annotators keep the option. Exists to
    # test "do not train not_visible at all, call it by confidence instead"
    # against "train it and threshold its probability", on the same OOF data.
    exclude_classes: list = Field(default_factory=list)

    # Split. The unit is a contiguous run of frames from one camera on one day —
    # at ingest.frame_interval_seconds = 60 those are minutes, so 30 is a
    # half-hour block. Instances from adjacent frames are near-duplicates, so a
    # per-instance split would score memorisation rather than generalisation.
    block_frames: int = 30
    val_fraction: float = 0.2
    split_seed: int = 1337

    # Training. `device: auto` resolves cuda > mps > cpu through the same
    # `resolve_device` the detector uses, so the same config trains on the GPU
    # box and scores on this CPU-only one.
    device: str = "auto"
    img_size: int = 224            # what the ImageNet backbone was trained at
    epochs: int = 50
    batch_size: int = 32
    lr: float = 3e-4
    weight_decay: float = 1e-4
    # Stop when mean macro-F1 across heads has not improved for this many
    # epochs. Macro rather than accuracy because the classes that matter here
    # are the rare ones — a gate that never predicts `not_cow` scores 89%
    # accuracy and is useless.
    patience: int = 10
    num_workers: int = 0           # a few thousand crops; a worker pool costs more than it saves
    seed: int = 1337

    # --- the recipe. Every default below was chosen by `cownting attrs cv`,
    # 5-fold grouped CV over the 2,381-instance corpus of 2026-09-16, and that
    # harness — not a single train/val run — is what should decide any change:
    # one split's fold-to-fold spread is ±0.014 pooled and its rerun noise
    # ±0.005, so a 0.01 improvement on one split is indistinguishable from
    # nothing. What the CV said, pooled mean macro-F1 with the baseline at
    # 0.773–0.778:
    #
    #   tempered class weights (power 0.5)          0.774   no effect
    #   + smoothing/EMA/warmup/head-LR              0.778   no effect on the mean;
    #                                                       gate fold spread ±0.015 -> ±0.006
    #   + rotate 10° / erase 0.25 / blur 0.25       0.788   +0.010, all three heads up,
    #                                                       gate F1 on 25–60px 0.77 -> 0.81
    #   size reweighting toward production's mix   0.776   HURT: gate 0.948 -> 0.937,
    #                                                       25–60px gate 0.77 -> 0.72
    #   longer schedule (50/10 vs 30/6)             0.786   neutral
    #   trunk: mobilenet_v3_large / efficientnet_b0 / resnet18 on the baseline
    #                                               0.780 / 0.792 / 0.792
    #                                                       behaviour 0.54 -> 0.59
    #
    # Measured on the serving box's two CPU threads, ms per crop:
    #   mobilenet_v3_small 4.4 · mobilenet_v3_large 16.4 · resnet18 36.5 ·
    #   efficientnet_b0 43.5 (depthwise convs are memory-bound on CPU; the
    #   GFLOPs ordering is not the wall-clock ordering).
    backbone: str = "resnet18"   # see instance_attrs.model.BACKBONES
    # Inverse-frequency class weights raised to this power. No measurable effect
    # either way; kept at 0.5 so forty noisy head_probing examples carry ~4x a
    # feeding one rather than ~15x, which is the less surprising failure mode.
    weight_power: float = 0.5
    label_smoothing: float = 0.1
    # Exponential moving average of the weights, evaluated instead of the raw
    # ones, with BatchNorm buffers averaged too. Did not move the mean; halved
    # the gate's fold-to-fold spread.
    ema_decay: float = 0.998
    warmup_epochs: int = 2
    head_lr_mult: float = 5.0
    # The one recipe change that cleared the noise bar. Geometry and resolution
    # only — NOT brightness/contrast, because shaded vs direct_sun IS a
    # brightness judgement. Blur is the deliberate one: the corpus spans 25–400px
    # crops all resized to one size, so a sharp cow and a blurred one are the
    # same class and the model is taught so.
    aug_rotate_deg: float = 10.0
    aug_erase_p: float = 0.25
    aug_blur_p: float = 0.25
    # Average the prediction over the crop and its mirror at eval/inference.
    tta_hflip: bool = False
    # Sample training crops so their SIZE distribution matches the pool the
    # model will score, not the pool annotators labelled. The two differ a lot:
    # a crop under 60px was usually flagged rather than answered, so the
    # labelled corpus is ~18% 25–60px where production is ~48%. Left alone, the
    # model learns least about the band it will see most — and the retrain on
    # the post-floor corpus measurably lost gate accuracy there (not_cow F1
    # 0.88 -> 0.75 on 25–60px) while its headline stayed at 0.97.
    size_reweight: bool = False
    # Production's share per band on the animal's longer side, measured over the
    # 46,486-detection pool on 2026-09-16. Keys are strata.SIZE_BANDS names.
    size_target_shares: dict = Field(default_factory=lambda: {
        "<25": 0.05, "25-60": 0.48, "60-90": 0.24, "90+": 0.23})
    # Grouped k-fold cross-validation over blocks, for `cownting attrs cv`.
    cv_folds: int = 5

    # Score newly-uploaded days automatically, as a pipeline stage after
    # localize. OFF by default, and separately from the weights file existing: a
    # checkpoint sitting on the box is evidence somebody was EVALUATING a model,
    # not consent for production footage to be scored by it. Presence-as-switch
    # means copying a file silently turns a pipeline stage on, which is not a
    # decision anybody made out loud.
    #
    # Gates the AUTOMATIC path only. `POST /api/attrs/score` and `cownting attrs
    # score` are explicit operator actions and work regardless — the same split
    # as `backup.enabled` (the weekly ticker) versus `POST
    # /api/labels/backup/run` (a person asking for it now). That is what lets you
    # score one day, look at the result, and only then decide whether arriving
    # footage should be scored.
    score_on_ingest: bool = False

    # Serving. Below this P(cow) a detection is flagged not-a-cow. Deliberately
    # high: wrongly dropping a real animal corrupts the count, which is the
    # product, while keeping a false positive merely fails to improve it.
    cow_threshold: float = 0.5
    # --- behaviour abstention ---
    # The behaviour head's not_visible class is the annotator's epistemic state,
    # not a look ("too little of the body is visible to read the posture"), and
    # it trains to ~40% recall / ~50% precision no matter what — a partially
    # occluded cow looks like a cow. So the dashboard does not try to DETECT
    # "not visible"; it declines to SHOW a posture it cannot back. Scoring writes
    # attr_behaviour = NULL (attr_model_rev still set, so it reads as 'unsure',
    # not 'unscored') when
    #     p(not_visible) >= abstain_nv_p   or   max(feeding, lying, standing) < abstain_min_conf
    # Fit on out-of-fold probabilities of all 1,943 behaviour-labelled instances
    # (2026-09-17, resnet18 recipe; `attrs cv` keeps the softmax). Coverage is
    # the share of crops that get a posture; precision is how often the shown
    # posture matches the annotator, with the annotator's own not_visible
    # counted as "should have abstained":
    #
    #     rule                     coverage   precision   nv caught
    #     argmax (no abstention)     92.0%      83.3%        —
    #     conf < 0.5                 81.8%      87.7%       61.9%
    #     conf < 0.6   <- default    73.1%      90.7%       74.8%
    #     conf < 0.7                 64.0%      93.0%       83.2%
    #     conf < 0.8                 50.8%      95.4%       92.6%
    #
    # Max-confidence is the stronger signal; a p(not_visible) threshold adds
    # nothing on top (high p(nv) already forces low confidence), so abstain_nv_p
    # is set where it only fires when not_visible is the argmax. The class is
    # still trained: it is what keeps the confidence honest on occluded crops.
    # The raw probability is kept in attr_behaviour_p either way.
    abstain_nv_p: float = 0.50
    abstain_min_conf: float = 0.60

    # Master switch for the gate's EFFECT. Scoring always writes its verdict;
    # this decides whether the rest of the app filters on it. Off by default
    # because turning it on moves every historical count in the dashboard.
    gate_enabled: bool = False


class FinetuneCfg(BaseModel):
    """Stage 1b: YOLO11-seg fine-tune on the corrected masks."""
    dataset_dir: str = "data/finetune/dataset"    # generated YOLO-seg dataset root
    runs_dir: str = "runs/finetune"               # ultralytics output root
    run_name: str = "cownting"
    base_weights: str = "yolo11m-seg.pt"          # start point (already on disk)
    weights_out: str = "data/finetune/cownting-seg.pt"  # promoted best.pt
    epochs: int = 100
    imgsz: int = 1280                             # match inference; critical for small cows
    batch: int = 4                                # small; safe on 12 GB VRAM / MPS at 1280
    freeze: int = 0                               # backbone layers to freeze (0 = full FT)
    patience: int = 20                            # early-stop patience
    val_fraction: float = 0.2
    split_by_time_block: bool = True              # avoid near-duplicate minutes leaking train->val


class FlagsCfg(BaseModel):
    within_camera_tracking: bool = False
    global_reid: bool = False
    pose_enabled: bool = False


class AuthCfg(BaseModel):
    """Login gate for the served dashboard. Only `enabled` lives here — the
    session secret and the bootstrap admin credentials are read from the
    environment (COWNTING_SECRET, COWNTING_ADMIN_USER/PASSWORD) so no secret is
    ever committed to the YAML. Disable only for tests / trusted-LAN demos."""
    enabled: bool = True
    session_max_age: int = 60 * 60 * 12   # cookie lifetime in seconds (12h)
    https_only: bool = False              # set True when served behind TLS


class PathsCfg(BaseModel):
    artifacts_dir: str = "data/artifacts"
    db_path: str = "data/cownting.duckdb"
    archive_db_path: str = "data/cownting_archive.duckdb"  # deleted days move here (not destroyed)
    count_areas: str = "data/count_areas.json"   # LEGACY flat fallback: used only when dataset_id is None; per-dataset areas live under data/areas/<dataset_id>/
    panel_areas: str = "data/panel_areas.json"   # LEGACY flat fallback: used only when dataset_id is None; per-dataset areas live under data/areas/<dataset_id>/
    orthophoto: Optional[str] = None
    # Annotations live in their OWN DuckDB file: purge_dataset() deletes detections
    # on re-ingest and archive_dataset() moves them out, so labels stored in the main
    # DB would be destroyed by routine data management. Nothing in the ingest path
    # ever opens this file.
    labels_db_path: str = "data/labels.duckdb"
    backups_dir: str = "data/backups"            # weekly label zips in <backups_dir>/labels/


class Config(BaseModel):
    project: str = "cownting"
    cameras: List[CameraCfg]
    dataset: DatasetCfg = Field(default_factory=DatasetCfg)
    ingest: IngestCfg = Field(default_factory=IngestCfg)
    detect: DetectCfg = Field(default_factory=DetectCfg)
    posture: PostureCfg = Field(default_factory=PostureCfg)
    pose: PoseCfg = Field(default_factory=PoseCfg)
    label: LabelCfg = Field(default_factory=LabelCfg)
    annotation: AnnotationCfg = Field(default_factory=AnnotationCfg)
    backup: BackupCfg = Field(default_factory=BackupCfg)
    instance_attrs: InstanceAttrsCfg = Field(default_factory=InstanceAttrsCfg)
    finetune: FinetuneCfg = Field(default_factory=FinetuneCfg)
    flags: FlagsCfg = Field(default_factory=FlagsCfg)
    paths: PathsCfg = Field(default_factory=PathsCfg)
    auth: AuthCfg = Field(default_factory=AuthCfg)

    @classmethod
    def load(cls, path: str | Path) -> "Config":
        # encoding="utf-8" explicitly: the shipped example contains ±, – and " in
        # its comments, and bare open() uses the platform default — cp1252 on a
        # German Windows box, which mojibakes those and would raise outright on a
        # non-ASCII VALUE. The container is safe (LANG=C.UTF-8); a dev box is not.
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
        if data is None:
            raise ValueError(f"{path}: the config file is empty")
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a YAML mapping, got {type(data).__name__}")
        # BEFORE constructing, so an unknown key is still reported when the file
        # also fails validation for some unrelated reason — which is exactly when
        # someone is staring at the config wondering what is wrong with it.
        _warn_unknown_keys(path, data, cls)
        return cls(**data)


def _warn_unknown_keys(path, data: dict, model, prefix: str = "") -> None:
    """Print a warning for every YAML key that is not a field on any model.

    Pydantic's default is to IGNORE unknown keys, so a typo is silent: the
    shipped example had `paths.panels` (the real field is `panel_areas`) and a
    whole inert `shade:` block, and an operator who tuned either got the default
    with no indication whatsoever. That is the worst kind of config bug — the
    system does something other than what its own config file says.

    A warning and not `extra="forbid"`, deliberately. Refusing to construct would
    turn a stale key in a long-lived deployment's config into a container that
    will not boot, discovered during a deploy. The point is to make the drift
    visible, not to make it fatal. The line is tagged so `alerts/watch.py`
    forwards it — see tests/test_alerts.py.
    """
    from pydantic import BaseModel as _BM
    fields = getattr(model, "model_fields", {})
    for key, value in data.items():
        if key not in fields:
            print(f"[cownting.alert] CONFIG unknown key '{prefix}{key}' in {path} "
                  f"— ignored, so its value has no effect", flush=True)
            continue
        ann = fields[key].annotation
        if isinstance(value, dict) and isinstance(ann, type) and issubclass(ann, _BM):
            _warn_unknown_keys(path, value, ann, prefix=f"{prefix}{key}.")


def resolve_dataset(config: "Config") -> Tuple[str, Optional[date], str]:
    """Resolve (dataset_id, day, label) for an ingest run from the config.

    Precedence: explicit `dataset.*` overrides, else derive from the earliest
    camera `start`. `dataset_id` defaults to the ISO day slug so re-ingesting the
    same day replaces rather than duplicates."""
    ds = config.dataset
    day: Optional[date] = None
    if ds.day:
        day = date.fromisoformat(ds.day)
    else:
        starts = [c.start for c in config.cameras if c.start]
        if starts:
            day = min(datetime.fromisoformat(s) for s in starts).date()
    if ds.id:
        dataset_id = ds.id
    elif day is not None:
        dataset_id = day.isoformat()
    else:
        # No dataset.id and no derivable day (no dataset.day, no cameras[].start).
        # The old fallback to a constant "dataset" id silently made every such
        # ingest purge + overwrite the previous one, because resolve_dataset can't
        # see the in-file capture time that index_video actually reads. Fail loud.
        raise ValueError(
            "cannot resolve a dataset id: set dataset.id or dataset.day in the "
            "config, or a cameras[].start — otherwise a re-ingest would overwrite "
            "the previous run under the shared id 'dataset'."
        )
    label = ds.label or (day.strftime("%b %d, %Y") if day else dataset_id)
    return dataset_id, day, label


def resolve_device(device: str) -> str:
    """Resolve 'auto' to the best available backend: cuda > mps > cpu."""
    if device != "auto":
        return device
    try:
        import torch

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:
        pass
    return "cpu"
