import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { Areas, Crosstab, FeatureInfo } from "./types";
import { getAreas, getCrosstab, getFeatures } from "./api";
import { FEATURES } from "./features";

/**
 * Shared cross-filter selection + result. The pivot is chosen once (Within =
 * `group`, ratio of = `ratio`) and fetched once here, so several views can render
 * the same selection without refetching or drifting apart: the interactive strips
 * under the heatmap (CrossFilter) and the right-rail mirror in the KPI panel
 * (CrossFilterSummary) both read this context. `group` is the crosstab primary,
 * `ratio` the breakdown.
 */

// The "within" (group) dimension: any categorical feature + time-of-day (hour);
// frame is excluded (2400 strips). The "ratio of" dimension is categorical only.
// Camera is dropped: a count area is per-camera, so region already carries the
// camera identity (labelled after it — see regionLabels), making "camera" a
// redundant coarser copy of "count area".
export const GROUPS = FEATURES.filter(
  (f) => (f.kind === "categorical" || f.key === "hour") && f.key !== "camera",
);
export const RATIOS = FEATURES.filter(
  (f) => (f.kind === "categorical" || f.key === "hour") && f.key !== "camera",
);

// Camera-based display labels for region ids (`<camera>::<area>`). A camera with
// a single count area shows just the camera name; multiple areas get an ordered
// `_area1`, `_area2`, … suffix. Derived from the areas config so numbering is
// stable regardless of which areas have detections under the current filter.
export function buildRegionLabels(areas: Areas): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [cam, list] of Object.entries(areas)) {
    if (list.length === 1) {
      out[`${cam}::${list[0].id}`] = cam;
    } else {
      list.forEach((a, i) => {
        out[`${cam}::${a.id}`] = `${cam}_area${i + 1}`;
      });
    }
  }
  return out;
}

type CrossFilterCtx = {
  group: string; // the "within" dimension (one strip each) — crosstab primary
  ratio: string; // the composition inside each strip — crosstab breakdown
  setGroup: (key: string) => void;
  setRatio: (key: string) => void;
  swap: () => void; // exchange group ⇄ ratio (only between two categoricals)
  canSwap: boolean;
  data: Crosstab | null;
  loading: boolean;
  available: Record<string, boolean>; // feature key -> has data yet
  regionLabels: Record<string, string>; // region id -> camera-based display label
};

const Ctx = createContext<CrossFilterCtx | null>(null);

export function CrossFilterProvider({ children }: { children: ReactNode }) {
  const [features, setFeatures] = useState<FeatureInfo[]>([]);
  const [regionLabels, setRegionLabels] = useState<Record<string, string>>({});
  const [group, setGroup] = useState("panel");
  const [ratio, setRatio] = useState("posture");
  const [data, setData] = useState<Crosstab | null>(null);
  const [loading, setLoading] = useState(false);

  const available = useMemo(() => {
    const m: Record<string, boolean> = {};
    for (const f of features) m[f.key] = f.available;
    return m;
  }, [features]);

  useEffect(() => {
    let alive = true;
    getFeatures()
      .then((f) => alive && setFeatures(f))
      .catch(() => {});
    getAreas()
      .then((a) => alive && setRegionLabels(buildRegionLabels(a)))
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getCrosstab(group, ratio)
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [group, ratio]);

  // GROUPS and RATIOS now accept the same dimensions (categoricals + hour), so
  // exchanging group ⇄ ratio is always valid.
  const canSwap = true;
  const swap = () => {
    setGroup(ratio);
    setRatio(group);
  };

  return (
    <Ctx.Provider
      value={{ group, ratio, setGroup, setRatio, swap, canSwap, data, loading, available, regionLabels }}
    >
      {children}
    </Ctx.Provider>
  );
}

export function useCrossFilter(): CrossFilterCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useCrossFilter must be used within a CrossFilterProvider");
  return v;
}
