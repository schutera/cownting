# Comparing two models without fooling yourself

Every mistake below has actually been made on this project, and each one produced
a confident recommendation that was wrong. They are cheap to avoid and expensive
to miss, because the output of a bad comparison is a deployed model plus a
rescored corpus.

## The three estimators answer different questions

| estimator | what it is | what it is for |
|---|---|---|
| `attrs cv` pooled mean macro-F1 | k-fold, every instance scored by the fold that never saw it | **deciding.** The only controlled number. |
| `.metrics.json` from `attrs train` | the held-out 20% of one split | "what the shipped checkpoint scored on its own split", and nothing more |
| live corpus distribution (`/api/attrs/status`, class counts) | what the model actually produced in production | sanity, coverage, and abstention behaviour — never quality |

Never compare a CV figure against a held-out-split figure. Never present the
held-out number as the model's accuracy without saying which split it is.

## Trap 1 — the held-out split is optimistic, twice over

- **`fit` early-stops on `va_items` and returns the BEST epoch's report on that
  same `va_items`.** With ~40 evaluations that is a maximum-over-epochs, worth
  roughly +0.025 for plateau noise of σ≈0.01 — about twice the fold spread. It is
  selection on the set being reported.
- **The split over-represents large crops** relative to the real corpus, and the
  model is much better on large crops (`by_size` in the report shows this
  directly). So it flatters production.

Both push the same direction. Quote CV.

## Trap 2 — a variant that changes the LABEL SET changes the EVAL set

This one cost a wrong recommendation. A variant that drops or merges a class
removes those instances from the **evaluation** population as well as from
training, so its macro-F1 is computed over an easier, smaller problem. It looked
better and was worse.

**Rule: compare on the same population.** If a variant changes which instances
exist, re-score the baseline on the variant's population too, and report both
numbers on both populations. If you cannot do that, you cannot compare them.

## Trap 2b — the training population is `annotations` UNION `mask_edits`

This one caught me while running this drill for real, and it is the easiest of
the lot to fall into because the wrong query looks so obviously right.

`v_current_answers` is **annotations only**. The `cow` head does not come from
there — it comes from `mask_edits`, where a `false_positive` verdict means
`not_cow` and `ok`/`polygon` mean `cow` (instance_attrs/dataset.py). So:

- counting `SELECT count(DISTINCT effective_key) FROM v_current_answers` gave
  2,567 against a deployed manifest of 2,973, and
- the difference looked like **444 training instances had vanished from the
  store** — a data-loss emergency.

Nothing had vanished. Those 444 were cow-gate-only instances: 413 `not_cow` and
31 `cow`, every one of them with `sun_exposure` and `behaviour` NULL, because a
geometry verdict answers neither question. Counting the union gave 3,014
instances, of which 2,973 were exactly the deployed set and 41 were genuinely
new.

`scripts/label-delta.py` does this correctly. Use it rather than writing the
query again.

Two related details worth not re-deriving:

- Compare against the **deployed model's `manifest.json` keys**, not a remembered
  training count. The manifest is the only authority on what the model saw.
- `manifest.json`'s `instance_key` field holds the store's `effective_key` at
  build time, so join on `effective_key`. Joining on `annotations.instance_key`
  will diverge for anything a re-ingest re-keyed.

## Trap 3 — per-class deltas inside the fold spread mean nothing

The CV output carries `spread.mean_macro_f1` — read the std from there, never
from memory, and never hardcode a number from a previous round.

State the spread **next to** every per-class delta. A single-class improvement
smaller than the spread is not a finding, and saying "the gate is where the real
progress is" off an uncontrolled table after having warned against exactly that
is how this went wrong before. If only one class clears the spread, say that
plainly — one real improvement is a fine result.

## Trap 4 — the uncontrolled table is not the controlled one

Two models trained on different label counts, with different recipes, reported on
different splits, is four confounds. If you find yourself comparing numbers from
two different `.metrics.json` files, stop: run CV on both recipes over the same
label set instead.

## Things that are inert and will waste your time

- `behaviour.not_visible` cannot appear in `seek_classes`: `infer.score_detections`
  picks the written class with `max()` over the SUBSTANTIVE classes only and
  writes NULL instead, so `attr_behaviour` is never that value. Those crops are
  the abstentions, which the weak-posture selector already catches.
- With `abstain_nv_p=0.50` and `abstain_min_conf=0.60`, the nv branch is provably
  unreachable — `p(nv) >= 0.5` forces max-substantive `<= 0.5 < 0.6`. Documented,
  not a bug.

## Reporting

Give the user, in this order:

1. **How many new labels**, and which `queue_reason` buckets they came from.
2. **The controlled CV comparison**: pooled mean macro-F1 old → new, the fold
   spread, then a per-class table with deltas and the spread visible.
3. **A recommendation with a reason** — ship or don't, and what carried it.
4. **The shipped checkpoint's own held-out report**, labelled as such.
5. **What the abstention is doing**: coverage, and the confidence separation at
   the threshold (the lowest shown vs the highest withheld should meet at
   `abstain_min_conf`).

Then ask about rescoring. Do not rescore as part of reporting.
