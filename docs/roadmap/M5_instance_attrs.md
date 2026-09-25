# Workstream M5 — Instance attribute model

One small classifier over the **instance crop**, trained on the annotations M3
collected, doing three jobs the pipeline cannot do today:

```
  crop ──▶ backbone ──┬──▶ is this a cow?      ──▶ gate: drop false detections
                      ├──▶ sun exposure        ──▶ fills in_shade (0/46,486 today)
                      └──▶ behaviour           ──▶ beyond the elongation proxy
```

It is deliberately *small*. The training set is 543 labeled instances; the
serving box is CPU-only with 2 torch threads. Anything larger than a
MobileNet-class backbone would be slower to run than the segmentation it is
filtering, and would overfit 543 samples harder than it already will.

This is the model track's third entry, after M1 (pose → posture) and M2 (shadow
segmentation, never designed). It **supersedes M2's purpose**: `in_shade` was
M2's output column and has sat at zero since it was declared, while the
`shade` feature in `features.py` carries a `RESERVED — unavailable until
in_shade is populated` comment. A learned sun head fills that column from labels
we already have, without the shadow-geometry work M2 never started.

---

## 1. Why a crop classifier and not more heuristics

Each head replaces something that is currently either absent or a proxy:

| Head | Today | After |
|---|---|---|
| cow / not-cow | nothing — every YOLO instance is counted | 76 human `false_positive` verdicts become a learned gate |
| sun exposure | `in_shade`: **0 of 46,486** rows populated | learned from 316 shaded/direct_sun answers |
| behaviour | `posture`: 100% populated, but mask elongation via `minAreaRect` — only ever emits `standing` or `lying` | feeding / lying / standing / head probing / not visible |

The elongation proxy cannot express *feeding* at all: a cow with its muzzle down
is geometrically a standing cow. 161 `feeding` answers exist and nothing
downstream can see them.

## 2. The corpus this trains on (2026-09-09)

543 instances carry class answers; 624 carry an outline verdict; 539 carry both.
**A `false_positive` never carries class answers** (verified: the overlap is
exactly 0) — there is nothing to ask about a thing that is not a cow. That makes
the two tasks cleanly separable rather than a hierarchy to model.

```
cow gate        548 cow (ok + polygon)      76 not-a-cow (false_positive)
sun exposure    direct_sun 194   shaded 122   not_visible 136    [cannot_tell 92 excluded]
behaviour       feeding 161  lying 151  standing 109  not_visible 64  head_probing 12
                                                                 [cannot_tell 46 excluded]
```

### 2.1 Which classes are trained

`cannot_tell` is dropped from both heads. It is the taxonomy's `is_escape` class
and means *the body is visible but the answer will not resolve* — genuine
annotator uncertainty, and the one thing a model must not be taught to imitate.
This matches `SQL_*_NO_ESCAPE` in `labels_db`, which already treats escape
answers as missing data for the agreement statistics.

`not_visible` **is** trained, on both heads. Its definition is a property of the
crop rather than of the animal ("too little of the body is visible to read the
posture"), so it is predictable from pixels in a way `cannot_tell` is not — and
it is the model's only honest output on the many unjudgeable crops in the 46k.
The corpus says plainly how many those are: annotators raised `low_resolution`
**556 times**, more than every other flag combined. Without this class the model
would be forced to assign a posture to crops where no posture is legible, and
those confident wrong labels would flow straight into the dashboard and the CSV.

`head_probing` is kept as a fourth behaviour class on 12 examples, knowingly.
Expect near-zero recall; the confusion matrix will show it plainly, and it is
visually near-identical to `feeding` ("head down at hardware, ground or water,
*investigating rather than eating*"). It is carried rather than merged so the
dashboard keeps the distinction the taxonomy makes, and so the class grows into
usefulness instead of having to be re-separated later.

### 2.2 `head_probing` was merged away (2026-09-17)

Kept as a fourth class through eleven CV configurations and never learnable:
pooled F1 ≤ 0.12 on all 56 examples, absorbed by `standing` (30) and `feeding`
(16) in the confusion matrix, and costing the headline ~0.02 for nothing. It is
now archived in the taxonomy — `cownting labels archive-class
behaviour.head_probing`, an audited soft archive; annotators are no longer
offered it — and its 56 existing answers are carried into the training set by
`config/instance_attrs_relabels.csv`: **17 → feeding, 41 → standing**, each
instance decided by the mean softmax of the two trained checkpoints (resnet18
`1452984b21ba` and efficientnet_b0 `9deb70536869`) over those two classes. The
two models disagreed on 16; those went by the averaged probability. Every
decision, both models' probabilities and the original annotator are in
`config/instance_attrs_relabels.audit.csv`.

The store was NOT rewritten — it is append-only by design — and the pre-merge
store is at `data/backups/labels-pre-merge/` on the box. Reverting the merge is
deleting the `relabels:` line and restoring the class; nothing else moves.

> **Backlog dependency.** `not_visible` is 136 + 64 answers because the queue is
> serving crops too small to judge. The fix is upstream in the sampling, not
> here — see `futurework.md` → *"Stop serving unjudgeable crops"*. Once that
> lands, `not_visible` should fall below 10% of answers and this head's class
> balance changes; the model is retrained, not redesigned.

### 2.3 One escape hatch, and why its answers are still not trained (2026-09-18)

Each question shipped with TWO ways to decline: `not_visible` ("too little of
the body is visible"), a normal trained class, and `cannot_tell` ("the body is
visible, but it will not resolve"), an `is_escape` class that the dataset
builder dropped as missing data. The distinction is real and was correctly
drawn. **Annotators did not observe it.**

Measured over the same days, the same camera-days and near-identical crop sizes
(median longer side 79px vs 84px):

| | `not_visible` | `cannot_tell` | sum | sun | shade |
|---|---|---|---|---|---|
| kilian | **45.4%** | 7.5% | 52.9% | 28.6% | 18.4% |
| alexandra | **17.8%** | 26.5% | 44.3% | 30.2% | 25.5% |

They agree closely on WHICH crops are unanswerable and disagree almost totally
on which button says so — day by day, kilian's `not_visible` tracks alexandra's
`not_visible + cannot_tell`. Because the escape was dropped and the other was
trained, kilian supplied **75%** of the trained `not_visible` examples while
supplying ~50% of `shaded` and `direct_sun`: the class had become one person's
convention. Inter-annotator agreement on the 49 doubly-labelled instances is
55% for sun and 71% for behaviour, and the single largest disagreement is
exactly `cannot_tell` vs `not_visible`.

**The taxonomy fix: one no-answer option per question.** `cannot_tell` is
archived; `not_visible` keeps its class_key (it is in every stored answer and
every `attr_*` column) and takes the title *Cannot tell*, the question glyph,
and a definition naming every reason for having no answer. Two buttons that
mean the same thing to the people pressing them are one button.

**The training fix that was measured and REJECTED.** The obvious follow-on —
fold the escape answers into the trained class and recover the 427 instances
(319 sun, 108 behaviour) the builder had been dropping — makes the model worse
on every class the dashboard shows:

| | E11 (deployed) | E12 (escapes merged) |
|---|---|---|
| behaviour coverage after abstention | 73.1% | 70.0% |
| behaviour precision of shown | 90.7% | 88.4% |
| sun shade/direct read correctly¹ | 80.4% | 78.4% |
| sun `shaded` F1¹ | 0.787 | 0.710 |
| sun `direct_sun` F1¹ | 0.857 | 0.826 |

¹ over the identical instances in both runs (1,052 / 439 / 613), so this is a
like-for-like comparison and not an artifact of the evaluation set growing.
Fold std is 0.011–0.013, so the `shaded` drop is six standard deviations.

The mechanism is visible in the class: `not_visible` grows 686 → 1,005 and
stops being one thing. "Too little of the body is visible" is a property of the
pixels and trains to 0.87. "The body is visible but it will not resolve" is the
annotator's epistemic state and is not in the crop at all. The mixed class is
noisier, and the noise bleeds outward — `shaded` precision falls 0.83 → 0.70.

So `merge_classes` stays empty and the escape stays out of training, which is
what `is_escape` always meant. The dataset builder still CARRIES an escape-only
answer now (`_SQL_ANSWERS` yields rather than discards, and a substantive answer
from any rater still wins) so the decision is a config line rather than a schema
change — but the measured answer is no.

**What remains open.** The surviving no-answer option is still trained, so
future answers to it re-enter the class this experiment just showed to be
mixed. The clean end state is probably to drop the trained catch-all on the sun
head as well and let confidence carry it, exactly as behaviour does (§9.3) —
`exclude_classes` exists for that experiment and it has not been run.

## 3. The banner mask is a methodological requirement

Training crops are cut by **`labeling.render_crop`** — the same function that
served the annotator — and for one non-negotiable reason.

Brinno burns wall-clock time into the bottom ~4% of every frame. Time of day
correlates directly with sun exposure. `render_crop` masks that band, and
`labeling.py` says why in its own words: a visible banner "hands the annotator
the answer and inflates agreement artificially". A model is a far more diligent
reader of a timestamp than a tired human. Cut these crops any other way — a
plain `Image.crop` on the bbox — and the sun head learns to read the clock,
reports ~95% validation accuracy, and collapses the first time it meets a camera
whose clock is set differently.

Reusing `render_crop` also means the model is trained on **exactly the pixels the
label describes**: same padding (`crop_pad`), same square canvas, same neutral
out-of-frame fill, same JPEG quantisation. A label is a statement about an image,
and the image has to be the same one.

## 4. Train/val split: contiguous camera-day blocks

80/20, but the unit is a **(dataset, camera, ~30 min) block**, not an instance.

The footage is 1 frame/minute and cows barely move minute-to-minute, so two
instances from adjacent frames are near-duplicates. Splitting at random puts one
in train and its twin in val, and the resulting score measures memorisation.
`finetune/dataset.py` already made this exact call for the mask fine-tune ("the
train/val split is by contiguous time-block, not random, so near-duplicate
adjacent minutes never straddle the split, which would inflate val metrics") —
M5 follows it rather than inventing a second rule.

The cost is honest and worth stating: with 543 samples, block-level splitting
makes the val set small and lumpy, so per-class val numbers on the rare classes
(`head_probing` above all) will be noisy. A noisy honest number beats a precise
dishonest one.

## 5. A false-positive is flagged, never deleted

The gate writes `attr_is_cow` + a probability. It does **not** delete rows.

Deleting is the tempting version — every existing query gets the benefit with no
code change — and it is wrong here. Re-segmenting to recover from a bad
threshold costs ~4 s/frame on this CPU-only box, about 8 hours for 7,261 frames,
and a threshold that is slightly too aggressive destroys real cows silently.
Flagging keeps a threshold change a re-query, keeps the dropped set auditable,
and matches the way every other judgement in this system is stored — the label
store supersedes rather than overwrites, and `mask_edits` retires an instance
without touching `detections`.

Filtering is applied through **one shared SQL predicate** defined in a single
place, so counts, day-series, area-counts, crosstabs and the CSV cannot drift
apart on what "a detection" means. Rows never scored are included: `coalesce`
makes an unscored corpus behave exactly as it does today.

> **This changes historical counts the moment it is run.** Every chart that
> counts detections will move. That is the point of the feature, but it is not a
> silent change — the gate is config-gated and off until deliberately enabled,
> and the dashboard reports how many detections the gate is currently hiding.

## 6. Storage

Additive columns on `detections`, via the `db.init_db` `ADD COLUMN IF NOT EXISTS`
idiom, so an existing database upgrades in place:

```
attr_is_cow        BOOLEAN   -- gate verdict at the configured threshold
attr_cow_p         DOUBLE    -- P(cow); the threshold is re-appliable without re-running
attr_sun           VARCHAR   -- shaded | direct_sun | not_visible
attr_sun_p         DOUBLE
attr_behaviour     VARCHAR   -- feeding | lying | standing | head_probing | not_visible
attr_behaviour_p   DOUBLE
attr_model_rev     VARCHAR   -- which checkpoint wrote this row
```

They live on `detections` — not in the label store — because they are *derived*:
a re-ingest purges the detection and the prediction should go with it. The
annotations that trained the model are the irreplaceable thing, and they stay
where they are. `attr_model_rev` is what makes a partial re-score detectable
rather than a silent mix of two models' opinions.

`in_shade` is **not** written. It is M2's column with M2's meaning (geometric
shadow), and overloading it would make the `shade` feature ambiguous about its
own provenance. The registry gains a `sun` feature instead; `in_shade` stays
reserved for whoever eventually builds M2.

## 7. Serving

Inference runs **in-process, triggered through the API**, exactly as
`POST /api/remask` does and for the same reason: DuckDB allows one read-write
process per file, the server holds it, and a CLI scorer would take the site down
for the duration.

The work is grouped **by frame, not by detection**. 46,486 detections live on
7,261 frames — roughly 6.4 per frame — so decoding per detection would decode
each frame six times over. One decode, all its crops, one batch.

A shell on the box has no session for the poweruser route, so there is a second
way in: a request file (`data/attrs_score.request`) that the in-process ticker
consumes and turns into the same job (DEPLOY.md). The pass yields to a waiting
*upload* — and only to an upload: the first production run yielded to its own
running job and reloaded the model every three seconds until py-spy found it
(commit `6cb20d4`). On the box's CPU a full pass over the corpus is ~65 min
(ResNet18, ~110 frames/min); an interrupted pass resumes, because scored rows
carry the rev that scored them.

## 8. Phasing

| Step | Ships |
|---|---|
| 1 | This document + the backlog entry it depends on |
| 2 | `instance_attrs/dataset.py` — label store → crops + manifest, blocked split. CLI `attrs build-dataset` |
| 3 | `instance_attrs/model.py` + `train.py` — multi-head net, masked loss, per-head val report. CLI `attrs train` |
| 4 | `instance_attrs/infer.py` + schema + `POST /api/attrs/score`. CLI `attrs score` |
| 5 | `features.py` + `db.export_df` + frontend — the numbers reach insights and the CSV |
| 6 | Deploy; score all 17 days |
| 7 | Calibrated abstention (§9.3), the `unsure` bucket end to end, KPI strips, frame badges, the request-file trigger |

Each step is independently green and independently useful: step 2 produces an
inspectable dataset whether or not step 3 ever trains well.

**Where it stands (2026-09-25).** All seven shipped. Production runs rev
`5a6dfd5ef295` (ResNet18, 2,973 labels) over the whole corpus;
`score_on_ingest` is on, so a new day scores itself after segmentation; and
overlays are repainted from the database whenever the verdicts change (§11), so
a detection the model rejects is no longer drawn as a cow. `gate_enabled` is
still off — the dashboard shows what the model thinks, it does not yet drop
what the model rejects from the COUNTS (§10).

| rev | labels | fixed-split mean macro-F1 | 5-fold CV pooled |
|---|---|---|---|
| `bd64f61f` | 477 | — | — |
| `e27d6d4d6638` | 2,381 | 0.8224 | 0.8455 |
| **`5a6dfd5ef295`** | **2,973** | **0.8465** | **0.8562** |

More labels, same recipe, better on seven of nine classes — including the gate
(`not_cow` 0.931 → 0.943) and the weakest class (`behaviour.not_visible`
0.426 → 0.473).

## 9. The recipe, and how it was chosen (2026-09-16/17)

Everything in `InstanceAttrsCfg`'s recipe block was decided by `cownting attrs
cv` — 5-fold grouped CV over the 2,381-instance corpus of 2026-09-16 — and that
harness is what should decide any future change. The reason it exists: a single
train/val split's fold-to-fold spread is **±0.014** pooled and its rerun noise
(same seed, cuDNN) **±0.005**, so a 0.01 gain on one split is indistinguishable
from nothing. The 2026-09-16 retrain showed the failure mode exactly — 3.8x the
labels, headline flat, because 16 val examples of `head_probing` masked a real
gain on every other class.

Pooled mean macro-F1, baseline 0.773–0.778 (mobilenet_v3_small, no augmentation):

| change | pooled | verdict |
|---|---|---|
| tempered class weights (power 0.5) | 0.774 | no effect |
| + label smoothing, EMA, warmup, 5x head LR | 0.778 | no effect on the mean; gate fold spread ±0.015 → ±0.006 |
| + rotation 10°, erasing 0.25, blur 0.25 | **0.788** | +0.010, all three heads up, 25–60px gate F1 0.77 → 0.81 |
| size reweighting toward production's mix | 0.776 | **hurt** — gate 0.948 → 0.937, 25–60px gate 0.77 → 0.72 |
| longer schedule (50/10 vs 30/6) | 0.786 | neutral |
| trunk: mobilenet_v3_large / efficientnet_b0 / resnet18 (baseline recipe) | 0.780 / 0.792 / 0.792 | behaviour 0.54 → 0.59 |
| **efficientnet_b0 + full recipe** | **0.801 ± 0.009** | wins every fold vs baseline |
| **resnet18 + full recipe** | **0.798 ± 0.007** | tie with B0; 36.5 vs 43.5 ms/crop on the box's CPU |
| resnet18 + full recipe, head_probing merged (§2.2) | 0.849 ± 0.007 | four behaviour classes, so NOT comparable to the row above; on the shared four, behaviour 0.717 → 0.741, standing 0.69 → 0.74; gate and sun unchanged |

ResNet18 ships: the two trunks tie within rerun noise on the headline and on
the 25–60px gate, and it is the faster of the two on the serving box (depthwise
convolutions are memory-bound on CPU; the GFLOPs ordering is not the wall-clock
ordering). Behaviour per class, pooled over all 1,943 labelled instances,
baseline → shipped → after the head_probing merge: feeding 0.72 → 0.81 → 0.82,
lying 0.91 → 0.97 → 0.97, standing 0.55 → 0.69 → 0.74, not_visible 0.42 → 0.40
→ 0.43. The merge's headline jump (0.798 → 0.849) is mostly the metric
changing — a mean over four learnable classes instead of five with one at 0.09
— and about +0.024 of it is a real gain on the shared classes, above the
±0.009 fold spread. Trained checkpoint: resnet18 `e27d6d4d6638`, 2026-09-17.

### 9.1 Judge it by crop size

The labelled corpus is bigger-crop than the pool the model scores: a crop under
60px was usually flagged rather than answered, so train and val sit at a median
of 84px while the 46,486-detection pool is a median of 57px with 48% at
25–60px. Every headline is therefore optimistic for production, and every report
now carries the size-band table. The number to read for the gate is **not_cow
F1 on 25–60px**: the 2026-09-16 retrain had dropped it from 0.88 to 0.75 while
its headline stayed at 0.97; the shipped recipe has it at 0.80 pooled under CV
and 0.88 on the fixed split.

### 9.2 What the fixed split can and cannot say

Two seeds of the same recipe on the same 477-item split landed at 0.763 and
0.775 — the seed alone moves it by 0.012, which is the entire gap between the
shipped model and the earlier ones on that split. The 58 items in `head_probing`
and behaviour's `not_visible` decide it, and neither is learnable at this
volume. The split is fine for early stopping and for the size-band gate figure;
it is not fine for choosing between recipes. Use the CV.

### 9.3 Behaviour: abstain rather than guess (2026-09-17)

The behaviour head's `not_visible` is the annotator's epistemic state, not a
look — "too little of the body is visible to read the posture" — and it trains
to ~40% recall / ~50% precision in every configuration, because a partially
occluded cow looks like a cow. Sun's `not_visible` is different (dark, blown
out, cut off *look* like something) and works at 0.84 F1; it is left alone.

So the dashboard does not try to detect "not visible" for behaviour. It declines
to show a posture it cannot back. Scoring writes `attr_behaviour = NULL` with
`attr_model_rev` set — the row reads as **unsure**, distinct from **unscored** —
when `max(feeding, lying, standing) < abstain_min_conf`. The threshold was fit on
out-of-fold probabilities of all 1,943 behaviour-labelled instances:

| rule | coverage | precision of shown postures | annotator's not_visible caught |
|---|---|---|---|
| argmax, no abstention | 92.0% | 83.3% | — |
| conf < 0.5 | 81.8% | 87.7% | 61.9% |
| **conf < 0.6 (default)** | **73.1%** | **90.7%** | **74.8%** |
| conf < 0.7 | 64.0% | 93.0% | 83.2% |
| conf < 0.8 | 50.8% | 95.4% | 92.6% |

A `p(not_visible)` threshold was tested alongside and adds nothing: high p(nv)
already forces low confidence. The class is still trained, and should be — it is
what keeps the confidence honest on occluded crops. Dropping it and relying on
confidence alone was not measured, and is the one variant here that was not.

The operating point is a config line (`instance_attrs.abstain_min_conf`); the
number to watch is the `unsure` bucket in the behaviour pivot and the KPI rail.

### 9.4 Dropping the decline class: measured, and rejected (2026-09-25)

§2.3 argues from the taxonomy that the surviving decline option should be
trained. With 2,973 labels it became worth settling with a number, because the
opposite is also arguable: "Cannot tell" is an annotator's epistemic state, and
training a classifier to predict a state of mind is a strange thing to ask.

Two 5-fold runs on identical data and recipe. **A** keeps the four-class
behaviour head. **B** removes `behaviour.not_visible` from the head entirely,
leaving abstention to carry every "cannot tell" through the confidence
threshold.

B looks emphatically better:

| behaviour | A | B |
|---|---|---|
| feeding | 0.827 | 0.896 |
| lying | 0.967 | 0.976 |
| standing | 0.735 | 0.793 |
| macro over the three postures | 0.843 | **0.888** |

**And that comparison is invalid.** Removing the class removes those instances
from the EVALUATION set as well as from training, so B is being marked on an
easier exam: 2,097 readable crops against A's 2,353 including the 256 a human
could not read. The gain is the exam, not the model.

Judged on the same population — every instance a human answered for behaviour,
declines included — and at matched coverage, the ranking reverses:

| at ~74% coverage | precision of shown postures | annotator-declines correctly abstained |
|---|---|---|
| **A** (conf < 0.6, the deployed rule) | **90.9%** | **75.8%** |
| B (conf < 0.76, tuned to match) | 88.3% | 53.9% |

A is better on both axes at once. The reason is visible in the confidences: on
crops a human could not read, B's median confidence is 0.738 against 0.893 on
readable ones — some separation, but nowhere near enough to act on, whereas A's
trained decline class pulls probability mass away from the postures and makes
the threshold mean something. **The class is what keeps the confidence honest.**

So it stays, and the lesson generalises past this head: when a change alters
which rows are SCORED, the headline metric is measuring a different question and
the two numbers cannot be compared at all.

## 10. Known risks

- **543 samples is small.** Transfer learning from ImageNet makes it viable, not
  comfortable. The honest expectation is a useful sun head, a usable behaviour
  head on its three populated classes, and a gate whose precision matters more
  than its recall.
- **76 negatives is smaller.** The gate is trained with class weighting and
  judged on precision at a high threshold: wrongly dropping a real cow is worse
  than keeping a false one, because the count is the product.
- **One site, five cameras, 17 days.** Nothing here has been shown to transfer to
  a different farm, and the block split does not test that. Treat cross-site
  performance as unmeasured.
- **`head_probing` is unlearnable at n=56** — pooled F1 ≤ 0.12 in every one of the eleven CV configurations tried. It is visually near-identical to `feeding` and should be merged into it or dropped from the model's head; keeping it costs the headline metric ~0.02 for nothing.

**Seen in the first production pass (2026-09-17).** Two things the numbers
say that the CV could not:

- *Sun has no word for overcast.* `sun_exposure.not_visible` is 2–3% of
  detections on 06-29, 07-02 and 07-05 and 91–98% on 07-04, 08-30, 09-01 and
  09-02 — all day long, every hour (07-04: 98% at 08:00, 91% at 10:00), not a
  dawn artefact and not a crop-size effect (≈45% in every size band). Those are
  overcast days: neither "shaded" nor "in direct sun" is true of anything, and
  the model reaches for the only remaining class. It is doing what the
  taxonomy allows. The annotator did the same — 56% of the labelled instances
  are `not_visible`. A fourth class ("diffuse / no direct light") would turn
  the largest bucket on the dashboard into an answer; until then, read the
  light strip per day, not across days.
- *The gate has found a fixed false positive.* 9.5% of detections would be
  rejected at p<0.5, but 17.5% on camera_04 against 4–9% elsewhere, and 921 of
  camera_04's rejections sit in one 100-px cell (x 0–100, y 600–700) across 12
  different days — something the segmenter reads as a cow at that spot, day
  after day, that the crop model reads as not one. That is the gate's whole
  purpose, and a good first thing to look at (frames 169/175/179 of
  2025-07-04 · camera_04 show five or six of them struck through) before
  deciding on `gate_enabled`.

## 11. The overlay is a cache of the model's opinion (2026-09-25)

`segment` burns the instance masks into a JPEG. The crop model has not run yet
at that point — it cannot have, the crops come from the very detections being
written — so the picture is made before anything knows which blobs are cows.
The verdict lands minutes later, and changes again at every retrain.

That makes an overlay a cache with no invalidation, and for a while it had
none: 4,435 detections the model called not-a-cow were still painted as cows on
3,078 frames.

`pipeline.reoverlay` rebuilds the image from `mask_poly` + `attr_is_cow` — no
GPU, no segmenter, ~55 ms a frame, about seven minutes for the corpus against
hours for a re-segmentation. `frames.overlay_rev` records the rev each image was
painted for, and a frame is selected only when that differs from the rev its own
detections now carry. That single column buys idempotence, resumability, and
automatic invalidation: a re-score marks exactly the affected frames without any
bookkeeping, and the pass is chained after both scoring paths.

Two invariants have pixel-probing tests, because either would silently corrupt
18 GB of images recoverable only by re-segmenting:

- **NULL is not FALSE.** `attr_is_cow` is NULL for every never-scored row, which
  is most of the corpus most of the time. `coalesce(attr_is_cow, TRUE)`
  (`db.mask_drawn_clause`), never `NOT attr_is_cow` — NULL propagates through
  NOT and would blank every unscored day.
- **The palette is positional.** Colours come from the `enumerate` index, so a
  hidden detection must still consume its index. Pre-filtering the list
  recolours every animal after it, on every frame.

Deliberately NOT tied to `gate_enabled`: that flag decides what COUNTS and moves
every historical number, so it needs deliberate opt-in; this decides what is
PAINTED, and nobody opts in to not having a fence post tinted as a cow.

**Fidelity.** `mask_to_polygon` stores the largest external contour only, so a
cow split by an occluding panel repaints as its bigger half; 4,135 of 46,486
detections have `mask_parts > 1`. The extra contours were discarded at
segmentation time and only `remask` can recover them.
