import { useCrossFilter } from "../lib/crossfilter";
import { featureByKey } from "../lib/features";
import { pivot } from "../lib/crosstab";
import { SectionLabel } from "./ui";

/**
 * The same breakdown as the panel under the map, for the WHOLE DAY.
 *
 * One pivot selection, two scopes — exactly the pattern the rail and
 * MomentPanel already use for the headline figures. The controls live once, in
 * the panel below the map; this follows whatever they are set to and answers it
 * across the day, so the reader can see "in the shade they are mostly lying —
 * and that holds all day, not just at 12:20" without touching anything.
 *
 * This is NOT the component that was deleted a few commits ago. That one showed
 * the whole day beside a panel that also showed the whole day, which is a copy.
 * Now the two differ in scope, which is the only thing that made the pair worth
 * having.
 *
 * Deliberately tighter than the centre version and with no aggregate block: the
 * rail already carries the day's composition in AttrStrips above, and the thing
 * this adds is the per-group comparison.
 */
export function DayBreakdown({ cameras }: { cameras: string[] }) {
  const { group, ratio, dayData, dayLoading, regionLabels, dropsUnassigned } = useCrossFilter();
  const groupLabel = (featureByKey(group)?.label ?? group).toLowerCase();
  const ratioLabel = (featureByKey(ratio)?.label ?? ratio).toLowerCase();

  if (!dayData || dayData.total === 0) {
    return (
      <div>
        <SectionLabel>Break it down</SectionLabel>
        <p className="text-[12px] text-gray-tertiary py-2">
          {dayLoading ? "Loading…" : "Nothing to break down for this day."}
        </p>
      </div>
    );
  }

  const { segs, segLabel, colorOf, groups } = pivot(dayData, group, ratio, cameras, regionLabels);
  if (groups.length === 0 || segs.length === 0) return null;

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2">
        <SectionLabel>Break it down</SectionLabel>
        <span className="text-[11px] text-gray-tertiary">whole day</span>
      </div>
      <div className="text-[12px] text-gray-tertiary mt-1 leading-snug">
        {ratioLabel} by {groupLabel}
      </div>

      {/* One shared key, then one thin bar per group value. */}
      <div className="mt-2.5 flex flex-wrap items-center gap-x-2.5 gap-y-1">
        {segs.map((s) => (
          <span key={s} className="inline-flex items-center gap-1 text-[11px] text-gray-tertiary">
            <span
              className="inline-block w-2 h-2 rounded-full"
              style={{ background: colorOf(s) }}
            />
            {segLabel(s)}
          </span>
        ))}
      </div>

      <div className="mt-2.5 flex flex-col gap-2">
        {groups.map(({ key, label, counts, total }) => {
          const top = counts.reduce((mi, v, i) => (v > counts[mi] ? i : mi), 0);
          return (
            <div key={key}>
              <div className="flex items-baseline justify-between gap-2 mb-0.5">
                <span className="text-[12px] text-near-black truncate">{label}</span>
                <span className="text-[11px] text-gray-tertiary tabular-nums shrink-0">
                  {Math.round((counts[top] / total) * 100)}% {segLabel(segs[top])}
                </span>
              </div>
              <div className="flex items-stretch gap-px h-2 w-full rounded-full overflow-hidden bg-surface-sunk">
                {segs.map((s, i) =>
                  counts[i] > 0 ? (
                    <div
                      key={s}
                      title={`${segLabel(s)}: ${counts[i].toLocaleString()} (${Math.round(
                        (counts[i] / total) * 100,
                      )}%)`}
                      style={{ flexGrow: counts[i], minWidth: "2px", background: colorOf(s) }}
                    />
                  ) : null,
                )}
              </div>
            </div>
          );
        })}
      </div>

      {dropsUnassigned[group] || dropsUnassigned[ratio] ? (
        <p className="mt-2 text-[11px] text-gray-tertiary leading-snug">
          Only cows inside a count area.
        </p>
      ) : null}
    </div>
  );
}
