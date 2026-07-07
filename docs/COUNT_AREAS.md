# Count areas

Count areas replace the old orthophoto **heatmap** (and the whole calibration
stack it depended on). Instead of projecting every cow into a metric world
frame, you draw a handful of named **areas** per camera and Cownting counts how
many cows fall inside each one. No lens model, no ground-truth points, no
homography — just polygons you trace directly on the camera frame.

## The dual-polygon model

Each area carries **two** polygons that do two different jobs:

- **`camera_polygon`** — in **image pixels**, drawn on the camera frame. This is
  what actually **does the counting**. A detection is inside an area when its
  ground-contact point (`ground_px`, the bottom-centre of the cow's mask) falls
  inside this polygon, tested with an image-space even-odd ray cast
  (`point_in_polygon`). Calibration-free.
- **`ortho_polygon`** — in **orthophoto pixels**. **Display only.** It has no
  effect on counting; it just tells the dashboard where to place the area's badge
  on the top-down site image so the map reads spatially.

Counting happens entirely in image space; the ortho polygon is only for
placement.

## Data file

Areas live in `data/count_areas.json`:

```json
{
  "camera_01": [
    {
      "id": "north-lane",
      "name": "North lane",
      "camera_polygon": [[120, 340], [980, 320], [1010, 700], [90, 720]],
      "ortho_polygon":  [[210, 140], [460, 150], [470, 380], [200, 370]]
    }
  ],
  "camera_02": [ ... ]
}
```

- Top-level keys are `camera_id`s; each maps to a **list** of area objects.
- A missing file is treated as `{}` (no areas — the pipeline skips region
  assignment cleanly for every camera).

## region_id

Each detection gets a **`region_id`**, the composite string

```
region_id = f"{camera_id}::{area_id}"
```

e.g. `camera_01::north-lane`. This is globally unique across cameras, so counts
never collide between two cameras that happen to reuse an area slug. A detection
whose ground point is inside no area (or whose ground point is non-finite) has
`region_id = NULL`.

The frontend maps a `region_id` back to its area — and to its `ortho_polygon`
for placement — by splitting on `"::"`, looking up the camera key in
`getAreas()`, then finding the matching `area.id`.

## Editing areas — the gear icon

In the dashboard, each camera exposes a **gear icon** that opens the area editor
at `/count-area/:camera`. There you trace an area's footprint on the camera frame
(sets `camera_polygon`) and its counterpart on the orthophoto (sets
`ortho_polygon`), name it, and save. Saving `POST`s the whole
`count_areas.json` dict back and **re-runs `localize`**, so region assignment is
recomputed immediately for the existing detections.

## Dashboard — AreaMap and AreaTrends

- **AreaMap** renders the orthophoto with an **aura badge** per area, positioned
  by its `ortho_polygon`, showing the current cow count in that area. It gives an
  at-a-glance spatial read of where the herd is without a dense pixel heatmap.
- **AreaTrends** plots per-area counts **over time** (Recharts), one series per
  `region_id`, so you can see how occupancy of each area shifts through the day.

## API

- `GET /api/areas` → the `count_areas.json` dict (`camera → [area, …]`).
- `POST /api/areas` — body `{ "areas": <same dict> }` — saves the file and
  re-runs `localize`; returns `{ "ok": true }`.
- `GET /api/area-counts?frame=<int|null>&window=<int default 15>` →
  `{ "counts": { "<region_id>": <int>, … }, "frame": <int|null>, "window": <int> }`.
  With a `frame`, counts are windowed over frames `frame-window … frame`;
  without one, over all frames.
- `GET /api/area-counts/over-time?camera=<str|null>&trunc=<str default "hour">` →
  `{ "series": [ { "t": <iso>, "region_id": <str>, "cows": <int> }, … ] }`.

## Caveat — occupancy, not unique cows

A count is **per-frame occupancy**: how many cow *detections* sit inside an area
in the counted frame(s). It is **not** a count of unique animals. The same cow
seen in several frames is counted each time, and — at the ~1 frame/minute
time-lapse cadence — Cownting has no identity link between frames (see the ReID
work in `futurework.md`). Read area counts as "how busy is this area right now",
not "how many distinct cows visited it".
