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

Caddy now waits on the app's healthcheck (`condition: service_healthy`), and the
Dockerfile allows a 45 s start period, so the first ~45 s of a deploy is expected
to look idle. That is not a hang.

## Rescore — only after the user has said yes

Through the API or the request file, **never the CLI**: DuckDB allows one
read-write process and the server holds it, so a CLI score takes the site off the
air rather than slowing it.

```bash
curl -X POST 'https://<host>/api/attrs/score'                 # whole corpus, resumable
curl -X POST 'https://<host>/api/attrs/score?dataset=<day>'   # one day first, to look
```

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

## Verify — on the box, not from memory

`GET /api/attrs/status`:

- `scored == detections` — the pass finished.
- `revs == 1` — **the one that matters.** `revs == 2` means the corpus is
  half-scored by two models and every count is a blend of them.
- `not_cow` — what the gate would remove. Compare against what you expected from
  CV's `not_cow` recall before assuming a scoring bug.
- `unsure` — abstentions. Expect roughly the CV coverage figure.

Then pull the live class distribution and the confidence separation: the lowest
confidence ever *shown* and the highest ever *withheld* should both sit at
`abstain_min_conf`. If they do not, the deployed checkpoint is not the one you
validated.

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
