---
name: new-labels
description: The drill for turning newly-collected Label-page annotations into a deployed instance-attribute model — back up, build the crop dataset, cross-validate against the model in production, decide honestly, train, ship, rescore the corpus, verify, and audit what the queue bought. Use when the user says new labels have landed, asks to retrain / revalidate / redeploy the attribute (crop / cow-gate / sun / behaviour) model, says "you know the drill" about labels, or asks whether a new model is better than the deployed one.
---

# New labels have landed

Two things need the user's explicit go-ahead before you do them, every time:

- **rescoring the corpus** (it rewrites `attr_*` on every detection and repaints
  every overlay), and
- **adding, merging or archiving a class.**

Present numbers, then ask. Never start either on your own initiative.

## The drill

**0 · Back up the labels first.** Annotator hours are the only state a re-ingest
cannot rebuild. `POST /api/labels/backup/run` (or `cownting labels backup
--force`). Note that a `--force` run is an extra copy, not a substitute for the
weekly one. Never print the webhook URL — pipe through
`sed -E "s#https://[^ ]*#<redacted>#gi"`.

**1 · Measure what actually landed.** `GET /api/label/progress` for counts, and
`queue_reason` on the new annotations for *which selector paid for them* (see
step 9). Say how many new labels there are before claiming anything about them.

**2 · Build the crop dataset — with the container stopped.**
`attrs build-dataset` takes a DuckDB write handle, and DuckDB allows one
read-write process per file: run it against a live server and the site goes
down, it does not merely slow.

```bash
docker compose stop cownting
docker compose run --rm cownting python -m cownting.cli attrs build-dataset \
    -c config/cownting.prod.yaml
docker compose up -d cownting        # bring it back before anything else
```

Copy the dataset off the box (a few MB) and work locally from here.

**3 · Cross-validate. This is the number that decides.**

```bash
py -3.11 -m cownting.cli attrs cv -c config/cownting.yaml --out cv_new.json
```

Run it for the **current recipe on the new label set** and, if you are changing
anything about the recipe, for each variant. Read `VALIDATION.md` before you
compare anything — the two ways this goes wrong have both already happened here,
and both produced a confident recommendation that was wrong.

**4 · Decide, and show your work.** Report pooled mean macro-F1, the fold
spread, and per-class deltas — with the spread stated next to them so a delta
inside it is visibly noise. If it is not better, say so and stop; a null result
is a result.

**5 · Train the checkpoint you intend to ship.** `attrs train` writes
`data/models/instance_attrs.pt` and, beside it, `instance_attrs.metrics.json`
(`<weights-stem>.metrics.json`). That report is the held-out 20% of one split and
is **optimistic** — quote CV for decisions and this only as "what the shipped
checkpoint scored on its own split".

**6 · Ship it.** See `SHIP.md` — the deploy has a hook-blocked push and a
permanently-dirty prod config, so `git pull` on the box does not work.

**7 · Rescore, once the user has said yes.** Through the API or the request
file, never the CLI — it must run in-process on the upload queue. Overlays
repaint themselves afterwards. `SHIP.md` has the commands and the
overlay-archiving caveat.

**8 · Verify on the box, not from memory.** `GET /api/attrs/status`: `scored`
should equal `detections` and `revs` should be `1`. If `revs` is 2 the corpus is
half-scored by two models and every count is a blend.

**9 · Audit what the queue bought.** The point of `queue_reason` is that the
selector can be judged against the model it fed. For each reason bucket, compare
the class mix it produced against `uniform` (the unbiased slice), and check which
classes actually moved in CV. Feed that back into `annotation.seek_classes` /
`gate_band` / `abstain_min_px` — that is how the next round gets cheaper.

**10 · Report.** Distinguish the three estimators explicitly; they answer
different questions and are not comparable. `VALIDATION.md` §Reporting has the
shape.

## Files

- [VALIDATION.md](VALIDATION.md) — how to compare two models without fooling yourself
- [SHIP.md](SHIP.md) — deploy, rescore, verify, and how to back out
