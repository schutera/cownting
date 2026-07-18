import type { Kpis } from "../lib/types";
import { Panel, Stat } from "./ui";
import { CrossFilterSummary } from "./CrossFilterSummary";

function Rule() {
  return <div className="h-px bg-border my-5" />;
}

/**
 * Right-rail KPIs — whole-day aggregates up top (big scannable numbers), then a
 * live mirror of the cross-filter pivot (CrossFilterSummary) where the two fixed
 * standing/lying + shelter bars used to sit: pick a "ratio of" dimension in the
 * cross-filter and this section follows it.
 */
export default function KpiPanel({ kpis, cameras }: { kpis: Kpis; cameras: string[] }) {
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

      <Rule />

      <CrossFilterSummary cameras={cameras} />

      <Rule />

      <div className="text-[12px] text-gray-tertiary leading-relaxed">
        <div>
          <span className="text-gray-mid">{kpis.valid_frames.toLocaleString()}</span> clear frames
        </div>
      </div>
    </Panel>
  );
}
