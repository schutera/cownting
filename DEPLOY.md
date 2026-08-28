# Deploying Cownting

Production deployment as a Docker Compose stack: the FastAPI app (uvicorn,
serving the built React frontend) behind a **Caddy** reverse proxy that
terminates TLS. CPU-only inference — segmentation/pose run on CPU, so no GPU or
NVIDIA drivers are required.

```
             :80/:443                     :8000 (internal)
  browser ───────────▶  Caddy  ─────────────────────▶  cownting
                        (auto-HTTPS)                    (uvicorn + SPA)
                                                            │
                                                     /app/data volume
                                          (DuckDB, frames/overlays, uploads)
```

## What's in the box

| file | role |
|------|------|
| `Dockerfile` | 2-stage build: Node builds `frontend/dist`, Python runtime serves it. CPU torch, weights pre-fetched. |
| `docker-compose.yml` | `cownting` + `caddy` services, `./data` bind mount, secrets from `.env`. |
| `Caddyfile` | reverse proxy, auto-HTTPS, 10 GB upload body limit. |
| `.env.example` | secrets template (session key, bootstrap admin, site address). |
| `config/cownting.prod.yaml` | server config (empty camera list — uploads drive ingestion). |

## Prerequisites

- A Linux server with **Docker** and the **Docker Compose plugin**
  (`docker compose version`).
- To get HTTPS: a **domain name** with a DNS `A`/`AAAA` record pointing at the
  server, and ports **80** and **443** open. (You can also run on plain HTTP for
  a quick internal test — see below.)

## First deploy

```bash
# 1. Get the code onto the server
git clone <your-repo-url> cownting && cd cownting

# 2. Secrets
cp .env.example .env
#    - COWNTING_SECRET:          openssl rand -hex 32
#    - COWNTING_ADMIN_PASSWORD:  choose a strong password
#    - CADDY_SITE_ADDRESS:       your domain (e.g. cownting.example.com)
nano .env

# 3. If you're using a real domain over HTTPS, flip the cookie to HTTPS-only:
#    set  auth.https_only: true  in config/cownting.prod.yaml
nano config/cownting.prod.yaml   # optional

# 4. Build + start (first build downloads torch + the YOLO weights — a few minutes)
docker compose up -d --build

# 5. Watch it come up
docker compose logs -f cownting
```

Then open your domain (or `http://<server-ip>` for the plain-HTTP case) and log
in with `COWNTING_ADMIN_USER` / `COWNTING_ADMIN_PASSWORD`. Add data from the
dashboard's **upload** flow (one video per camera); the day processes in the
background (ingest → segment → localize) and appears on the dashboard.

### Plain HTTP (no domain, quick test)

Leave `CADDY_SITE_ADDRESS=:80` in `.env` and keep `auth.https_only: false`. The
app is reachable at `http://<server-ip>/`. Don't run a public instance this way —
the login cookie travels unencrypted.

## Operating it

```bash
docker compose ps                 # status + health
docker compose logs -f cownting   # app logs (upload/processing progress)
docker compose restart cownting   # restart just the app
docker compose down               # stop (data volume is preserved)
```

Update to a new version:

```bash
git pull
docker compose up -d --build      # rebuilds the image, recreates containers
```

### Managing login accounts from the host

```bash
# -u cownting: exec sessions default to root (the image boots as root so the
# entrypoint can heal data/ ownership); run maintenance as the app user instead.
docker compose exec -u cownting cownting python -m cownting.cli \
  --help                                   # see all commands
docker compose exec -u cownting cownting python -m cownting.cli user list \
  -c config/cownting.prod.yaml
docker compose exec -u cownting cownting python -m cownting.cli user passwd <name> \
  -c config/cownting.prod.yaml             # reset a password / recover access
```

Roles: `user` (view only), `poweruser` (upload/download/delete), `admin` (also
manages accounts, in the Admin page).

## Data & backups

**All persistent state is the `./data` directory** (bind-mounted to `/app/data`):
the DuckDB database, frame/overlay artifacts, uploaded videos, and the
count/panel area polygons. Back it up while the stack is stopped (or snapshot the
volume):

```bash
docker compose stop cownting
tar czf cownting-data-$(date +%Y%m%d).tgz data/
docker compose start cownting
```

The session secret and admin credentials live in `.env` — back that up too, out
of band. Neither `.env` nor `data/` is committed to git.

### Label store & backups

The in-app **Label** page writes annotations to their own DuckDB file,
`data/labels.duckdb` — deliberately outside the main DB, so re-ingesting or
deleting a day can't destroy them. It is also **the one file on the box that
cannot be regenerated**: frames, detections and overlays all come back from a
re-ingest; annotator hours do not. Treat it accordingly.

Because the weekly zips (below) land in `data/backups/`, the full-`data/` tar
above should skip them — they *are* backups:

```bash
tar czf cownting-data-$(date +%Y%m%d).tgz --exclude='data/backups' data/
```

**Weekly off-box backup.** An in-process scheduler zips the label store
(snapshot + CSV + taxonomy + manifest) into `data/backups/labels/` once a week
and posts the zip to a Discord webhook. It ships **off by default**:

```bash
# 1. config/cownting.prod.yaml:  backup.enabled: true
# 2. .env:                       COWNTING_DISCORD_WEBHOOK=<dedicated webhook URL>
docker compose up -d --build
```

Two rules for that webhook:

- **Use a dedicated channel — never the login-alerts webhook.** The zip contains
  annotator usernames and per-annotator timings; share it like personnel data,
  not like a database dump. The first run has no watermark, so it posts the
  *entire* store in one go.
- **Blank or unset is fine**: the job still zips and rotates locally
  (`backup.keep` zips, default 8), it just posts nothing. That is a supported
  state, not a failure.

**Label maintenance from the host** — same `-u cownting` rule as accounts:

```bash
docker compose exec -u cownting cownting python -m cownting.cli labels status \
  -c config/cownting.prod.yaml             # webhook configured? watermark? last runs?
docker compose exec -u cownting cownting python -m cownting.cli labels backup \
  -c config/cownting.prod.yaml             # manual run (--force skips the due-gate)
# also: labels export-csv OUT.csv | labels reconcile [--dataset X] | labels reseed --force
```

A `skipped` result means "not due" or "store busy" — normal, no action needed. A
`failed` run prints a `[cownting.alert] LABEL-BACKUP` line and holds the
watermark, so nothing is silently dropped from the next post.

**Keeping labels attached:**

- **After re-uploading an already-labeled day, run `labels reconcile`.** A
  re-ingest shifts bounding boxes; reconciliation re-attaches the existing labels
  to the new detections (non-destructively — it never deletes anything).
- **Never run `cownting migrate` once labeling has started.** It rewrites the
  identities labels attach to, and the rekey step is not wired — labels on the
  migrated partition would strand as orphans.
- **Mixed-vintage restore:** a `labels.duckdb` from a recent zip next to an
  *older* `data/` tar makes the next reconciliation report a wall of `hijacked`.
  That is the correct alarm and the labels are fine — restore a matching-vintage
  `data/` tar (or re-ingest), then reconcile again. Do **not** delete
  `data/labels.duckdb` over it.

### File ownership in `data/`

The app runs as uid **10001** inside the container, and `data/` is a host bind
mount — so a root-run host tool that writes into it (a manual copy, a
migration script) leaves files the app cannot overwrite, which surfaces later
as a 500 (`PermissionError`) on some unrelated save. Two layers of defence:

- **Prefer `docker compose exec -u cownting cownting …`** for maintenance that
  touches `data/` — it runs as the app user, so ownership stays correct (a
  plain `exec` enters as root, exactly the mistake this section is about).
- **The entrypoint self-heals on boot**: anything in `/app/data` not owned by
  the app user is re-owned before the server starts (then privileges drop to
  uid 10001). A root-owned stray at worst breaks things until the next
  `docker compose restart cownting`.

## Notes & tuning

- **CPU speed.** The default detector is `yolo11x-seg.pt` — accurate but slow on
  CPU. If uploads take too long, switch `detect.yolo_weights` in
  `config/cownting.prod.yaml` to `yolo11m-seg.pt` (and set the matching
  `YOLO_WEIGHTS` build arg in `docker-compose.yml`), then rebuild.
- **Moving to a GPU host later.** Swap the Dockerfile's CPU torch line for the
  CUDA wheels, use an `nvidia/cuda` base image, add `deploy.resources` /
  `gpus: all` in compose, and install the nvidia-container-toolkit on the host.
  `device: auto` already picks CUDA when present — no app changes needed.
- **Uploads are in-memory jobs.** A restart mid-processing loses the *job
  progress bar*, not the data (finished rows are durable in DuckDB); just
  re-upload that day if it was interrupted.

## Shipping segmentation outlines (M4 phase 0) and backfilling them

This release stores each detection's **instance outline** (`detections.mask_poly`,
`mask_parts`) so annotators can correct the model's actual mask instead of a
rectangle. Two independent steps: deploy the code, then backfill the outlines
onto footage processed before it. **The deploy is safe on its own** — until the
backfill runs, the Label page falls back to a box-shaped outline, which is
degraded but not broken.

### 1. Deploy (no manual migration)

```bash
git pull
docker compose up -d --build
docker compose logs -f cownting
```

`db.init_db` runs on every boot, so the two columns are added automatically —
there is no migration command. Verify they landed, on **both** tables:

```bash
docker compose exec -u cownting cownting python -c \
'from cownting import db; c=db.connect("data/cownting.duckdb", read_only=True); \
print([r[1] for r in c.execute("PRAGMA table_info(detections)").fetchall()][-2:]); \
print([r[1] for r in c.execute("PRAGMA table_info(clipped_detections)").fetchall()][-2:])'
```

Both must print `['mask_poly', 'mask_parts']`. If `clipped_detections` is missing
them the next camera clip raises — that table is created once by a CTAS and
these are the first columns ever added to `detections` after it existed.

### 2. Back up first — nothing does it for you

`data/cownting.duckdb` has **no** automatic backup (the weekly zip covers
`labels.duckdb` only, and it is off by default). The backfill issues one
`UPDATE` per detection with no surrounding transaction, so this copy is the only
undo:

```bash
docker compose stop cownting
cp -a data/cownting.duckdb ~/backups/cownting.duckdb.$(date +%F-%H%M)
docker compose start cownting
```

### 3. Backfill from the app, NOT from the CLI

```
POST /api/remask            (poweruser)   optional: ?dataset=&camera=&limit=
```

Progress appears on the dashboard as **"Tracing cow outlines"** and in
`GET /api/uploads` like any other job.

> **Do not run `cownting remask` against a live server.** DuckDB allows one
> read-write *process* per file and the pass holds a write handle for its whole
> duration, so the CLI does not slow the app down — it takes it **off the air**
> until it finishes: every request burns its ~9 s connect-retry budget and then
> 500s. The route runs the same pass *inside* the server process, where the
> handle is already ours and there is no contention.
>
> The CLI form stays correct for a **stopped** stack or a fresh box:
> ```bash
> docker compose stop cownting
> docker compose run --rm -T -u cownting cownting \
>   python -m cownting.cli remask -c config/cownting.prod.yaml
> docker compose start cownting
> ```
> (`run`, not `exec` — `exec` cannot reach a stopped container.)

**Budget the time.** This server is CPU-only, and the pass is one full
segmenter inference per frame with `yolo11x-seg` at `imgsz: 1280` — think
seconds per frame, so a large corpus is hours. It occupies the same serial queue
uploads use, so a day uploaded meanwhile waits behind it. The pass is
**resumable** (it only selects frames that still have a detection with no
outline), so run it in chunks and let uploads through between them:

```bash
curl -X POST 'https://<site>/api/remask?dataset=2025-07-03'   # one day at a time
```

Size the job and watch it finish:

```bash
docker compose exec -u cownting cownting python -c \
'from cownting import db; c=db.connect("data/cownting.duckdb", read_only=True); \
print(c.execute("SELECT count(*) AS dets, count(mask_poly) AS with_outline FROM detections").fetchone())'
```

The job's final message reports the **match rate**. Each re-predicted mask is
attached to an existing detection by IoU against its stored box, and **only the
two outline columns are written** — never the box, the score or the area, all of
which are identity material for the labels already collected. A rate below 80%
is called out explicitly: it means the current weights no longer reproduce these
detections, so `detect.yolo_weights` must match what the footage was originally
segmented with (`yolo11x-seg.pt`). Do not "speed up" the backfill with a smaller
model — the outlines would come from a different model than the boxes beside them.

### 4. Undo

Rolling back the *data* costs nothing structural — the columns are additive and
nullable:

```sql
UPDATE detections SET mask_poly = NULL, mask_parts = NULL;   -- app stopped
```

That returns the Label page to box-shaped outlines. If you roll back the *code*
after the columns exist, drop them in the same window: `restore_clip` on old code
would silently return clipped rows with their outlines NULLed, and a fresh
archive DB created by old code will not line up with a live table that has two
extra columns.
