import { computeDetermination } from "../lib/determination";
import { SectionLabel } from "./ui";

/**
 * Live "calibration determination" panel — a vertical bar giving immediate
 * feedback on how well-*determined* the current point set makes the fit, updated
 * as the user places points (no save/backend needed). It reflects the full
 * per-camera calibration — fisheye lines + center pairs + ground anchors (fence
 * vertices included).
 *
 * A ~0.0 px reproj from an *exactly* determined fit (e.g. 3 ground pts) is a trap:
 * it fits its own points perfectly but has no margin. This bar surfaces that — an
 * exact fit reads amber at ~8%, not green.
 */
export function DeterminationBar({
  nCenter,
  nGroundEff,
  nGoodLines,
}: {
  nCenter: number;
  nGroundEff: number;
  nGoodLines: number;
}) {
  const d = computeDetermination(nCenter, nGroundEff, nGoodLines);
  const pct = Math.round(d.fill * 100);

  return (
    <div className="flex flex-col gap-2 w-[168px] shrink-0">
      <SectionLabel>Determination</SectionLabel>

      <div className="flex gap-2 items-stretch">
        {/* Vertical track, fills from the bottom. */}
        <div
          className="relative w-3.5 rounded-full overflow-hidden bg-surface-sunk border border-border"
          style={{ height: 220 }}
          role="progressbar"
          aria-valuenow={pct}
          aria-valuemin={0}
          aria-valuemax={100}
          aria-label="calibration determination"
        >
          <div
            className="absolute bottom-0 left-0 right-0 transition-all duration-300"
            style={{ height: `${pct}%`, background: d.color }}
          />
          {/* 1x / 2x reference ticks (well-determined target = 2x). */}
          <div className="absolute left-0 right-0 h-px bg-border/70" style={{ bottom: "50%" }} />
        </div>

        <div className="flex flex-col justify-between py-0.5">
          <span className="font-mono text-[9px] text-gray-tertiary leading-none">2× ✓</span>
          <span className="font-mono text-[9px] text-gray-tertiary leading-none">1×</span>
          <span className="font-mono text-[9px] text-gray-tertiary leading-none">0×</span>
        </div>
      </div>

      <div className="font-mono text-[18px] leading-none tabular-nums" style={{ color: d.color }}>
        {pct}%
      </div>
      <div className="font-mono text-[10px] leading-tight" style={{ color: d.color }}>
        {d.label}
      </div>

      {/* What to add next — targets the weakest stage, ladder-aware. */}
      <div className="font-mono text-[10px] leading-snug" style={{ color: d.color }}>
        → {d.advice}
      </div>

      {/* Per-stage breakdown; the weakest (binding) stage is marked with a dot. */}
      <div className="mt-0.5 flex flex-col gap-0.5 border-t border-border pt-1.5">
        {d.subs.map((s) => (
          <div
            key={s.name}
            className="flex items-center justify-between font-mono text-[10px] tabular-nums"
            style={{ color: s.color }}
            title={s.binding ? "weakest stage — it caps the score" : undefined}
          >
            <span>
              {s.binding ? "● " : "  "}
              {s.name}
            </span>
            <span>
              {s.pts}/{s.terms} · {s.ratio.toFixed(1)}×
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
