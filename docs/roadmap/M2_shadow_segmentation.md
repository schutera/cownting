I have everything I need. Here is the design.

---

# Workstream M2 — Shadow Segmentation (moving shade → `detections.in_shade`)

## 1. Goal & the anchor it copies

Fill the reserved, always-NULL `detections.in_shade` column with each cow's **actual** shade status. The exact code path to imitate is the static panel-footprint shelter test in `pipeline.localize`:

```
localize: assign_regions(ground_px, panel_polys, cam) -> db.update_shelter -> under_panel/panel_id
```

Shade uses the **same per-camera, image-space point-in-polygon** primitive (`scene.regions.assign_regions` / `point_in_polygon`, unchanged). The one structural difference: **panels are near-static, shadows move with the sun**, so shadow footprints are *time-indexed*. That single fact drives every new piece below.

**Crisp semantic split (must be preserved everywhere):**
| Column | Source | Meaning | Time |
|---|---|---|---|
| `under_panel` / `panel_id` | hand-traced polygons in `data/panel_areas.json` | ground point inside a panel's **ground footprint** | static |
| `in_shade` / `shade_id` (new) | machine-segmented shadow polygons per time-bucket | ground point inside the **cast shadow** at the cow's capture time | moves through the day |

A cow can be `under_panel=True, in_shade=False` (noon, narrow shadow directly under the row) or `under_panel=False, in_shade=True` (low sun, shadow thrown onto open ground) — that cross-tab is the agrivoltaics signal the feedback round asks for.

## 2. Where the model runs (offline/serve split — hard constraint)

- The **shadow segmenter runs only in the offline GPU batch**, as a new pipeline stage `shade`, between `segment` and `localize`. It writes shadow polygons to a **new DB table `shade_regions`** (precomputed artifact).
- `localize` stays **model-free**: it only *reads* `shade_regions` and does point-in-polygon. This is critical because `localize` is invoked from the serve box (`/api/localize`, `/api/areas`, `/api/panel-areas`). The small serve box never loads a seg model.
- If `shade.enabled=false` or no footprints exist for a dataset, `localize`'s shade block is a no-op and `in_shade` stays NULL — fully additive, exactly like `under_panel` being NULL for cameras with no panel areas.

## 3. Time-sampling design (the moving-shadow core)

Shadows move slowly and smoothly; per-frame segmentation is wasteful and noisy. Bucket **capture time** (`frames.ts` / `detections.ts`, already real solar wall-clock from the time-lapse ingest):

```python
# scene/regions.py (new helper)
def shade_bin(ts, bucket_minutes: float) -> int:
    return int(pd.Timestamp(ts).timestamp() // (bucket_minutes * 60))
```

- Default `shade.bucket_minutes = 15` → shadow moves modestly within a bucket; also denoises segmentation across the bucket's frames.
- Segment **one representative frame per (camera, shade_bin)** (e.g. the median-index frame in the bucket, reusing the `reference_frame` idea). Store that bucket's polygons.
- At `localize`, each detection maps `ts → shade_bin`, and is tested against **its own bucket's** polygons (nearest existing bin if that bin is empty).
- `bucket_minutes = 0` degrades to per-frame (keyed by `frame_path`) for maximal fidelity — same code path, finer key. Recommend 15 min default.
- **Overcast / night:** if a bucket yields no strong shadow (segmenter returns nothing, low sun elevation), write **no** footprint row → detections in that bucket get `in_shade = NULL` (unknown), *not* `False`. This mirrors the NULL-means-not-evaluated convention and keeps "no shadow to seek" out of the sun/shade ratio.

## 4. The shadow model itself (two backends, one artifact)

Both emit the identical `shade_regions` polygons, so all DB/API/frontend work is done once regardless of which backend matures.

**Backend A — classical CV baseline (`shade.backend="classical"`, ship first, effort M).** Shadows are low-luminance but chroma-preserving regions on the ground: in Lab/HSV, shadow pixels drop L/V while hue stays ~constant and normalized saturation rises vs. a sunlit reference. Because panels are near-static and the ground is mostly static, build a per-camera daytime **illumination reference** (e.g. a bright-quantile composite across the day) and flag pixels significantly darker at preserved hue. Confounders handled by: (a) **masking out cow instances** (re-run the cow segmenter in the shade stage, or subtract detection bboxes for that frame — see Open Decision 7), (b) optional restriction to below-horizon ground rows, (c) morphological open/close + `min_area_px`. Contour → `approxPolyDP` → polygons. Unblocks the entire plumbing with zero labeling.

**Backend B — trained shadow class (`shade.backend="yolo"`, effort L).** Add a `shadow` class via the existing Stage 1b loop: Grounded-SAM2 prompt `"shadow on the ground."` → correct in CVAT (`label_field="shadow"`) → fine-tune a YOLO-seg. Reuses `cownting/finetune/*` almost verbatim. More robust to clouds/overexposure. Same polygon output.

**Backend C — geometric (`shade.backend="geometric"`, deferred).** Sun az/elev from lat/lon+ts (`astral`/`pvlib`) + panel tilt projected to a ground shadow. **Blocked**: Cownting is calibration-free — there is no image↔ground map to place a world-space shadow into image pixels. Keep as an open item tied to the dropped calibration, not the primary path. (lat/lon still useful to gate night/low-sun buckets.)

New module `cownting/detect/shade.py`:
```python
class ShadowSegmenter(Protocol):
    def shadows(self, image_bgr) -> list[np.ndarray]:   # list of Nx2 polygons (image px)

def build_shadow_segmenter(shade_cfg, device) -> ShadowSegmenter   # classical|yolo|geometric
def mask_to_polygons(mask, min_area_px, epsilon) -> list[np.ndarray]  # cv2 findContours+approxPolyDP
```

## 5. Exact files / functions to touch

### `cownting/config.py` — effort S
- New `class ShadeCfg(BaseModel)`: `enabled: bool = False`, `backend: Literal["classical","yolo","geometric"] = "classical"`, `bucket_minutes: float = 15.0`, `margin_px: float = 0.0` (boundary band, parity with panel `shade.margin_px` idea in futurework), `min_area_px: int = 2000`, `poly_epsilon: float = 3.0`, `yolo_shadow_weights: str = "cownting-shadow-seg.pt"`, classical params (`darken_ratio`, `hue_tol`, `ref_quantile`), geo params (`latitude`, `longitude`, `min_sun_elevation_deg`).
- Add `shade: ShadeCfg = Field(default_factory=ShadeCfg)` to `Config`.
- `PathsCfg`: optional `shade_debug_dir: str = "data/artifacts"` (canonical store is the DB table; JSON dump only for inspection).

### `cownting/scene/regions.py` — effort S
- Add `shade_bin(ts, bucket_minutes)` helper (above). `assign_regions` / `point_in_polygon` reused unchanged.

### `cownting/detect/shade.py` (new) + `cownting/detect/__init__.py` — effort M (classical) / L (trained)
- `ShadowSegmenter`, `build_shadow_segmenter`, `ClassicalShadowSegmenter`, `YoloShadowSegmenter`, `mask_to_polygons`. Export `build_shadow_segmenter` from `detect/__init__.py`.

### `cownting/pipeline.py` — effort M
- **New stage** `def shade(config, dataset_id=None) -> int`: guard on `config.shade.enabled`; build shadow segmenter (GPU only); for each camera, for each `shade_bin` over processed frames, pick a representative frame, `seg.shadows(img)`, convert to area dicts `{"id": f"shadow-{bin}-{k}", "camera_polygon": poly}`; `db.purge_shade_footprints(con, dataset_id)` then `db.upsert_shade_footprints(...)`. Optional second overlay pass drawing shadow contours.
- **`localize`**: 
  - extend the reset (line 124) to also clear shade:
    `UPDATE detections SET region_id=NULL, under_panel=NULL, panel_id=NULL, in_shade=NULL, shade_id=NULL{reset_scope}`
  - after the panel block, add the shade block (reuses `assign_regions`, groups by bucket):
    ```python
    shade_fp = db.load_shade_footprints(con, dataset_id)  # {(cam, bin): [area dicts]}
    if shade_fp:
        for camera_id in {c for c, _ in shade_fp}:
            d = con.execute("SELECT detection_id, ground_px_x, ground_px_y, ts "
                            f"FROM detections WHERE camera_id=?{scope}", [camera_id]+dsp).df()
            if d.empty: continue
            d["bin"] = d["ts"].map(lambda t: shade_bin(t, config.shade.bucket_minutes))
            for b, grp in d.groupby("bin"):
                polys = shade_fp.get((camera_id, b)) or _nearest_bin(shade_fp, camera_id, b)
                if not polys: continue        # no map -> leave in_shade NULL
                sids = assign_regions(grp[["ground_px_x","ground_px_y"]].to_numpy(), polys, camera_id)
                grp = grp.assign(in_shade=[s is not None for s in sids], shade_id=sids)
                db.update_shade(con, grp[["detection_id","in_shade","shade_id"]])
    ```
- **`process`**: insert `shade(config)` between `segment` and `localize`, gated by `config.shade.enabled`.

### `cownting/db.py` — effort M
- `init_db`: create `shade_regions(dataset_id, camera_id, shade_bin BIGINT, bin_ts TIMESTAMP, shade_id VARCHAR, polygon VARCHAR, created_at TIMESTAMP)`; forward-compat `ALTER TABLE detections ADD COLUMN IF NOT EXISTS shade_id VARCHAR` (`in_shade` already exists).
- `upsert_shade_footprints(con, df)` (polygons as JSON text); `purge_shade_footprints(con, dataset_id)` (idempotent replace, mirrors `purge_dataset`); `load_shade_footprints(con, dataset_id) -> dict[(cam,bin), list[area dict]]`.
- `update_shade(con, df[detection_id, in_shade, shade_id])` — exact mirror of `update_shelter`.
- `kpi_summary`: add `(SELECT count(*) FROM detections WHERE in_shade{dp}) AS shaded`, plus a cross count `under_panel AND in_shade`; return `shaded`, `pct_shaded`, and `pct_shaded_of_sheltered`.
- `day_series`: add `count(...) FILTER (WHERE d.in_shade) AS shaded` (and `WHERE d.in_shade = false AS sunlit`).
- `area_summary` + `area_counts_whole_day`: add `... FILTER (WHERE in_shade) AS shaded` (peak variant in the whole-day fn).
- `shelter_over_time`: add `count(*) FILTER (WHERE in_shade) AS shaded` — one bucketed series now carries both footprint-occupancy and actual-shade.
- `export_df` already emits `in_shade` (line 504); add `d.shade_id`.

### `cownting/api.py` — effort M
- `/api/site` → `kpis` passes through automatically once `kpi_summary` adds fields (only the TS type needs the new keys).
- `/api/shelter`: response now also carries `shaded` per bucket (from extended `shelter_over_time`). Keep endpoint name; document the two distinct series.
- `/api/area-counts`: add a `shaded` map (`{region_id: n}`) alongside the existing `sheltering` map, in both the whole-day and per-frame branches.
- `/api/day-series`: add `shaded` (and `sunlit`) arrays to the keys list.
- **New** `/api/shade-footprints?camera=&frame=` (or `bin=`): returns the shadow polygons active at that frame's bucket, so the AreaMap / seg overlay can draw the **moving shade outline**. Reads `shade_regions` only (model-free).
- Optional **new** `/api/cross?by=in_shade&split=posture` (or under_panel) — powers the feedback round's interactive cross-tab ("under panels ratio by shade", "posture ratio by shade").

### Frontend `frontend/src/` — effort M (KPIs) + L (map viz + cross-tab)
- `lib/types.ts`: `Kpis` += `shaded, pct_shaded`; `AreaSummaryRow` += `shaded`; `DaySeries` += `shaded, sunlit`; `AreaCounts` += `shaded`.
- `lib/api.ts`: extend `AreaCounts`; add `getShadeFootprints`, optional `getCross`.
- `lib/palette.ts`: add `SHADE_COLOR` **distinct from `SHELTER_COLOR`** (teal already = footprint/shelter; make shade a deeper indigo/slate = "actual cast shadow"). This is the visual encoding of static-footprint vs moving-shade — do not reuse the teal.
- `components/KpiPanel.tsx`: add a third split bar **"In real shade vs. sun"** under the existing "Under panels vs. open", with copy that names the distinction ("under panels" = footprint; "in shade" = actual shadow now). Repeat in `AreaBreakdown` per area.
- `components/AreaMap.tsx`: add a shade indicator per area (second block row using `SHADE_COLOR`, from the new `shaded` map) and, when a frame is scrubbed, draw the shadow polygon outline from `/api/shade-footprints` — literally showing the shadow sweep across the day.
- `components/DaySeries.tsx`: add a `shaded` strip beside `sheltering`.
- `components/CameraSegStack.tsx` / `detect/overlay.py`: optional — `render_overlay` gains a `shadow_polys` param so the offline seg view shows the cast shadow; requires the `shade` stage to do the overlay pass (overlays are otherwise written in `segment`, before shadows exist).
- **New** cross-tab widget (feedback item): pick a primary (posture / under_panel / in_shade) and view the ratio of another within it. Effort L.

### `tests/` — effort S/M
- `tests/test_shade.py` (mirror `test_shelter.py` + `test_panel_areas.py`): `shade_bin` bucketing; `upsert/load_shade_footprints` round-trip; `update_shade` targeting + empty no-op; a point that is `under_panel` but **not** `in_shade` and vice-versa (proves the distinction); `kpi_summary` `pct_shaded`; NULL-when-no-footprint (overcast) behavior. Register in `tests/__main__.py` (gated by the serve test gate).

## 6. Effort summary

| Piece | Effort |
|---|---|
| Config `ShadeCfg` | S |
| `shade_bin` helper | S |
| DB: table + upsert/load/purge/update_shade + query extensions | M |
| Classical shadow segmenter + `mask_to_polygons` | M |
| Trained `shadow` YOLO class (Stage-1b loop) | L |
| Pipeline `shade` stage + `localize` shade block + `process` wiring | M |
| API endpoints + passthrough | M |
| Frontend KPI split + palette + types | M |
| AreaMap moving-shade viz + overlay contours | L |
| Interactive cross-tab | L |
| Tests | S–M |
| **Workstream total** | **L** (classical + KPIs), **XL** if trained model + full map viz + cross-tab all in scope |

## 7. Risks

- **Serve box must stay model-free.** The one real architectural risk: someone wires the segmenter into `localize`. Mitigation: `shade` is a distinct offline stage; `localize` only reads `shade_regions`. A future upload→auto-process worker must run on the GPU box (or skip `shade`); footprints always originate from the GPU batch.
- **Classical shadow confounders** — cow bodies (dark), cloud shadows, panel structure, overexposure, long low-sun shadows merging. Mitigate by masking cow instances, illumination-reference differencing (chroma-preserving darkening only), morphology + `min_area_px`. The trained class is the durable fix.
- **Geometric path is blocked by calibration-free design** — no image↔ground map. Don't bet the workstream on it.
- **Bucket-edge mismatch** — a detection near a bucket boundary tested against a slightly-stale shadow. Mitigate with smaller buckets or `bucket_minutes=0` (per-frame). Interpolating polygons is overkill.
- **Single-point proxy** — `in_shade` tests the ground-contact point, same convention as `under_panel`; a partially-shaded cow reads as one bit. Document like the posture proxy.
- **NULL vs FALSE discipline** — overcast/night/no-map buckets must stay `in_shade = NULL`, else the sun/shade ratio is polluted by "no shadow existed." Enforced by only writing rows where a footprint exists.
- **Idempotency** — `shade` must `purge_shade_footprints` for the dataset before writing, and `localize`'s reset must clear `in_shade/shade_id`, or re-runs leave stale shade.
- **Overlay ordering** — overlays are written during `segment`, before shadows exist; drawing shadow contours needs a second pass in `shade`.
- **Fisheye** — non-issue: polygons are image-space, distortion handled implicitly (same as panel footprints).

## 8. Open decisions

1. **Backend priority** — ship classical to unblock plumbing, then trained `shadow` class? (Recommended.) Geometric only if calibration returns.
2. **Bucket width** — 15 min default vs per-frame (`bucket_minutes=0`). 
3. **Footprint storage** — DB table `shade_regions` (recommended; keeps serve model-free, dataset-scoped, machine-produced) vs a `data/shade_areas.json` mirroring `panel_areas.json` (rejected: it's time-indexed and per-dataset, not hand-traced).
4. **`shade_id` column** — add for parity/debug (recommended) or boolean-only?
5. **Cow-instance masking in the classical path** — re-run the cow segmenter inside `shade` (simple, costs a second pass) vs persist masks from `segment` vs subtract detection bboxes (cheap, coarse). Leaning re-run for a clean ground.
6. **`in_shade` independent of `under_panel`** — confirmed independent; the cross-tab is the point.
7. **Training data** — reuse the Stage-1b CVAT loop with a `shadow` label, or a separate annotation project?
8. **Night/low-sun gating** — use `astral` + lat/lon `min_sun_elevation_deg` to skip buckets (write no footprint → NULL) vs rely on the segmenter returning empty.
9. **Overlay** — show the moving shadow polygon in the seg view now, or defer? (Cheap once `shade` runs; strong demo value.)
10. **Where the shade stage sits in a future auto-process-on-upload worker** — GPU box only; the small server never runs it.

**Files touched (absolute):** `/Users/markschutera/PycharmProjects/Cownting/cownting/config.py`, `.../cownting/scene/regions.py`, `.../cownting/detect/shade.py` (new), `.../cownting/detect/__init__.py`, `.../cownting/detect/overlay.py`, `.../cownting/pipeline.py`, `.../cownting/db.py`, `.../cownting/api.py`, `.../cownting/cli.py` (optional `shade` subcommand mirroring `localize`), `.../frontend/src/lib/{types.ts,api.ts,palette.ts}`, `.../frontend/src/components/{KpiPanel.tsx,AreaMap.tsx,DaySeries.tsx,CameraSegStack.tsx}`, `.../tests/test_shade.py` (new) + `.../tests/__main__.py`.