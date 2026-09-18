import type { DatasetRow } from "../lib/types";

/* "This day counts nothing into any count area."
 *
 * Areas are drawn PER DAY, because the cameras move between days. That makes it
 * easy to upload a day and never draw its polygons — and the failure is almost
 * silent: the map shows an empty orthophoto, a breakdown by count area shows
 * nothing, and every other number on the page is fine, so it reads as a quiet
 * day rather than as missing setup. Two days shipped that way (2025-09-03 and
 * 2025-09-04, 5,281 cows between them) and went unnoticed for three weeks.
 *
 * Two different states, and conflating them would be the whole mistake:
 *
 *   NO AREAS AT ALL — nothing is counted into any area. Actionable, always
 *   wrong, and worth interrupting for.
 *   AREAS EXIST BUT MISS MOST OF THE HERD — legitimate (cows wander outside the
 *   pen) but worth knowing when it is most of them, because it usually means a
 *   polygon was drawn before the camera was moved. Quieter, and only raised
 *   past a threshold.
 */

// Below this share of a day's cows falling inside some area, the polygons are
// probably not where the herd is. Deliberately generous — cows genuinely leave
// the pen, and crying wolf at 80% would train the reader to ignore the banner.
const THIN_COVERAGE = 0.25;

export type Coverage =
  | { kind: "none" }
  | { kind: "thin"; inside: number; total: number; pct: number }
  | { kind: "ok" };

export function coverageOf(row: DatasetRow | null | undefined): Coverage {
  if (!row) return { kind: "ok" };
  // An older server sends neither field; say nothing rather than guess.
  if (row.n_areas === undefined) return { kind: "ok" };
  if (row.n_areas === 0) return { kind: "none" };
  const total = row.n_detections ?? 0;
  const unassigned = row.n_unassigned ?? 0;
  if (total === 0) return { kind: "ok" };          // no cows: nothing to cover
  const inside = total - unassigned;
  const pct = inside / total;
  if (pct < THIN_COVERAGE) return { kind: "thin", inside, total, pct };
  return { kind: "ok" };
}

/** The full-width banner, for the dashboard. */
export function AreaCoverageBanner({
  row,
  onFix,
}: {
  row: DatasetRow | null | undefined;
  onFix?: () => void;
}) {
  const cov = coverageOf(row);
  if (cov.kind === "ok") return null;
  const none = cov.kind === "none";
  return (
    <div
      role="status"
      className={
        "mb-4 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border px-4 py-3 " +
        (none ? "border-accent bg-accent-soft" : "border-border bg-surface")
      }
    >
      <span aria-hidden="true" className="text-base leading-none">
        {none ? "⚠" : "◔"}
      </span>
      <div className="min-w-0 text-[13px] text-near-black">
        {none ? (
          <>
            <span className="font-medium">No count areas for this day.</span>{" "}
            <span className="text-gray-mid">
              Nothing is counted into an area and the map has nothing to draw. Areas are
              drawn per day, so an earlier day&rsquo;s polygons do not carry over.
            </span>
          </>
        ) : (
          <>
            <span className="font-medium">
              Only {Math.round(cov.pct * 100)}% of this day&rsquo;s cows are inside a count
              area.
            </span>{" "}
            <span className="text-gray-mid">
              {(cov.total - cov.inside).toLocaleString()} of {cov.total.toLocaleString()} fall
              outside every polygon — usually a camera that moved after the areas were drawn.
            </span>
          </>
        )}
      </div>
      {onFix ? (
        <button
          onClick={onFix}
          className="ml-auto shrink-0 font-mono text-[11px] uppercase tracking-[0.14em] text-accent-deep hover:underline"
        >
          Draw areas →
        </button>
      ) : null}
    </div>
  );
}

/** The compact chip, for a day card in the data overview. */
export function AreaCoverageChip({ row }: { row: DatasetRow }) {
  const cov = coverageOf(row);
  if (cov.kind === "ok") return null;
  const none = cov.kind === "none";
  return (
    <span
      title={
        none
          ? "No count areas are drawn for this day — nothing is counted into an area."
          : `Only ${Math.round(cov.pct * 100)}% of this day's cows fall inside a count area.`
      }
      className={
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[10.5px] font-mono uppercase tracking-[0.1em] " +
        (none
          ? "bg-accent-soft text-accent-deep"
          : "border border-border text-gray-tertiary")
      }
    >
      <span aria-hidden="true">{none ? "⚠" : "◔"}</span>
      {none ? "no areas" : `${Math.round(cov.pct * 100)}% in areas`}
    </span>
  );
}
