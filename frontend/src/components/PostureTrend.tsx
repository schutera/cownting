import { useEffect, useState } from "react";
import type { PostureRow } from "../lib/types";
import { getPosture } from "../lib/api";
import { REST_COLOR, CHART_MUTED } from "../lib/palette";

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
 * Compact "cows resting over time" sparkline. Mirrors ShelterTrend: the sage
 * line is the lying count from the reused posture proxy; a faint hairline traces
 * total detections so resting reads as a fraction of the herd in view.
 */
export default function PostureTrend({ camera, trunc }: { camera: string; trunc: string }) {
  const [rows, setRows] = useState<PostureRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getPosture(camera, trunc)
      .then((r) => alive && setRows(r))
      .catch((e: unknown) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [camera, trunc]);

  if (error) {
    return <p className="text-[12px] text-gray-tertiary">Resting trend unavailable.</p>;
  }

  const lying = rows.map((r) => Number(r.lying ?? 0));
  const total = rows.map((r) =>
    Object.entries(r).reduce((s, [k, v]) => (k === "t" ? s : s + (Number(v) || 0)), 0),
  );
  const n = lying.length;

  if (n < 2) {
    return <p className="text-[12px] text-gray-tertiary">Not enough data yet for a resting trend.</p>;
  }

  // Share a vertical scale so resting reads as a fraction of the total in view.
  const max = Math.max(...total, ...lying, 0.0001);
  const x = (i: number) => (i / (n - 1)) * W;
  const y = (v: number) => PAD_T + (1 - v / max) * (H - PAD_T - PAD_B);

  const path = (vals: number[]) =>
    vals.map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`).join(" ");
  const restLine = path(lying);
  const restArea = `${restLine} L${W},${H} L0,${H} Z`;
  const totalLine = path(total);

  let peakIdx = 0;
  for (let i = 1; i < n; i++) if (lying[i] > lying[peakIdx]) peakIdx = i;
  const peakLabel = fmtTime(rows[peakIdx].t, trunc);
  const anyResting = lying.some((v) => v > 0);

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-[13px] text-gray-mid">Resting over time</span>
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
          <linearGradient id="postureFill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={REST_COLOR} stopOpacity={0.22} />
            <stop offset="100%" stopColor={REST_COLOR} stopOpacity={0} />
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
        <path d={restArea} fill="url(#postureFill)" />
        <path
          d={restLine}
          fill="none"
          stroke={REST_COLOR}
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          vectorEffect="non-scaling-stroke"
        />
        <circle
          cx={x(peakIdx)}
          cy={y(lying[peakIdx])}
          r={2.5}
          fill={REST_COLOR}
          vectorEffect="non-scaling-stroke"
        />
      </svg>
      <p className="text-[12px] text-gray-mid mt-2">
        {anyResting ? (
          <>
            Most resting around <span className="text-near-black font-medium">{peakLabel}</span>.
          </>
        ) : (
          <>No resting cows detected yet.</>
        )}
      </p>
    </div>
  );
}
