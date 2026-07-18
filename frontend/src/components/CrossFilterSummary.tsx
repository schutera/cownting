import { useCrossFilter } from "../lib/crossfilter";
import { featureByKey } from "../lib/features";
import { pivot } from "../lib/crosstab";
import { SectionLabel } from "./ui";

/**
 * Right-rail mirror of the cross-filter's current pivot. Reflects both live
 * selections without a second fetch (reads the shared CrossFilterProvider): the
 * aggregate composition of the chosen "ratio of" dimension overall, then that
 * same composition broken down by the "Within" group — a compact echo of the
 * strips under the heatmap, in the panel's tighter idiom. Change a chip in the
 * cross-filter and this section follows.
 */
export function CrossFilterSummary({ cameras }: { cameras: string[] }) {
  const { group, ratio, data, loading, regionLabels } = useCrossFilter();
  const ratioLabel = featureByKey(ratio)?.label ?? ratio;
  const groupLabel = (featureByKey(group)?.label ?? group).toLowerCase();

  if (!data || data.total === 0) {
    return (
      <div>
        <SectionLabel>Cross-filter</SectionLabel>
        <p className="text-[12px] text-gray-tertiary py-3">
          {loading ? "Loading…" : "No detections for this breakdown."}
        </p>
      </div>
    );
  }

  const { segs, segLabel, colorOf, groups, aggregate, aggTotal } = pivot(
    data,
    group,
    ratio,
    cameras,
    regionLabels,
  );

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <SectionLabel>Cross-filter</SectionLabel>
        <span className="text-[11px] text-gray-tertiary">follows the pivot</span>
      </div>
      <div className="font-display text-xl text-near-black leading-none mt-1.5">{ratioLabel}</div>
      <div className="text-[12px] text-gray-tertiary mt-0.5">by {groupLabel}</div>

      {/* overall composition of the ratio dimension (all groups combined) */}
      <div className="mt-3.5">
        <StackBar counts={aggregate} segs={segs} segLabel={segLabel} colorOf={colorOf} />
        <div className="mt-2.5 flex flex-col gap-1.5">
          {segs.map((s, i) =>
            aggregate[i] > 0 ? (
              <div key={s} className="flex items-center justify-between text-[13px]">
                <span className="flex items-center gap-1.5 text-gray-mid">
                  <Dot color={colorOf(s)} /> {segLabel(s)}
                </span>
                <span className="text-gray-tertiary tabular-nums">
                  {Math.round((aggregate[i] / aggTotal) * 100)}% ·{" "}
                  {aggregate[i].toLocaleString()}
                </span>
              </div>
            ) : null,
          )}
        </div>
      </div>

      {/* the same composition, broken down by each group value */}
      {groups.length > 1 ? (
        <div className="mt-4 pt-3.5 border-t border-border">
          <div className="text-[12px] text-gray-mid mb-2.5">By {groupLabel}</div>
          <div className="flex flex-col gap-2.5">
            {groups.map(({ key, label, counts, total }) => {
              const top = counts.reduce((mi, v, i) => (v > counts[mi] ? i : mi), 0);
              const topPct = Math.round((counts[top] / total) * 100);
              return (
                <div key={key}>
                  <div className="flex items-baseline justify-between gap-2 mb-1">
                    <span className="text-[12px] text-near-black truncate">{label}</span>
                    <span className="text-[11px] text-gray-tertiary tabular-nums shrink-0">
                      {topPct}% {segLabel(segs[top])} · {total.toLocaleString()}
                    </span>
                  </div>
                  <StackBar counts={counts} segs={segs} segLabel={segLabel} colorOf={colorOf} thin />
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function Dot({ color }: { color: string }) {
  return <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: color }} />;
}

// A multi-segment proportion bar (generalizes ui's two-tone SplitBar to the N
// ratio segments). Segment widths are count-proportional; colours come from the
// feature registry so a value keeps its hue everywhere.
function StackBar({
  counts,
  segs,
  segLabel,
  colorOf,
  thin = false,
}: {
  counts: number[];
  segs: string[];
  segLabel: (seg: string) => string;
  colorOf: (seg: string) => string;
  thin?: boolean;
}) {
  const total = counts.reduce((a, b) => a + b, 0);
  if (total === 0) return null;
  return (
    <div
      className={
        "flex items-stretch gap-px w-full rounded-full overflow-hidden " +
        (thin ? "h-2.5" : "h-4")
      }
    >
      {segs.map((s, i) =>
        counts[i] > 0 ? (
          <div
            key={s}
            title={`${segLabel(s)}: ${counts[i]} (${Math.round((counts[i] / total) * 100)}%)`}
            style={{ flexGrow: counts[i], minWidth: "2px", background: colorOf(s) }}
          />
        ) : null,
      )}
    </div>
  );
}
