import { useCrossFilter, GROUPS, RATIOS } from "../lib/crossfilter";
import { SectionLabel } from "./ui";
import { DimensionPicker } from "./DimensionPicker";
import { RatioStrips } from "./RatioStrips";

/**
 * Interactive cross-filter: read the herd as conditional ratios — "within
 * <group>, the ratio of <X>". Pick the group dimension (posture, under-panels,
 * shade, count-area, or time-of-day) and the ratio dimension; swap which
 * is which. Count areas are labelled after their camera (a camera's lone area
 * shows as the camera name; multiple areas get `_area1`, `_area2`, …), so there
 * is no separate camera dimension. Renders one flat proportion strip per group
 * value, in the same
 * "share" style as the old time-of-day strips — which it now subsumes, so those
 * are gone (reachable here via group = Time of day).
 *
 * Selection + fetched result live in CrossFilterProvider, so the right-rail KPI
 * mirror (CrossFilterSummary) reflects the very same pivot without a second fetch.
 */
export function CrossFilter({ cameras }: { cameras: string[] }) {
  const { group, ratio, setGroup, setRatio, swap, canSwap, data, loading, available } =
    useCrossFilter();

  return (
    <div className="bg-surface border border-border px-4 py-3 mt-6 animate-fade-slide-in">
      <div className="flex items-baseline justify-between">
        <SectionLabel>CROSS-FILTER</SectionLabel>
        <span className="font-mono text-[11px] text-gray-tertiary">summed across cameras</span>
      </div>

      <div className="flex items-end flex-wrap gap-x-5 gap-y-2 mt-3 mb-4">
        <DimensionPicker
          label="Within"
          value={group}
          onChange={setGroup}
          features={GROUPS}
          available={available}
        />
        <button
          type="button"
          onClick={swap}
          disabled={!canSwap}
          title="Swap group and ratio dimensions"
          className="font-mono text-sm px-2 py-1 self-center text-gray-tertiary hover:text-accent disabled:opacity-25 disabled:hover:text-gray-tertiary"
        >
          ⇄
        </button>
        <DimensionPicker
          label="ratio of"
          value={ratio}
          onChange={setRatio}
          features={RATIOS}
          available={available}
        />
      </div>

      {data && data.total > 0 ? (
        <RatioStrips data={data} groupKey={group} ratioKey={ratio} cameras={cameras} />
      ) : (
        <p className="text-gray-tertiary font-mono text-xs py-4">
          {loading ? "Loading…" : "No detections for this breakdown."}
        </p>
      )}
    </div>
  );
}
