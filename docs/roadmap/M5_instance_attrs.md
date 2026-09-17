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

## 8. Phasing

| Step | Ships |
|---|---|
| 1 | This document + the backlog entry it depends on |
| 2 | `instance_attrs/dataset.py` — label store → crops + manifest, blocked split. CLI `attrs build-dataset` |
| 3 | `instance_attrs/model.py` + `train.py` — multi-head net, masked loss, per-head val report. CLI `attrs train` |
| 4 | `instance_attrs/infer.py` + schema + `POST /api/attrs/score`. CLI `attrs score` |
| 5 | `features.py` + `db.export_df` + frontend — the numbers reach insights and the CSV |
| 6 | Deploy; score all 17 days |

Each step is independently green and independently useful: step 2 produces an
inspectable dataset whether or not step 3 ever trains well.

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
