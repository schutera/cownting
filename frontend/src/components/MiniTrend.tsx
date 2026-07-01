import { useEffect, useState } from "react";
import type { CountRow } from "../lib/types";
import { getCounts } from "../lib/api";
import { CHART_PRIMARY } from "../lib/palette";

function fmtTime(iso: string, trunc: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (trunc === "hour") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

const W = 100;
const H = 30;
const PAD_T = 3;
const PAD_B = 2;

/**
 * Compact "cows in view over time" sparkline — replaces the two full charts.
 * Hand-rolled SVG so it stays crisp at any side-panel width.
 */
export default function MiniTrend({ camera, trunc }: { camera: string; trunc: string }) {
  const [rows, setRows] = useState<CountRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getCounts(camera, trunc)
      .then((r) => alive && setRows(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [camera, trunc]);

  if (error) {
    return <p className="text-[12px] text-gray-tertiary">Trend unavailable.</p>;
  }

  const vals = rows.map((r) => r.cows_per_frame ?? 0);
  const n = vals.length;

  if (n < 2) {
    return <p className="text-[12px] text-gray-tertiary">Not enough data yet for a trend.</p>;
  }

  const max = Math.max(...vals, 0.0001);
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => PAD_T + (1 - v / max) * (H - PAD_T - PAD_B);

  const line = vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const area = `${line} L${W},${H} L0,${H} Z`;

  let peakIdx = 0;
  for (let i = 1; i < n; i++) if (vals[i] > vals[peakIdx]) peakIdx = i;
  const peakLabel = fmtTime(rows[peakIdx].t, trunc);

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionCaption>In view over time</SectionCaption>
        <span className="text-[12px] text-gray-tertiary">
          {trunc === "hour" ? "today" : "by day"}
        </span>
      </div>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        preserveAspectRatio="none"
        className="w-full h-12 mt-2 overflow-visible"
      >
        <defs>
          <linearGradient id="trendFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={CHART_PRIMARY} stopOpacity={0.22} />
            <stop offset="100%" stopColor={CHART_PRIMARY} stopOpacity={0} />
          </linearGradient>
        </defs>
        <path d={area} fill="url(#trendFill)" />
        <path
          d={line}
          fill="none"
          stroke={CHART_PRIMARY}
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        <circle
          cx={x(peakIdx)}
          cy={y(vals[peakIdx])}
          r={2.5}
          fill={CHART_PRIMARY}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <p className="text-[12px] text-gray-mid mt-2">
        Busiest around <span className="text-near-black font-medium">{peakLabel}</span>.
      </p>
    </div>
  );
}

function SectionCaption({ children }: { children: React.ReactNode }) {
  return <span className="text-[13px] text-gray-mid">{children}</span>;
}
