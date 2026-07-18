# Releases

Shipped work, newest first. Keep this current: when a future-work item lands,
move it here with the date it shipped and a one-line "what it means for the user".

## 2026-07-18 — Server deployment + resilient upload progress

- **Deploy on a server** — one-command Docker Compose stack (FastAPI app behind a
  Caddy auto-HTTPS proxy, CPU-only inference, a single `./data` volume for all
  state). See [DEPLOY.md](DEPLOY.md). Fixed two blockers found on the way: the web
  deps were installed-but-undeclared in `requirements.txt`, and a macOS-only
  Tailwind binary broke the Linux frontend build.
- **Upload progress survives anything** — a day's detection progress bar now
  reflects server-side work for *every* client, not just the tab that started it:
  refresh the page, open a second tab, or be another user and the running upload
  reconnects its bar. Progress also survives a server restart (an interrupted day
  is flagged so you can re-upload to finish it), and the upload shows an
  "Uploading…" state while the clips stream up.

## 2026-07-18 — First feedback round (16.07) response

Response to the first-feedback-round epics (data upload + model + interactive
analysis). Present in the working tree; dates reflect the working batch.

- **Accounts & access** — dashboard login and user management (scrypt-hashed
  users, session cookie, admin page, `/api` gated). First-boot `admin/admin`,
  configurable via `COWNTING_SECRET` / `ADMIN_*`; `cownting user passwd` for
  recovery.
- **Data upload** — upload footage from the dashboard: custom set of cameras
  with editable names, frames stored framewise in the database, and new frames
  auto-processed after upload so they show up in the frontend.
- **Capture-day from the stream** — each upload's day is read from the file's
  own metadata (container creation time → file mtime), never from pixels. Adds a
  day / dataset dimension so users can pick a day (data package) from the landing
  page. *(Caveat: Brinno zeroes the container time, so its day rides on file
  mtime.)*
- **Interactive data analysis (cross-filter)** — pick a dimension (posture,
  under-panel, head position, shade, …) and see ratios broken down by another,
  in either direction, over anything stored per detection.
- **Pose stage — M1 (flag-off)** — zero-shot ViTPose++ (AP-10K) runs inline on
  mask-zeroed crops; leg-ratio posture from pose can override the elongation
  proxy. Behind a flag, thresholds untuned — see [futurework.md](futurework.md)
  "Pose estimation for behaviour" for what remains.

## 2026-07-07 — Count-areas pivot & dashboard

- **Per-camera count areas** replace the removed camera calibration:
  hand-traced image-space polygons (dual polygons, aura badges + charts), which
  proved far more robust than the fragile polynomial warp.
- **Count-areas dashboard** — panel areas, day-series, collapsible KPIs.
- **Panel shade Phase 1** — static per-camera panel ground footprints;
  a cow is `under_panel` when its ground-contact point falls inside a footprint
  (image-space, calibration-free, configurable `shade.margin_px` band). Surfaces
  a `pct_sheltering` KPI and a shelter-vs-time-of-day trend.
- **Posture proxy** — standing/lying via mask-elongation proxy; 3-segment ring
  on the area map + resting sparkline. *(Superseded as the primary posture
  signal once the pose stage is tuned.)*

## 2026-07-04 — Calibration, scoring, tests (superseded)

- Fence/panel calibration, determination scoring, and tests. The metric
  calibration here was removed in the 2026-07-07 count-areas pivot.

## 2026-07-01 — Stage 1b

- Detector fine-tune / re-label pipeline; Windows/CUDA readiness.
