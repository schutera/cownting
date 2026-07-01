# Cownting

Offline computer-vision pipeline for analyzing cows on a solar field from
prerecorded camera video. Built to run on this Mac (Apple Silicon / MPS), and
on CUDA or CPU unchanged.

**Pipeline:** `video → instance segmentation → facts (DuckDB) → dashboard`,
plus a Stage-2 orthophoto calibration that unlocks the spatial **heatmap**.

```
video file ─▶ ingest ─▶ segment ─▶ [calibrate in dashboard] ─▶ localize
                 │           │                                      │
            frames table  detections (masks→ground point,      world_x/y
                          posture, score, …)                   (orthophoto px)
                                       └────────────▶ Dash dashboard ◀───────┘
                              Overview · Segmentation · Heatmap · Calibration
```

The DuckDB `detections` table already carries every later-stage column
(`world_x/y`, `track_id`, `global_id`, `in_shade`, …), written NULL for now —
so identity, pose and shade are additive, not rewrites.

## Install

```bash
pip install -r requirements.txt        # ultralytics, duckdb, dash, ...
# optional open-vocabulary backend:
# pip install transformers "git+https://github.com/facebookresearch/sam2.git"
```

## Configure

```bash
cp config/cownting.example.yaml config/cownting.yaml
```
Point `cameras[0].video` at your file and set `start` (ISO time of the first
frame). Start with a single camera; add more entries later. The default
detector is **YOLO11-seg** (COCO `cow`) on `device: auto` (→ MPS here).

## Run

```bash
python -m cownting.cli ingest        # decode video → timestamped frames
python -m cownting.cli quality       # flag blind (lens-occluded) frames
python -m cownting.cli segment       # segment cows → detections + overlays
python -m cownting.cli kpis          # quick summary
python -m cownting.cli serve         # React app + API at http://127.0.0.1:8000
```

## Frontend (React + Tailwind v4, served by FastAPI)

The UI is a Vite/React app in `frontend/`, styled to the project style guide
(EB Garamond + Geist Mono, warm-on-cool palette, no border-radius, Recharts).
FastAPI (`cownting serve`) exposes the DuckDB data as JSON and serves the built app.

```bash
# one-time build (prod): FastAPI then serves frontend/dist at /
cd frontend && npm install && npm run build && cd ..
python -m cownting.cli serve                 # http://127.0.0.1:8000

# live dev (hot reload): two terminals
python -m cownting.cli serve                 # API on :8000
cd frontend && npm run dev                    # UI on :5173 (proxies /api → :8000)
```

The dashboard is a single page — KPIs, a big **occupancy heatmap** over the
orthophoto, activity trends, and a segmentation reviewer — plus a **Calibration**
tab that nudges you when a camera is uncalibrated. (The old Dash UI,
`cownting dashboard`, is superseded by `serve`.)

For the **heatmap**: set `paths.orthophoto` to a top-down site image, open the
**Calibration** tab, click ≥4 matched point pairs (camera frame ↔ orthophoto),
**Compute & save**, then:

```bash
python -m cownting.cli localize      # project detections onto the orthophoto
```

(After `pip install -e .` the commands are just `cownting ingest`, etc.)

## Backends

| backend         | what it is                                  | on this Mac |
|-----------------|---------------------------------------------|-------------|
| `yolo` (default)| YOLO11-seg, COCO `cow`, no training         | fast (MPS)  |
| `grounded_sam2` | Grounding-DINO + SAM2, open-vocab zero-shot | slower      |

Switch with `detect.backend` in the config. `grounded_sam2` is the bootstrap
labeler for the fine-tune loop below.

## Stage 1b — fine-tune / re-label loop
Zero-shot recall on distant/shade cows is marginal. The fix is a
bootstrap → correct → train loop that teaches a fast YOLO11-seg model this
specific scene. Runs best on a CUDA GPU — see **[docs/SETUP_WINDOWS.md](docs/SETUP_WINDOWS.md)**
for CUDA PyTorch, SAM2, and CVAT setup.

```bash
pip install -e ".[label,finetune]"   # + SAM2 from source (see the setup doc)

cownting label-select     # pick a diverse frame subset to correct
cownting label-export     # Grounded-SAM2 masks -> CVAT annotation task
# (correct masks in CVAT: add the cows the model missed, drop false positives)
cownting dataset-build    # pull corrections -> YOLO-seg dataset
cownting train            # fine-tune YOLO11-seg -> data/finetune/cownting-seg.pt
cownting eval-detect      # mask/box mAP on the held-out val split
```

Promote by pointing `detect.yolo_weights` at the new `.pt`, then re-run
`segment` + `spotcheck` to confirm count MAE actually improved.

## What's deliberately not here yet
Tracking/identity, pose (head-down grazing / lameness), shade-from-imagery,
multi-camera fusion. All reserved in the schema and config flags, none built.

## Quality check
Produce a small `frame_path,manual_count` CSV and run
`python -m cownting.cli spotcheck manual.csv` to see count MAE/bias — that tells
you whether the zero-shot detector is good enough or you should fine-tune.
