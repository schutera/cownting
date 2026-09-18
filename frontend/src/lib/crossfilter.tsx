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
import { useTimeline } from "./timeline";
import { FEATURES } from "./features";

/**
 * Shared cross-filter selection + result. The pivot is chosen once and fetched
 * once here; `group` is the crosstab primary, `ratio` the breakdown.
 *
 * TWO SCOPES, ONE SELECTION. `data` carries the timeline's current instant, so
 * the strips under the map describe the moment the map is showing; `dayData` is
 * the same pivot across the whole day, for the rail. The controls exist once,
 * under the map.
 *
 * The rail briefly showed a copy of the centre strips — same pivot, same whole
 * day, four hundred pixels apart — and that copy was deleted. This is not that:
 * the two now differ in scope, which is the only thing that made a pair worth
 * having, and is the same arrangement KpiPanel and MomentPanel use for the
 * headline figures.
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
  data: Crosstab | null;      // the scrubbed instant
  loading: boolean;
  // The SAME pivot across the whole day, for the rail. One selection, two
  // scopes — the pattern the headline figures already use (KpiPanel vs
  // MomentPanel). Fetched separately because it must NOT refetch on every
  // scrubber move, only when the pivot itself changes.
  dayData: Crosstab | null;
  dayLoading: boolean;
  available: Record<string, boolean>; // feature key -> has data yet
  // feature key -> pivoting on it skips detections with no value (see the API's
  // `drops_unassigned`). The UI has to say so, or the strips look like they are
  // losing cows the panel above them just counted.
  dropsUnassigned: Record<string, boolean>;
  regionLabels: Record<string, string>; // region id -> camera-based display label
  // The instant this pivot is scoped to, straight from the scrubber. null =
  // whole day. Exposed so the chart can SAY which it is: the same strip means
  // two very different things at 08:00 and across the day, and a reader who
  // cannot tell them apart will quote one as the other.
  frame: number | null;
};

const Ctx = createContext<CrossFilterCtx | null>(null);

export function CrossFilterProvider({ children }: { children: ReactNode }) {
  // THE PIVOT FOLLOWS THE SCRUBBER, the right rail does not. The two surfaces
  // were showing the same whole-day numbers in two places — the rail literally
  // mirrored the strips 400px below it — so they now answer different
  // questions: the rail is the day's baseline, this is the moment on screen,
  // and the comparison between them is the point.
  const { frame } = useTimeline();
  const [features, setFeatures] = useState<FeatureInfo[]>([]);
  const [regionLabels, setRegionLabels] = useState<Record<string, string>>({});
  const [group, setGroup] = useState("panel");
  const [ratio, setRatio] = useState("posture");
  const [data, setData] = useState<Crosstab | null>(null);
  const [loading, setLoading] = useState(false);
  const [dayData, setDayData] = useState<Crosstab | null>(null);
  const [dayLoading, setDayLoading] = useState(false);

  const available = useMemo(() => {
    const m: Record<string, boolean> = {};
    for (const f of features) m[f.key] = f.available;
    return m;
  }, [features]);

  const dropsUnassigned = useMemo(() => {
    const m: Record<string, boolean> = {};
    for (const f of features) m[f.key] = f.drops_unassigned === true;
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
    // `frame ?? undefined` and not `frame ?? 0`: the route reads a missing
    // frame as "whole day" and frame 0 as a real instant, and 0 is a legitimate
    // bucket key (the first capture of the day).
    getCrosstab(group, ratio, { frame: frame ?? undefined })
      .then((d) => alive && setData(d))
      .catch(() => alive && setData(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [group, ratio, frame]);

  // Whole-day companion for the rail. Depends on the pivot only: a scrub must
  // not re-fetch it, and re-fetching it would also make the rail flicker while
  // dragging — the rail is the thing that is supposed to hold still.
  useEffect(() => {
    let alive = true;
    setDayLoading(true);
    getCrosstab(group, ratio)
      .then((d) => alive && setDayData(d))
      .catch(() => alive && setDayData(null))
      .finally(() => alive && setDayLoading(false));
    return () => {
      alive = false;
    };
  }, [group, ratio]);

  // No `swap` any more: it existed for a ⇄ button between two chip rows, and
  // the sentence replaced both. Swapping is now "change the two dropdowns",
  // which is also honest about the result being a different question rather
  // than the same one rearranged.

  return (
    <Ctx.Provider
      value={{ group, ratio, setGroup, setRatio, data, loading, dayData, dayLoading,
               available, dropsUnassigned, regionLabels, frame }}
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
