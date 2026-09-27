# Shipping a new attribute model

Repo `DEPLOY.md` §"The instance attribute model (M5)" is the reference for what
the routes do and how the three switches (`score_on_ingest`, `gate_enabled`,
scoring itself) interact. This file covers only what that document gets wrong or
leaves out for *this* deployment.

## The code deploy: `git pull` on the box does not work

`DEPLOY.md` says `git pull && docker compose up -d --build`. On this box it
fails, for two reasons that are both permanent:

- **`config/cownting.prod.yaml` is tracked and permanently modified on the
  server** (it is bind-mounted and edited in place), so a pull refuses. Never
  stage that file in a commit from the dev box either — a commit touching it
  would conflict on every future deploy.
- **pushing is hook-blocked**, so the box cannot fetch from origin. The user
  pushes to origin themselves, on their own schedule.

So ship with a bundle:

```bash
# 1. Ask the box what it is actually on. Do NOT assume it matches origin/main.
ssh <box> 'cd /opt/cownting && git rev-parse --short HEAD'

# 2. Bundle from THAT commit, not from your idea of it. Getting this wrong
#    produces a bundle the box cannot apply, with a confusing error.
git bundle create /tmp/ship.bundle <server-head>..HEAD

# 3. Copy, fetch, check out.
scp /tmp/ship.bundle <box>:/tmp/
ssh <box> 'cd /opt/cownting && git fetch /tmp/ship.bundle HEAD && git checkout FETCH_HEAD'
```

If the bundle will not apply, re-read step 1 — it is almost always a base
mismatch, not a corrupt bundle.

## The checkpoint

`scp data/models/instance_attrs.pt <box>:/opt/cownting/data/models/`

The rev is `sha256(bytes)[:12]` of the file as written and is stored inside it,
so `attr_model_rev` on a detection identifies the exact bytes that scored it.
Read it from either end without loading the model onto a device:

```bash
py -3.11 - <<'PY'
from cownting.instance_attrs import model as M
b = M.load_checkpoint("data/models/instance_attrs.pt", device="cpu")
print(b["rev"], b["img_size"], list(b["heads"]))
PY
```

It refuses a `CHECKPOINT_VERSION` it cannot read, so a stale layout fails loudly
here rather than mis-predicting later. Also compare the md5 on both sides of the
`scp`: a truncated copy is otherwise indistinguishable from a working one until
scoring produces nonsense.

## Rebuild and wait for health

```bash
ssh <box> 'cd /opt/cownting && docker compose up -d --build'
ssh <box> 'cd /opt/cownting && docker compose ps'    # wait for (healthy)
```

**Read `docker-compose.override.yml` on the box before reasoning about the
topology.** It is gitignored and server-local, so it is invisible from here, and
on this deployment it changes three things that matter: it publishes the app to
`127.0.0.1:8090` (NOT 8000 — a stale docker-proxy still holds that port and
answers 404, which looks exactly like a broken deploy), it caps the container at
6 GB / 3.5 CPU, and it **disables the compose Caddy service entirely**
(`profiles: ["disabled"]`). Caddy runs on the HOST instead, fronting several
unrelated stacks.

So the `condition: service_healthy` that `docker-compose.yml` now puts on the
compose Caddy is inert here — host Caddy does NOT wait for the app's 45 s start
period, and will serve 502s across a restart. Curl the app on 8090 from the box,
or inside the container on 8000; curling host :8000 tells you nothing.

## Rescore — only after the user has said yes

Through the API or the request file, **never the CLI**: DuckDB allows one
read-write process and the server holds it, so a CLI score takes the site off the
air rather than slowing it.

```bash
curl -X POST 'https://<host>/api/attrs/score'                 # whole corpus, resumable
curl -X POST 'https://<host>/api/attrs/score?dataset=<day>'   # one day first, to look
```

**Do NOT pass `rescore=true` for a new model.** `pending_frames` selects on
`attr_model_rev IS NULL OR attr_model_rev <> <the new rev>`, so a new checkpoint
already matches every row in the corpus — the plain call rescores all of it.
`rescore=true` exists only to re-run the **same** rev, and it is the flag that
makes a pass non-resumable: with it, an interruption restarts from zero instead
of continuing. The `rescore=true` in the back-out section below is for putting
the PREVIOUS checkpoint back, where the rev is one the rows already carry.

Without a browser session, from a shell on the box:

```bash
docker exec -u cownting cownting-cownting-1 \
  sh -c 'echo "{}" > /app/data/attrs_score.request'
```

The in-process ticker picks it up within 15 minutes (2 minutes right after a
restart). Progress rides `GET /api/uploads` like every other job — there is no
second status route. The pass yields to a waiting upload and re-queues itself,
and scored rows carry the rev, so an interrupted pass resumes rather than
restarts.

**Scoring chains a repaint.** Overlays are a cache of a model's opinion: the
masks of detections the new model rejects stop being drawn. The repaint
**overwrites the JPEGs in place** and the originals come back only from a
re-ingest or a re-segmentation. If the current look matters, archive first:

```bash
ssh <box> 'cd /opt/cownting && cp -r data/artifacts/*/overlays data/backups/overlays-pre-<rev>/'
```

A repaint that yielded to an upload part-way re-queues itself and says so in the
job message. To force one by hand: `POST /api/reoverlay` (or `?force=true` to
repaint frames already stamped current).

## What a rescore does NOT reach

"Whole corpus" means `FROM detections` in the live DB. Three pockets sit outside
it, in every mode — including `rescore=true` and an explicit `?dataset=`:

- **`clipped_detections` / `clipped_frames`.** A camera clip MOVES rows here.
  `restore_clip` puts them back carrying whatever rev they left with, or none.
  **Re-score after any restore** — nothing does it for you and nothing warns.
  (On this box today: 23 rows for `2025-10-15 / camera_03`, all unscored. They
  would be counted as cows on restore, because the gate reads
  `coalesce(attr_is_cow, TRUE)`.)
- **The archive DB.** Archived days are never scored, and there is no implemented
  restore — `archive_dataset`'s "could be restored by the reverse move" is not
  code. An archive written before M5 has no `attr_*` columns at all.
- **Rows whose crop cannot be rendered.** Not a disk fault: `render_crop`
  deterministically refuses a crop the timestamp banner would cover more than
  `max_banner_fraction` of, and these cameras look down with the herd crossing
  the bottom of frame. Such a row is skipped WITHOUT a rev, and because a frame
  is pending if any of its detections lacks the rev, that frame stays pending
  forever. If it had been scored before, it keeps its OLD rev — which is the real
  way a corpus ends up holding two models at once.

## Verify — on the box, not from memory

Run [scripts/verify-rescore.py](scripts/verify-rescore.py). It exists because the
two obvious checks are both wrong:

- `scored == detections` may be **unsatisfiable** (see the unrenderable case
  above). The honest check is `scored + unscorable == detections`, with the
  shortfall recorded once as a baseline. The pass reports its own `unreadable`
  count in the job message on `GET /api/uploads` — read it and reconcile.
- `revs == 1` **cannot see unscored rows**: `count(DISTINCT attr_model_rev)`
  ignores NULL. It catches a stale-rev mix, which is the case that matters, but
  not an incomplete pass.

Two more things it checks that nothing else does: that the single rev present is
actually the deployed one, and that no frame carries two revs at once — the
repaint resolves a frame's rev with `max(attr_model_rev)`, a **lexicographic** max
over sha256 prefixes rather than a version, so a mixed frame can be stamped with
the OLDER rev and then read as clean forever.

Then check `GET /api/attrs/status` for `not_cow` (what the gate removes — compare
against CV's `not_cow` recall before assuming a bug) and `unsure` (abstentions,
expect roughly the CV coverage figure), and confirm the confidence separation:
the lowest confidence ever *shown* and the highest ever *withheld* should both
sit at `abstain_min_conf`. If they do not, the deployed checkpoint is not the one
you validated.

## If you changed a THRESHOLD instead of the weights

`cow_threshold`, `abstain_min_conf` and `abstain_nv_p` change the verdicts without
changing the checkpoint, so the rev does not move. Consequences:

- The score pass needs `rescore=true`, because the default skips rows already
  carrying that rev.
- **The chained repaint will not fire.** `_run_attrs_score` calls
  `pipeline.reoverlay` with `force` at its default False, and the staleness
  predicate compares `overlay_rev` against the detections' rev — unchanged, so
  every frame reads as clean while its mask decisions have actually changed. Run
  `POST /api/reoverlay?force=true` by hand.

## Backing out

Nothing is destroyed at any point.

- **Bad model, corpus already rescored:** put the previous checkpoint back and
  rescore with `?rescore=true` (the default skips rows already carrying the
  current rev, so without it nothing happens). Scoring OVERWRITES `attr_*` — the
  previous model's verdicts are not kept anywhere — so the only way back is the
  previous `.pt`. Keep it, and its `.metrics.json`, before you copy a new one
  over.
- **Counts moved unexpectedly:** `instance_attrs.gate_enabled: false` and
  restart. The verdicts stay on the rows; the counts return. Edit the prod config
  **on the box**, never in a commit.
- **Overlays wrong:** restore the archived directory, or `POST /api/reoverlay?force=true`
  once the right checkpoint is in place.
