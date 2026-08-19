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
