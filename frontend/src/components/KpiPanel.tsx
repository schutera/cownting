import type { Kpis } from "../lib/types";
import { Panel, Stat, SplitBar } from "./ui";
import { REST_COLOR, ACTIVE_COLOR } from "../lib/palette";
import MiniTrend from "./MiniTrend";

function Rule() {
  return <div className="h-px bg-border my-5" />;
}

function Dot({ color }: { color: string }) {
  return <span className="inline-block w-2.5 h-2.5 rounded-full" style={{ background: color }} />;
}

export default function KpiPanel({
  kpis,
  camera,
  trunc,
}: {
  kpis: Kpis;
  camera: string;
  trunc: string;
}) {
  const resting = Math.round(kpis.pct_lying);
  const active = Math.max(0, 100 - resting);

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
        <Stat value={kpis.detections.toLocaleString()} label="cows seen" size="hero" />
      </div>

      <Rule />

      <Stat
        value={kpis.cows_per_frame.toFixed(1)}
        label="usually in view"
        accent="var(--color-accent-deep)"
      />

      <Rule />

      <div>
        <div className="text-[13px] text-gray-mid mb-2.5">Resting vs. active</div>
        <SplitBar fraction={resting / 100} leftColor={REST_COLOR} rightColor={ACTIVE_COLOR} />
        <div className="flex items-center justify-between mt-2.5 text-[13px]">
          <span className="flex items-center gap-1.5 text-gray-mid">
            <Dot color={REST_COLOR} /> {resting}% resting
          </span>
          <span className="flex items-center gap-1.5 text-gray-mid">
            {active}% active <Dot color={ACTIVE_COLOR} />
          </span>
        </div>
      </div>

      <Rule />

      <MiniTrend camera={camera} trunc={trunc} />

      <Rule />

      <div className="text-[12px] text-gray-tertiary leading-relaxed">
        <div>
          <span className="text-gray-mid">{kpis.valid_frames.toLocaleString()}</span> clear ·{" "}
          <span className="text-gray-mid">{kpis.blind_frames.toLocaleString()}</span> unclear frames
        </div>
        <div>
          <span className="text-gray-mid">{Math.round(kpis.pct_localized)}%</span> placed on the map
        </div>
      </div>
    </Panel>
  );
}
