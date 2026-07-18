import type { Crosstab } from "./types";
import { featureByKey } from "./features";

// One group's row in a pivoted crosstab: the per-segment counts (aligned to the
// shared `segs` order) and their total.
export interface PivotGroup {
  key: string; // group (primary) value
  label: string; // display label — hours read as a clock, else the value itself
  counts: number[]; // per-segment counts, aligned to Pivot.segs
  total: number;
}

export interface Pivot {
  segs: string[]; // ratio (breakdown) values, in domain order
  segLabel: (seg: string) => string; // display label for a segment value (else itself)
  colorOf: (seg: string) => string; // stable per-value colour
  groups: PivotGroup[]; // one per group value, empty groups dropped
  aggregate: number[]; // per-segment totals across all groups (aligned to segs)
  aggTotal: number; // sum of aggregate (== the crosstab grand total)
}

/**
 * Shared pivot math for a Within(group = primary) × ratio-of(ratio = breakdown)
 * crosstab: per-group segment counts plus the overall aggregate, with stable
 * per-value colours from the feature registry. Both the interactive strips under
 * the heatmap (CrossFilter/RatioStrips) and the right-rail mirror in the KPI
 * panel (CrossFilterSummary) render from this, so the two views can never drift.
 */
export function pivot(
  data: Crosstab,
  groupKey: string,
  ratioKey: string,
  cameras: string[],
  labels: Record<string, string> = {},
): Pivot {
  const feat = featureByKey(ratioKey);
  const segs = data.breakdown_domain.map(String);
  // Hour segments read as a clock; region segments as their camera-based label;
  // everything else is its own value.
  const isRatioHour = feat?.kind === "temporal_hour";
  const segLabel = (seg: string) =>
    isRatioHour ? `${seg.padStart(2, "0")}:00` : labels[seg] ?? seg;
  const colorOf = (seg: string) => feat.colorFor(seg, data.breakdown_domain, cameras);

  // Hours read as a clock; region values read as their camera-based label
  // (`labels`); every other group value is its own label.
  const isHour = featureByKey(groupKey)?.kind === "temporal_hour";
  const labelOf = (g: string) =>
    isHour ? `${g.padStart(2, "0")}:00` : labels[g] ?? g;

  const cell = new Map<string, number>();
  for (const c of data.cells) cell.set(`${c.primary} ${c.breakdown}`, c.n);

  const aggregate = segs.map(() => 0);
  const groups: PivotGroup[] = data.primary_domain
    .map((g) => {
      const key = String(g);
      const counts = segs.map((s, i) => {
        const n = cell.get(`${key} ${s}`) ?? 0;
        aggregate[i] += n;
        return n;
      });
      const total = counts.reduce((a, b) => a + b, 0);
      return { key, label: labelOf(key), counts, total };
    })
    .filter((r) => r.total > 0);

  const aggTotal = aggregate.reduce((a, b) => a + b, 0);
  return { segs, segLabel, colorOf, groups, aggregate, aggTotal };
}
