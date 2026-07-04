import { useEffect, useState } from "react";
import type { ShelterRow } from "../lib/types";
import { getShelter } from "../lib/api";
import { SHELTER_COLOR, CHART_MUTED } from "../lib/palette";

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
 * Compact "cows under the panels over time" sparkline. Mirrors MiniTrend: a
 * hand-rolled SVG so it stays crisp at any side-panel width. The teal line is
 * the sheltering count; a faint hairline traces total detections for context.
 */
export default function ShelterTrend({ camera, trunc }: { camera: string; trunc: string }) {
  const [rows, setRows] = useState<ShelterRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getShelter(camera, trunc)
      .then((r) => alive && setRows(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [camera, trunc]);

  if (error) {
    return <p className="text-[12px] text-gray-tertiary">Shelter trend unavailable.</p>;
  }

  const shelter = rows.map((r) => r.sheltering ?? 0);
  const total = rows.map((r) => r.detections ?? 0);
  const n = shelter.length;

  if (n < 2) {
    return <p className="text-[12px] text-gray-tertiary">Not enough data yet for a shelter trend.</p>;
  }

  // Share a vertical scale so sheltering reads as a fraction of the total.
  const max = Math.max(...total, ...shelter, 0.0001);
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => PAD_T + (1 - v / max) * (H - PAD_T - PAD_B);

  const path = (vals: number[]) =>
    vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const shelterLine = path(shelter);
  const shelterArea = `${shelterLine} L${W},${H} L0,${H} Z`;
  const totalLine = path(total);

  let peakIdx = 0;
  for (let i = 1; i < n; i++) if (shelter[i] > shelter[peakIdx]) peakIdx = i;
  const peakLabel = fmtTime(rows[peakIdx].t, trunc);
  const anySheltering = shelter.some((v) => v > 0);

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <SectionCaption>Under panels over time</SectionCaption>
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
          <linearGradient id="shelterFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={SHELTER_COLOR} stopOpacity={0.22} />
            <stop offset="100%" stopColor={SHELTER_COLOR} stopOpacity={0} />
          </linearGradient>
        </defs>
        {/* Faint context line: total detections in view. */}
        <path
          d={totalLine}
          fill="none"
          stroke={CHART_MUTED}
          strokeWidth={1.5}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        <path d={shelterArea} fill="url(#shelterFill)" />
        <path
          d={shelterLine}
          fill="none"
          stroke={SHELTER_COLOR}
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        <circle
          cx={x(peakIdx)}
          cy={y(shelter[peakIdx])}
          r={2.5}
          fill={SHELTER_COLOR}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <p className="text-[12px] text-gray-mid mt-2">
        {anySheltering ? (
          <>
            Most sheltering around <span className="text-near-black font-medium">{peakLabel}</span>.
          </>
        ) : (
          <>No cows under the panels yet.</>
        )}
      </p>
    </div>
  );
}

function SectionCaption({ children }: { children: React.ReactNode }) {
  return <span className="text-[13px] text-gray-mid">{children}</span>;
}
