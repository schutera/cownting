# syntax=docker/dockerfile:1

# =====================================================================
# Stage 1 — build the React/Vite frontend into frontend/dist
# =====================================================================
FROM node:20-bookworm-slim AS frontend
WORKDIR /web

# Install deps from the lockfile first (cached unless the lockfile changes).
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci

# Build the SPA. FastAPI mounts the resulting /web/dist at "/".
COPY frontend/ ./
RUN npm run build


# =====================================================================
# Stage 2 — Python runtime that serves the API + the built frontend
# =====================================================================
FROM python:3.11-slim-bookworm AS runtime

# OpenCV needs libGL + glib; ffmpeg gives OpenCV robust video decoding for the
# ingest step (mp4/mov/avi/mkv). curl is used by the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# CPU-only torch FIRST, from the PyTorch CPU wheel index, so ultralytics doesn't
# drag in the multi-GB CUDA build. Swap this line for the CUDA wheels if you move
# to a GPU host (and use an nvidia/cuda base image).
RUN pip install --index-url https://download.pytorch.org/whl/cpu torch torchvision

# Application Python dependencies.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application source (run from /app via `python -m`, NOT pip-installed, so that
# api.py resolves frontend/dist relative to /app).
COPY cownting/ ./cownting/
COPY config/ ./config/

# Built frontend from stage 1.
COPY --from=frontend /web/dist ./frontend/dist

# Pre-fetch the detector weights so the first upload doesn't need network egress.
# Must match detect.yolo_weights in config/cownting.prod.yaml.
ARG YOLO_WEIGHTS=yolo11x-seg.pt
RUN python -c "from ultralytics import YOLO; YOLO('${YOLO_WEIGHTS}')"

# Run as an unprivileged user; the data volume + weights are owned by it.
RUN useradd --create-home --uid 10001 cownting \
    && mkdir -p /app/data \
    && chown -R cownting:cownting /app
USER cownting

EXPOSE 8000

# "/" returns the SPA index (no auth gate) once the app is up.
HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD curl -fsS http://localhost:8000/ >/dev/null || exit 1

# --skip-tests: the pre-boot test gate is for dev; CI/build validates the suite,
# so prod boots don't depend on the tests/ dir (which isn't shipped in the image).
CMD ["python", "-m", "cownting.cli", "serve", \
     "--config", "config/cownting.prod.yaml", \
     "--host", "0.0.0.0", "--port", "8000", "--skip-tests"]
