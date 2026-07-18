import type { Crosstab } from "../lib/types";
import { pivot } from "../lib/crosstab";
import { useCrossFilter } from "../lib/crossfilter";

/**
 * One normalized proportion strip per GROUP value (data.primary_domain), each
 * showing the composition across the RATIO dimension (data.breakdown_domain) —
 * the same flat "share" aesthetic as the time-of-day strips, but per category.
 * Reads as "in <group>: <ratio>", e.g. in "under panel": 83% standing · 17% lying.
 */
export function RatioStrips({
  data,
  groupKey,
  ratioKey,
  cameras,
}: {
  data: Crosstab;
  groupKey: string;
  ratioKey: string;
  cameras: string[];
}) {
  const { regionLabels } = useCrossFilter();
  const { segs, segLabel, colorOf, groups } = pivot(data, groupKey, ratioKey, cameras, regionLabels);

  if (groups.length === 0 || segs.length === 0) {
    return <p className="text-gray-tertiary font-mono text-[11px] py-2">No data for this breakdown.</p>;
  }

  return (
    <div className="animate-fade-slide-in">
      {/* shared legend for the ratio segments */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mb-3">
        {segs.map((s) => (
          <span key={s} className="flex items-center gap-1.5 text-[11px] text-gray-tertiary">
            <span className="inline-block w-2 h-2 rounded-full" style={{ background: colorOf(s) }} />
            {segLabel(s)}
          </span>
        ))}
      </div>

      <div className="flex flex-col gap-3">
        {groups.map(({ key, label, counts, total }) => {
          const top = counts.reduce((mi, v, i) => (v > counts[mi] ? i : mi), 0);
          const topPct = Math.round((counts[top] / total) * 100);
          return (
            <div key={key}>
              <div className="flex items-baseline justify-between gap-2 mb-1">
                <span className="text-[12px] text-gray-mid">
                  in <span className="text-near-black">{label}</span>
                </span>
                <span className="text-[11px] text-gray-tertiary tabular-nums">
                  {topPct}% {segLabel(segs[top])} · {total} cows
                </span>
              </div>
              <div className="flex items-stretch gap-px h-5">
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
            </div>
          );
        })}
      </div>
    </div>
  );
}
