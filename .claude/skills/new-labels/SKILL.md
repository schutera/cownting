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

Both scripts below are read-only and run inside the container:

```bash
S=.claude/skills/new-labels/scripts/label-delta.py      # or verify-rescore.py
B=$(basename $S) && scp $S <box>:/tmp/ && ssh <box> \
  "docker cp /tmp/$B cownting-cownting-1:/tmp/ &&
   docker exec -u cownting cownting-cownting-1 python /tmp/$B"
```

**0 · Is there enough new work? Gate on this before touching anything.** Run
[scripts/label-delta.py](scripts/label-delta.py). It compares the live store against the **deployed model's own dataset manifest**
and exits non-zero when a retrain would chase noise. It counts `annotations`
UNION `mask_edits`, which is the training population — counting answers alone
understates it by hundreds of instances (VALIDATION.md trap 2b).

NOT ENOUGH means **stop here**: report the numbers, say why you are not
retraining, and do not back up, stop the container or build anything. "41 new
instances, +1.4%, inside the fold spread" is a complete answer.

**1 · Back up the labels.** Only now, because the next step stops the container.
Annotator hours are the only state a re-ingest cannot rebuild.
`POST /api/labels/backup/run` (or `cownting labels backup --force` — an extra
copy, no longer a substitute for the weekly one). Never print the webhook URL:
pipe through `sed -E "s#https://[^ ]*#<redacted>#gi"`.

**2 · Build the crop dataset — with the container stopped.** `build-dataset`
takes a DuckDB write handle and DuckDB allows one read-write process per file, so
against a live server the site goes down, not merely slow.

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

Run it for the current recipe on the new label set, and for each variant if you
are changing the recipe. **Read [VALIDATION.md](VALIDATION.md) before comparing
anything** — four ways this goes wrong, every one of them already made here.

**4 · Decide, and show your work.** Pooled mean macro-F1, the fold spread, and
per-class deltas with the spread beside them so noise is visibly noise. Not
better? Say so and stop; a null result is a result.

**5 · Train the checkpoint you intend to ship.** `attrs train` writes
`data/models/instance_attrs.pt` and `instance_attrs.metrics.json` beside it. That
report is one split's held-out 20% and is **optimistic** — quote CV for
decisions, this only as "what the shipped checkpoint scored on its own split".

**6 · Ship it.** See `SHIP.md` — the deploy has a hook-blocked push and a
permanently-dirty prod config, so `git pull` on the box does not work.

**7 · Rescore, once the user has said yes.** Via the API or the request file,
never the CLI — it must run in-process. A **new** rev makes the plain call cover
the whole corpus; `rescore=true` is for re-running the *same* rev and makes the
pass non-resumable. Overlays repaint themselves. See `SHIP.md`.

**8 · Verify on the box, not from memory.** Run
[scripts/verify-rescore.py](scripts/verify-rescore.py).

`scored == detections` is **not** a sufficient check and can be impossible to
satisfy — a crop the timestamp-banner mask refuses is skipped without a rev and
pins its frame pending forever. And `revs == 1` cannot see unscored rows at all,
because `count(DISTINCT ...)` ignores NULL. The script knows this, checks the
overlays too, and names the data no rescore reaches. See `SHIP.md`.

**9 · Audit what the queue bought.** `queue_reason` exists so the selector can be
judged against the model it fed. Per bucket, compare the class mix against
`uniform` (the unbiased control) and check which classes actually moved in CV,
then feed that into `annotation.seek_classes` / `gate_band` / `abstain_min_px`.
`uniform` **can be empty** — membership is a hash of the key, so a small batch
may contain none, and then there is no control and no audit. Say that rather than
comparing selectors against each other.

**10 · Report.** Distinguish the three estimators explicitly; they answer
different questions and are not comparable. `VALIDATION.md` §Reporting has the
shape.

## Files

- [scripts/label-delta.py](scripts/label-delta.py) — step 0's gate; run it first, every time
- [scripts/verify-rescore.py](scripts/verify-rescore.py) — step 8; the naive checks do not work
- [VALIDATION.md](VALIDATION.md) — how to compare two models without fooling yourself
- [SHIP.md](SHIP.md) — deploy, rescore, verify, and how to back out
