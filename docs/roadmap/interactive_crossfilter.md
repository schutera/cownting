Design grounded in the real code. Here it is.

---

# Cross-Filter Analysis — design spec

Turn the static strips below the heatmap into an interactive pivot: pick a **primary** dimension and a **breakdown** dimension, see the ratio, swap them, and pivot across *any* stored per-detection feature. The whole thing hangs off one generic feature registry so a new feature (shade, head-pose, …) is one entry per side, no rewrite.

## 0. The core realization

The current `DaySeries` is already a crosstab — it's `GROUP BY frame_idx × {posture | under_panel}` rendered as share strips (`db.day_series` computes exactly `standing/lying` and `sheltering/open` per frame). So **everything the user wants is one generalization**: `GROUP BY <primaryExpr> × <breakdownExpr>`. When the primary is *temporal* (per-frame / hour) we render strips (and keep click-to-scrub); when it's *categorical* (posture, area, camera) we render stacked bars. One endpoint, one db helper, two render paths.

That means we don't throw away the "great" plots — we subsume them.

---

## 1. The generic feature abstraction

A **feature** = a categorical bucket derivable from a `detections` row via a *whitelisted* SQL expression, plus display metadata. It lives in **two** registries that agree on `key`:

- **Backend** owns the SQL (the injection boundary).
- **Frontend** owns label + color + render kind.
- A tiny `/api/features` endpoint reports *availability* (is the column populated yet?), so reserved features (`in_shade`, future `head_pose`) auto-light-up when data arrives — zero code change.

### 1a. Backend registry — new file `/Users/markschutera/PycharmProjects/Cownting/cownting/features.py`

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class FeatureSpec:
    key: str            # stable id used in the URL/query — matches frontend key
    sql: str            # trusted SQL expr producing the bucket (NEVER from user input)
    kind: str           # 'categorical' | 'temporal_frame' | 'temporal_hour'
    needs_frames: bool  # True -> must JOIN frames (frame_idx lives there)
    fixed_domain: tuple[str, ...] | None = None   # None -> domain derived from data
    avail_col: str | None = None  # column whose non-null count gates availability

# The whitelist. `d` = detections alias, `f` = frames alias.
FEATURES: dict[str, FeatureSpec] = {
    "posture": FeatureSpec("posture", "coalesce(d.posture, 'unknown')",
                           "categorical", False,
                           ("standing", "lying", "unknown"), "posture"),
    "panel":   FeatureSpec("panel",
                           "CASE WHEN d.under_panel THEN 'under panel' "
                           "WHEN d.under_panel = false THEN 'open' ELSE 'unknown' END",
                           "categorical", False,
                           ("under panel", "open", "unknown"), "under_panel"),
    "shade":   FeatureSpec("shade",       # RESERVED — avail=False until in_shade fills
                           "CASE WHEN d.in_shade THEN 'shade' "
                           "WHEN d.in_shade = false THEN 'sun' ELSE 'unknown' END",
                           "categorical", False,
                           ("shade", "sun", "unknown"), "in_shade"),
    "region":  FeatureSpec("region", "d.region_id", "categorical", False,
                           None, "region_id"),
    "camera":  FeatureSpec("camera", "d.camera_id", "categorical", False, None),
    "hour":    FeatureSpec("hour", "cast(extract('hour' FROM d.ts) AS INTEGER)",
                           "temporal_hour", False),
    "frame":   FeatureSpec("frame", "f.frame_idx", "temporal_frame", True),  # DaySeries axis
    # future: "pose": FeatureSpec("pose","d.head_pose","categorical",False,None,"head_pose"),
}

def resolve(key: str) -> FeatureSpec:
    try:
        return FEATURES[key]
    except KeyError:
        raise ValueError(f"unknown feature {key!r}")   # -> 400, never interpolated
```

**Adding a feature later = one dict entry here** (+ one on the frontend). Availability is computed from `avail_col` so `shade`/`pose` cost nothing until their column is written.

### 1b. Frontend registry — new file `/Users/markschutera/PycharmProjects/Cownting/frontend/src/lib/features.ts`

```ts
import { ACTIVE_COLOR, REST_COLOR, SHELTER_COLOR, OPEN_COLOR,
         ACCENT_COLORS, HEAT, cameraColor } from "./palette";

export type FeatureKind = "categorical" | "temporal_frame" | "temporal_hour";

export interface FeatureDef {
  key: string;
  label: string;                       // "Posture", "Under panels", "Count area"
  kind: FeatureKind;
  /** stable value->color; falls back to index ramp for dynamic domains */
  colorFor: (value: string, domain: string[], cameras: string[]) => string;
  dropUnknown?: boolean;               // default hide the 'unknown' bucket for binary splits
}

const UNKNOWN = "#c9c2b4";             // CHART_MUTED grey

export const FEATURES: FeatureDef[] = [
  { key: "posture", label: "Posture", kind: "categorical", dropUnknown: true,
    colorFor: v => v === "standing" ? ACTIVE_COLOR : v === "lying" ? REST_COLOR : UNKNOWN },
  { key: "panel", label: "Under panels", kind: "categorical", dropUnknown: true,
    colorFor: v => v === "under panel" ? SHELTER_COLOR : v === "open" ? OPEN_COLOR : UNKNOWN },
  { key: "shade", label: "Shade", kind: "categorical", dropUnknown: true,
    colorFor: v => v === "shade" ? SHELTER_COLOR : v === "sun" ? OPEN_COLOR : UNKNOWN },
  { key: "region", label: "Count area", kind: "categorical",
    colorFor: (v, dom) => ACCENT_COLORS[dom.indexOf(v) % ACCENT_COLORS.length] },
  { key: "camera", label: "Camera", kind: "categorical",
    colorFor: (v, _d, cams) => cameraColor(cams, v) },     // color == same camera everywhere
  { key: "hour", label: "Hour of day", kind: "temporal_hour", colorFor: () => HEAT },
  { key: "frame", label: "Time of day", kind: "temporal_frame", colorFor: () => HEAT },
];

export const featureByKey = (k: string) => FEATURES.find(f => f.key === k)!;
```

Presentation stays in the design layer; SQL stays in the security layer; the shared `key` string is the only contract. A one-line test (`frontend keys ⊆ backend keys`) prevents drift.

---

## 2. Backend contract

### 2a. New endpoint (in `cownting/api.py`)

```
GET /api/crosstab?primary=<key>&breakdown=<key>&dataset=<id|all>&camera=<id?>&frame=<idx?>
```

`breakdown` optional → 1-D distribution of `primary`. Returns **long-form** cells (easiest to pivot in Recharts and to compute ratios) plus ordered domains so bar order and colors are stable:

```jsonc
{
  "primary": "posture",
  "breakdown": "panel",
  "primary_domain":   ["standing", "lying", "unknown"],
  "breakdown_domain": ["under panel", "open", "unknown"],
  "cells": [
    { "primary": "standing", "breakdown": "under panel", "n": 1234 },
    { "primary": "standing", "breakdown": "open",        "n": 5678 },
    { "primary": "lying",    "breakdown": "under panel", "n": 900 }
  ],
  "primary_totals": { "standing": 6912, "lying": 3100 },
  "total": 20012
}
```

```python
@app.get("/api/crosstab")
def crosstab(primary: str, breakdown: str | None = None,
             dataset: str | None = None, camera: str | None = None,
             frame: int | None = None):
    try:
        c = con()
        ds = None if dataset == "all" else (dataset or db.latest_dataset(c))
        df, pdom, bdom = db.crosstab(c, primary, breakdown,
                                     dataset_id=ds, camera_id=camera, frame=frame)
    except ValueError as e:
        raise HTTPException(400, str(e))
    finally:
        c.close()
    return { "primary": primary, "breakdown": breakdown,
             "primary_domain": pdom, "breakdown_domain": bdom,
             "cells": _records(df),
             "primary_totals": df.groupby("primary")["n"].sum().to_dict(),
             "total": int(df["n"].sum()) }

@app.get("/api/features")
def features():
    c = con()
    avail = db.available_features(c)   # {key: bool} from non-null coverage
    c.close()
    return [ {"key": k, "kind": s.kind, "available": avail.get(k, True)}
             for k, s in features_mod.FEATURES.items() ]
```

### 2b. New db helpers (in `cownting/db.py`)

```python
def crosstab(con, primary: str, breakdown: str | None = None, *,
             dataset_id: str | None = None, camera_id: str | None = None,
             frame: int | None = None) -> tuple[pd.DataFrame, list[str], list[str]]:
    """Long-form GROUP BY of two whitelisted feature expressions.
    Returns (df[primary, breakdown, n], primary_domain, breakdown_domain).

    Feature keys are resolved through features.FEATURES — their SQL comes ONLY
    from that trusted registry, never from the argument string (injection guard).
    Uses count(d.detection_id) (not count(*)) so a LEFT JOIN's zero-cow frames
    count as 0, mirroring day_series. Joins frames only when a temporal_frame
    feature or a `frame` filter needs frame_idx."""

def available_features(con) -> dict[str, bool]:
    """{feature_key: has_any_non_null_data} — gates reserved features (in_shade,
    head_pose) in the UI until their column is populated."""
```

SQL sketch (built only from registry pieces + parameterized filters):

```sql
SELECT {p.sql} AS primary, {b.sql} AS breakdown, count(d.detection_id) AS n
FROM detections d
{LEFT JOIN frames f ON d.camera_id=f.camera_id AND d.frame_path=f.frame_path}  -- iff needs_frames
WHERE 1=1
  {AND d.region_id IS NOT NULL}      -- region feature drops nulls
  {AND d.dataset_id = ?}             -- dataset scope (parameterized)
  {AND d.camera_id = ?}             -- optional camera filter
  {AND f.frame_idx = ?}             -- optional 'at this moment'
GROUP BY 1, 2
```

- **Domains**: `fixed_domain` from the registry when present (posture/panel/shade); otherwise derived from result values ordered by total-desc (region/camera) or ascending (hour/frame).
- `frame` primary reproduces `day_series` exactly when `breakdown ∈ {posture, panel}` — the LEFT JOIN + `count(d.detection_id)` keeps zero-cow frame slots so the strip axis stays continuous and click-to-scrub still lands on a real frame_idx.

---

## 3. The UI

Component tree (new components in `/Users/markschutera/PycharmProjects/Cownting/frontend/src/components/`):

```
CrossFilter.tsx                      ← occupies DaySeries' slot under the hero
├─ header:   SectionLabel "BREAKDOWN"  +  mode Chips [ Share · 100% · Count ]
├─ PickerRow
│   ├─ DimensionPicker  (primary)     "Show  ⟨Posture⟩"
│   ├─ SwapButton  ⇄                   (swaps primary⇄breakdown, disabled if breakdown=none)
│   └─ DimensionPicker  (breakdown)   "by  ⟨Under panels⟩ / none"
└─ body — switch on featureByKey(primary).kind
    ├─ categorical      → CrosstabBars.tsx    (Recharts stacked / 100%-stacked bars)
    └─ temporal_*       → ShareStripSet.tsx   (the existing strip UI, now data-driven, keeps onFrame scrub)
```

New files:
- `src/components/CrossFilter.tsx` — orchestrator: holds `{primary, breakdown, mode}` local state, fetches, memoizes the pivot, routes to the renderer.
- `src/components/DimensionPicker.tsx` — dropdown/segmented control built from `Chip` (reused from `ui.tsx`); unavailable features render as disabled chips ("Shade — coming soon").
- `src/components/CrosstabBars.tsx` — Recharts renderer for categorical primary.
- `src/components/ShareStripSet.tsx` — the strip renderer extracted from `DaySeries.tsx` (`ShareStrip`, `Hover`, `idxFromEvent`, `Dot` move here verbatim, generalized from 2 fixed segments to N breakdown values).

### 3a. `CrosstabBars` — the Recharts piece

Pivot long cells → wide rows (one row per primary value, one numeric column per breakdown value), then one `<Bar>` per breakdown value. **100%-stacked is native** via `stackOffset` — the mode chip just toggles it, no refetch:

```tsx
// mode: "share" | "expand"(=100%) | "count"
<ResponsiveContainer width="100%" height={Math.max(160, primaryDomain.length * 44)}>
  <BarChart data={wide} layout="vertical"                 // horizontal bars: long area names read
            stackOffset={mode === "expand" ? "expand" : "none"}
            margin={{ top: 4, right: 8, bottom: 0, left: 8 }}>
    <CartesianGrid stroke="#2e5a6a" strokeOpacity={0.25} horizontal={false} />
    <XAxis type="number" hide={mode === "expand"} tick={AXIS_TICK} />
    <YAxis type="category" dataKey="primary" width={110} tick={AXIS_TICK} />
    <Tooltip content={<CrosstabTooltip />} cursor={{ fill:"#2e5a6a", fillOpacity:0.12 }} />
    {breakdownDomain.map(v => (
      <Bar key={v} dataKey={v} stackId="a"
           fill={featureByKey(breakdown).colorFor(v, breakdownDomain, cameras)}
           isAnimationActive animationDuration={700} />
    ))}
  </BarChart>
</ResponsiveContainer>
```

- **"Share" mode** = absolute counts (`stackOffset="none"`); **"100%"** = `stackOffset="expand"` (the literal "under-panels ratio by posture" the user asked for); **"count"** = same as share (kept as an explicit label). Guard all-zero rows so `expand` doesn't emit NaN.
- When `breakdown = none`: single-series bars (a distribution) — reuse the same chart with one `<Bar>`, or fall back to `Stat`/`SplitBar` for the binary case.
- Reuses `PostureChart`'s exact axis/tooltip styling (dark `#264653` tooltip, mono terracotta ticks) so it's visually of-a-piece with the existing charts.

### 3b. Temporal path reuses the strips

For `primary ∈ {frame, hour}` we render `ShareStripSet` — the *same* strip visuals lifted out of `DaySeries`, but now stacking **N** breakdown segments (colored by `featureByKey(breakdown).colorFor`) instead of a hardcoded pair. `primary = frame` preserves per-frame `onFrame(frames[i])` click-to-scrub (drives the whole dashboard through `useTimeline`); `hour` disables scrub (coarser). The header's `TIME OF DAY` label + clock axis (`clockOfFrame`) carry over unchanged.

### 3c. Wiring into `Dashboard.tsx`

Phase 1 (non-breaking): drop `<CrossFilter frame={frame} allDay={allDay} onFrame={setFrame} />` **below** the existing `<DaySeries>` in the hero `<section>` (line ~104). Both live; nothing regresses.
Phase 2: default `CrossFilter` to `primary=frame, breakdown=posture` (reproduces the POSTURE strip pixel-for-pixel), delete `DaySeries.tsx`, keep `ShareStripSet` as its heir.

### 3d. api.ts + types.ts additions

```ts
// src/lib/types.ts
export interface CrosstabCell { primary: string; breakdown: string | null; n: number; }
export interface Crosstab {
  primary: string; breakdown: string | null;
  primary_domain: string[]; breakdown_domain: string[];
  cells: CrosstabCell[]; primary_totals: Record<string, number>; total: number;
}
export interface FeatureInfo { key: string; kind: string; available: boolean; }

// src/lib/api.ts
export function getCrosstab(primary: string, breakdown?: string,
    opts: { dataset?: string; camera?: string; frame?: number } = {}): Promise<Crosstab> {
  const q = new URLSearchParams({ primary });
  if (breakdown) q.set("breakdown", breakdown);
  if (opts.dataset) q.set("dataset", opts.dataset);
  if (opts.camera)  q.set("camera", opts.camera);
  if (opts.frame != null) q.set("frame", String(opts.frame));
  return j<Crosstab>(`/api/crosstab?${q}`);
}
export function getFeatures(): Promise<FeatureInfo[]> { return j<FeatureInfo[]>("/api/features"); }
```

---

## 4. Performance & dataset-scoping

- **One aggregation.** A 2-column `GROUP BY` over `detections` (+ optional frames LEFT JOIN) is a single fast DuckDB scan; result size is bounded (categorical domains are small; `frame` primary yields exactly today's `day_series` row count). No pagination.
- **Mode is client-only.** Share↔100%↔count toggles `stackOffset` — no refetch. Swap = swap two params → one refetch. Pivot lives in `useMemo`.
- **Dataset-scoped by default.** `crosstab` defaults `dataset_id = latest_dataset(con)` (per the dataset-dimension direction in memory), with `?dataset=all` as the whole-DB escape hatch — so we never scan a multi-day DB unless asked.
- **Fetch hygiene.** Reuse the existing `alive`/cleanup pattern (add `AbortController`); cache last ~6 results in a `Map` keyed by `${primary}|${breakdown}|${dataset}|${camera}|${frame}` so swap/back is instant. Colors keyed by *value* (never fetch-order index) so region/camera hues don't shuffle between requests.

---

## 5. Effort

| Piece | Effort |
|---|---|
| `features.py` registry + `db.crosstab` + `db.available_features` + tests | **M** |
| `/api/crosstab` + `/api/features` endpoints | **S** |
| `types.ts` + `api.ts` (`getCrosstab`, `getFeatures`) | **S** |
| `features.ts` presentation registry | **S** |
| `DimensionPicker` + `CrossFilter` shell (state, fetch, cache, memo) | **M** |
| `CrosstabBars` (Recharts stacked / expand + tooltip) | **M** |
| Extract `ShareStripSet` from `DaySeries`, generalize to N segments, keep scrub | **M** |
| Dashboard wiring; Phase-2 retire `DaySeries` | **S** |
| **Total** | **L** (→ **XL** only if small-multiples/faceting + saved views are in scope) |

---

## 6. Risks

1. **LEFT JOIN / count semantics for `primary=frame`** — must use `count(d.detection_id)` and keep zero-cow frame slots, or the scrub strips misrender vs today. Mirror `day_series` exactly (regression-test against it).
2. **Color instability on dynamic domains** (region/camera) — color by value via `colorFor`, never by array index, or hues shuffle across fetches.
3. **100%-stack degenerates** — `stackOffset="expand"` emits NaN on all-zero rows; guard/filter zero-total primary values before rendering.
4. **Injection** — the *only* safe path is resolving `primary`/`breakdown` through `FEATURES`; unknown key → 400. Never string-interpolate the query param (note: existing helpers interpolate `trunc` into f-strings — do **not** copy that pattern here; keep crosstab's interpolation limited to trusted registry SQL).
5. **Registry drift** — frontend/backend keys must match; add a test asserting `set(frontend keys) ⊆ set(FEATURES)`.
6. **Scrub only makes sense for `temporal_frame`** — disable click-to-scrub when primary is categorical or `hour`, or clicks silently do nothing/mislead.
7. **Dataset default changes numbers** — defaulting crosstab to `latest_dataset` while today's KPI endpoints run whole-DB could show different totals than the rest of the dashboard until those are aligned.

---

## 7. Open decisions

1. **Default dataset scope**: `latest_dataset` vs whole-DB — align with where the KPI/day-series endpoints are heading (they currently pass `None` = whole-DB).
2. **Keep `DaySeries` beside `CrossFilter`, or fold it in?** Recommend the 2-phase plan (ship alongside, then retire) to de-risk the "great plots stay great" constraint.
3. **`unknown` bucket**: default hide (via `dropUnknown`) to match today's binary strips, with a global "show unknown" toggle? Or always show muted-grey?
4. **Bar orientation** for categorical primary: horizontal (recommended — long area/camera labels, reads as a proportion row like `SplitBar`) vs vertical (matches `PostureChart`).
5. **Optional `breakdown=none`** default (1-D distribution) — recommend yes, so the picker has a sane initial state.
6. **Picker state location**: local to `CrossFilter` now, or lifted to a small context so a future "click a bar → cross-highlight the heatmap / filter the AreaMap" works? Recommend local-but-context-ready.
7. **Faceting / small multiples** (e.g. posture-ratio-per-camera grid of mini 100% bars) — MVP or follow-up?
8. **`/api/features` availability source** — hybrid (static frontend presentation + backend availability flag) vs backend-driven labels too.
9. **Time-filter coupling** — should `CrossFilter` optionally honor the scrubber (`frame`/`allDay`) for an "at this moment" crosstab? The `frame` param supports it; default whole-day.

---

**New files**: `/Users/markschutera/PycharmProjects/Cownting/cownting/features.py`, `/Users/markschutera/PycharmProjects/Cownting/frontend/src/lib/features.ts`, `/Users/markschutera/PycharmProjects/Cownting/frontend/src/components/CrossFilter.tsx`, `.../DimensionPicker.tsx`, `.../CrosstabBars.tsx`, `.../ShareStripSet.tsx`.
**Edited**: `cownting/api.py` (+2 endpoints), `cownting/db.py` (+2 helpers), `frontend/src/lib/api.ts` (+2 fns), `frontend/src/lib/types.ts` (+3 types), `frontend/src/pages/Dashboard.tsx` (mount `CrossFilter`), and eventually retire `frontend/src/components/DaySeries.tsx` into `ShareStripSet.tsx`.