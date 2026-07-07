import { useEffect, useState } from "react";
import type { Kpis, AreaSummaryRow, Areas } from "../lib/types";
import { getAreaSummary, getAreas } from "../lib/api";
import { Panel, Stat, SplitBar } from "./ui";
import { REST_COLOR, ACTIVE_COLOR, SHELTER_COLOR, OPEN_COLOR } from "../lib/palette";

function Rule() {
  return <div className="h-px bg-border my-5" />;
}

function Dot({ color }: { color: string }) {
  return <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: color }} />;
}

/**
 * Right-rail KPIs — fully aggregated, whole-day, static (no time charts; those
 * live under the timeline now). Big scannable numbers + two-tone proportion bars
 * for standing/lying and shelter, then a per-area breakdown (cows spotted +
 * standing/lying split) for every count area across all cameras.
 */
export default function KpiPanel({
  kpis,
  postureEnabled = true,
}: {
  kpis: Kpis;
  postureEnabled?: boolean;
}) {
  const posed = kpis.standing + kpis.lying; // detections with a posture (excludes unknown)
  const lyingFrac = posed ? kpis.lying / posed : 0;
  const sheltering = Math.round(kpis.pct_sheltering);
  const open = Math.max(0, 100 - sheltering);

  return (
    <Panel className="lg:sticky lg:top-24">
      <div className="flex items-center gap-2">
        <span className="text-lg">☀</span>
        <div>
          <div className="font-display text-xl text-near-black leading-none">Today</div>
          <div className="text-[12px] text-gray-tertiary mt-0.5">across all cameras</div>
        </div>
      </div>

      <div className="mt-5">
        <Stat value={kpis.detections.toLocaleString()} label="cows spotted" size="hero" />
      </div>

      <Rule />

      <Stat
        value={kpis.cows_per_frame.toFixed(1)}
        label="usually in view"
        accent="var(--color-accent-deep)"
      />

      {postureEnabled && (
        <>
          <Rule />

          <div>
            <div className="text-[13px] text-gray-mid mb-2.5">Resting vs. active</div>
            <SplitBar fraction={lyingFrac} leftColor={REST_COLOR} rightColor={ACTIVE_COLOR} />
            <div className="flex items-center justify-between mt-2.5 text-[13px]">
              <span className="flex items-center gap-1.5 text-gray-mid">
                <Dot color={REST_COLOR} /> {kpis.lying.toLocaleString()} lying
              </span>
              <span className="flex items-center gap-1.5 text-gray-mid">
                {kpis.standing.toLocaleString()} standing <Dot color={ACTIVE_COLOR} />
              </span>
            </div>
          </div>
        </>
      )}

      <Rule />

      <div>
        <div className="text-[13px] text-gray-mid mb-2.5">Under panels vs. open</div>
        <SplitBar fraction={sheltering / 100} leftColor={SHELTER_COLOR} rightColor={OPEN_COLOR} />
        <div className="flex items-center justify-between mt-2.5 text-[13px]">
          <span className="flex items-center gap-1.5 text-gray-mid">
            <Dot color={SHELTER_COLOR} /> {sheltering}% sheltering
          </span>
          <span className="flex items-center gap-1.5 text-gray-mid">
            {open}% in the open <Dot color={OPEN_COLOR} />
          </span>
        </div>
      </div>

      <Rule />

      <AreaBreakdown postureEnabled={postureEnabled} />

      <Rule />

      <div className="text-[12px] text-gray-tertiary leading-relaxed">
        <div>
          <span className="text-gray-mid">{kpis.valid_frames.toLocaleString()}</span> clear frames
        </div>
      </div>
    </Panel>
  );
}

/** Per-area cows spotted + standing/lying split, one row per count area. */
function AreaBreakdown({ postureEnabled }: { postureEnabled: boolean }) {
  const [rows, setRows] = useState<AreaSummaryRow[] | null>(null);
  const [names, setNames] = useState<Record<string, string>>({});

  useEffect(() => {
    let alive = true;
    Promise.all([getAreaSummary(), getAreas()])
      .then(([summary, areas]: [AreaSummaryRow[], Areas]) => {
        if (!alive) return;
        setRows(summary);
        const map: Record<string, string> = {};
        Object.entries(areas).forEach(([cam, list]) =>
          list.forEach((a) => {
            map[`${cam}::${a.id}`] = a.name;
          }),
        );
        setNames(map);
      })
      .catch(() => alive && setRows([]));
    return () => {
      alive = false;
    };
  }, []);

  return (
    <div>
      <div className="text-[13px] text-gray-mid mb-3">By area</div>
      {rows === null ? (
        <p className="text-[12px] text-gray-tertiary">Loading…</p>
      ) : rows.length === 0 ? (
        <p className="text-[12px] text-gray-tertiary">
          No count areas yet — draw one on a camera.
        </p>
      ) : (
        <div className="flex flex-col gap-3.5">
          {rows.map((r) => {
            const posed = r.standing + r.lying;
            const frac = posed ? r.lying / posed : 0;
            return (
              <div key={r.region_id}>
                <div className="flex items-baseline justify-between">
                  <span className="text-[13px] text-near-black truncate pr-2">
                    {names[r.region_id] ?? r.region_id}
                  </span>
                  <span className="text-[13px] text-near-black font-medium tabular-nums">
                    {r.total.toLocaleString()}
                  </span>
                </div>
                {postureEnabled && posed > 0 ? (
                  <div className="mt-1.5">
                    <SplitBar fraction={frac} leftColor={REST_COLOR} rightColor={ACTIVE_COLOR} />
                    <div className="text-[11px] text-gray-tertiary mt-1 tabular-nums">
                      {r.lying.toLocaleString()} lying · {r.standing.toLocaleString()} standing
                    </div>
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
