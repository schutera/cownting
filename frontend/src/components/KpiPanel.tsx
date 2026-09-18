import type { AttrKpis, Kpis } from "../lib/types";
import { ACTIVE_COLOR, REST_COLOR, FEED_COLOR, SHELTER_COLOR, OPEN_COLOR, CHART_MUTED } from "../lib/palette";
import { UNSURE_COLOR } from "../lib/features";
import { Panel, SectionLabel, Stat } from "./ui";
import { CrossFilterSummary } from "./CrossFilterSummary";

function Rule() {
  return <div className="h-px bg-border my-5" />;
}

/**
 * Right-rail KPIs — whole-day aggregates up top (big scannable numbers), then
 * what the crop model says the herd is doing (AttrStrips), then a live mirror
 * of the cross-filter pivot (CrossFilterSummary): pick a "ratio of" dimension
 * in the cross-filter and that section follows it.
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

      {kpis.attrs ? (
        <>
          <Rule />
          <AttrStrips attrs={kpis.attrs} />
        </>
      ) : null}

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

/* What the crop model says, as two composition strips.
 *
 * Deliberately the quietest block on the rail: a 6px bar and one legend line
 * each, tertiary text, no numbers larger than 12px — the hero above is the
 * count, and this must not compete with it. Colours match the cross-filter's
 * own palette for the same classes (features.ts), so a colour means the same
 * thing here, in the pivot strips and in the legend under the frame.
 *
 * `unsure` is drawn as its own segment and named in the legend, never folded
 * into a posture: it is the share of animals the model looked at and declined
 * to call, and making that share visible is the point of the abstention.
 * The denominator is `scored`, not `detections` — the model's own view of the
 * day — and a day it has not scored says so in words rather than drawing empty
 * bars that read as "nothing happened".
 */
function AttrStrips({ attrs }: { attrs: AttrKpis }) {
  if (attrs.scored === 0) {
    return (
      <div>
        <SectionLabel>Behaviour · light</SectionLabel>
        <p className="text-[12px] text-gray-tertiary py-2">The crop model has not scored this day yet.</p>
      </div>
    );
  }
  const b = attrs.behaviour;
  const s = attrs.sun;
  const behaviour = [
    { key: "feeding", label: "feeding", n: b.feeding, color: FEED_COLOR },
    { key: "lying", label: "lying", n: b.lying, color: REST_COLOR },
    { key: "standing", label: "standing", n: b.standing, color: ACTIVE_COLOR },
    { key: "not_visible", label: "not visible", n: b.not_visible, color: CHART_MUTED },
    { key: "unsure", label: "unsure", n: b.unsure, color: UNSURE_COLOR },
  ];
  const sun = [
    { key: "direct_sun", label: "in the sun", n: s.direct_sun, color: OPEN_COLOR },
    { key: "shaded", label: "in shade", n: s.shaded, color: SHELTER_COLOR },
    { key: "not_visible", label: "not visible", n: s.not_visible, color: CHART_MUTED },
  ];
  return (
    <div className="flex flex-col gap-4">
      <Strip title="Behaviour" segs={behaviour} total={attrs.scored} />
      <Strip title="Light" segs={sun} total={attrs.scored} />
      <div className="font-mono text-[10.5px] text-gray-tertiary -mt-1">
        of {attrs.scored.toLocaleString()} scored by the crop model
      </div>
    </div>
  );
}

function Strip({
  title,
  segs,
  total,
}: {
  title: string;
  segs: { key: string; label: string; n: number; color: string }[];
  total: number;
}) {
  const shown = segs.filter((x) => x.n > 0);
  return (
    <div>
      <SectionLabel>{title}</SectionLabel>
      <div className="mt-2 flex h-1.5 w-full overflow-hidden rounded-full bg-surface-sunk">
        {shown.map((x) => (
          <div
            key={x.key}
            title={`${x.label} ${Math.round((100 * x.n) / total)}%`}
            style={{ width: `${(100 * x.n) / total}%`, background: x.color }}
          />
        ))}
      </div>
      <div className="mt-2 flex flex-wrap gap-x-3 gap-y-1">
        {shown.map((x) => (
          <span key={x.key} className="inline-flex items-center gap-1.5 text-[11px] text-gray-tertiary">
            <span className="inline-block h-2 w-2 rounded-full" style={{ background: x.color }} />
            <span className="text-gray-mid tabular-nums">{Math.round((100 * x.n) / total)}%</span>
            {x.label}
          </span>
        ))}
      </div>
    </div>
  );
}
