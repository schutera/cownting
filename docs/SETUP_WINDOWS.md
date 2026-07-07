# Windows + CUDA setup & the Stage 1b fine-tune loop

Moving Cownting to the Windows/CUDA box is the right call: training YOLO11-seg at
`imgsz=1280` is the GPU-hungry step, and a real NVIDIA GPU is far faster than MPS.
The code is device-agnostic (`detect.device: auto` → `cuda > mps > cpu`), so nothing
in the pipeline changes — you just install the CUDA build of PyTorch.

---

## 0. Get the code and the data onto the box

**Code** comes via git. **Data does not** — videos, frames, weights, the DuckDB
file, and the orthophoto are all `.gitignore`d and must be copied out-of-band
(USB / network share / `scp` / cloud drive):

```
data/<camera_N>/*.MP4        # source videos
data/orthophoto.png          # for the AreaMap placement (optional for Stage 1b)
data/count_areas.json        # traced count areas (optional; git-tracked if you keep it)
config/cownting.yaml         # your machine-specific config (also gitignored)
```

You can either copy the already-ingested `data/artifacts/` + `data/cownting.duckdb`,
or just copy the videos and re-run `ingest` + `quality` + `segment` on the GPU box
(faster there anyway). The re-label loop only needs the frames + DB.

```powershell
git clone <your-repo-url> Cownting
cd Cownting
# ...copy the data/ files described above into place...
copy config\cownting.example.yaml config\cownting.yaml   # then edit camera paths
```

## 1. Python env + CUDA PyTorch

Do **not** rely on the torch that `ultralytics` pulls by default — it is often the
CPU build on Windows. Install the CUDA wheel first, then the rest:

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip

# CUDA 12.4 wheel (pick the index matching your driver: cu121 / cu124 / ...)
pip install torch torchvision --index-url https://download.pytorch.org/whl/cu124

pip install -e .              # core deps (ultralytics, duckdb, typer, ...)
```

Verify the GPU is visible:

```powershell
python -c "import torch; print(torch.cuda.is_available(), torch.cuda.get_device_name(0))"
```

`detect.device: auto` will now resolve to `cuda` automatically.

## 2. Optional deps for the re-label loop

```powershell
pip install -e ".[label,finetune]"     # fiftyone + transformers (Grounding-DINO) + matplotlib
```

**SAM2** is not on PyPI — install from source (needs Git + the "Desktop development
with C++" workload from the VS Build Tools for the CUDA extension compile):

```powershell
pip install "git+https://github.com/facebookresearch/sam2.git"
```

Download a SAM2 checkpoint + point the config at it. `checkpoints/` and `configs/`
are gitignored:

```powershell
mkdir checkpoints
curl -L -o checkpoints\sam2.1_hiera_small.pt ^
  https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt
```

`detect.sam2_cfg` (`configs/sam2.1/sam2.1_hiera_s.yaml`) resolves inside the installed
`sam2` package — no local file needed. Grounding-DINO-tiny auto-downloads from
Hugging Face on first use.

## 3. CVAT (annotation server) via Docker

CVAT runs in Docker Desktop on Windows (enable the WSL2 backend):

```powershell
git clone https://github.com/cvat-ai/cvat
cd cvat
docker compose up -d
# create a superuser once:
docker exec -it cvat_server bash -ic "python3 ~/manage.py createsuperuser"
```

CVAT is then at `http://localhost:8080` — matches `label.cvat_url`. Put those same
credentials where FiftyOne expects them (env vars `FIFTYONE_CVAT_USERNAME` /
`FIFTYONE_CVAT_PASSWORD`, or in `~/.fiftyone/annotation_config.json`).

---

## The Stage 1b loop

```powershell
# (pipeline stages, if not copied over)
cownting ingest
cownting quality
cownting segment                 # zero-shot baseline: gives selection its density buckets

# --- fine-tune / re-label ---
cownting label-select            # -> data/labeling/selected.{txt,csv}
cownting label-export            # Grounded-SAM2 masks -> CVAT task 'cownting_stage1b'
#   >>> correct masks in CVAT: add the distant/shade cows the model missed
#       (the recall fix), delete condensation false-positives, tighten masks <<<
cownting dataset-build           # pull corrections -> data/finetune/dataset/{images,labels}
cownting train                   # fine-tune -> data/finetune/cownting-seg.pt
cownting eval-detect             # mask/box mAP on the val split
```

**Promote** the weights only if they beat the baseline: set

```yaml
detect:
  yolo_weights: data/finetune/cownting-seg.pt
```

then re-run the operational check:

```powershell
cownting segment                 # re-detect all frames with the fine-tuned model
cownting spotcheck manual.csv    # count MAE / bias vs your hand counts — did recall improve?
```

If it improved, re-run `localize` (re-assigns count-area `region_id` + panel
shelter on the new detections) and you're done. If not, iterate: correct more
frames (especially the failure cases), or harden the selection, and retrain.

### Notes
- `finetune.batch` is set to 4 (safe on 12 GB at 1280). Bump it on a bigger GPU.
- Train/val split is **by time-block** so adjacent near-duplicate minutes don't
  leak across the split — keep `split_by_time_block: true`.
- Everything except the CVAT correction step is scripted; that one step is the human
  labor (~150 frames ≈ a couple of hours) and is where recall actually gets fixed.
