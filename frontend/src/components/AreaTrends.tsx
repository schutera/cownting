import { useEffect, useState } from "react";
import { getAreaCountsOverTime, getAreas } from "../lib/api";
import { ACCENT_COLORS } from "../lib/palette";

function fmtTime(iso: string, trunc: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (trunc === "hour") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

type SeriesRow = { t: string; region_id: string; cows: number };

const W = 100;
const H = 30;
const PAD_T = 3;
const PAD_B = 2;

/**
 * Compact "cows per count area over time" panel. One hand-rolled SVG sparkline
 * per region_id (same crisp, width-agnostic pattern as MiniTrend / ShelterTrend),
 * fed by getAreaCountsOverTime() and labelled by area.name from getAreas().
 */
export default function AreaTrends({ trunc = "hour" }: { trunc?: string }) {
  const [series, setSeries] = useState<SeriesRow[]>([]);
  const [labels, setLabels] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    Promise.all([getAreaCountsOverTime(undefined, trunc), getAreas()])
      .then(([res, areas]) => {
        if (!alive) return;
        setSeries(res.series ?? []);
        const map: Record<string, string> = {};
        Object.entries(areas).forEach(([cam, list]) =>
          list.forEach((a) => {
            map[`${cam}::${a.id}`] = a.name;
          }),
        );
        setLabels(map);
      })
      .catch((e: unknown) =>
        alive && setError(e instanceof Error ? e.message : String(e)),
      );
    return () => {
      alive = false;
    };
  }, [trunc]);

  if (error) {
    return <p className="text-[12px] text-gray-tertiary">Area trends unavailable.</p>;
  }

  // Group by region_id, preserving the API's time-ordered rows.
  const groups = new Map<string, SeriesRow[]>();
  for (const row of series) {
    const arr = groups.get(row.region_id);
    if (arr) arr.push(row);
    else groups.set(row.region_id, [row]);
  }
  const ids = Array.from(groups.keys()).sort();

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="text-[13px] text-gray-mid">By area over time</span>
        <span className="text-[12px] text-gray-tertiary">
          {trunc === "hour" ? "today" : "by day"}
        </span>
      </div>
      {ids.length === 0 ? (
        <p className="text-[12px] text-gray-tertiary mt-2">
          No area counts yet — draw a count area on a camera.
        </p>
      ) : (
        <div className="mt-3 flex flex-col gap-4">
          {ids.map((rid, i) => (
            <AreaSparkline
              key={rid}
              rows={groups.get(rid) ?? []}
              label={labels[rid] ?? rid}
              color={ACCENT_COLORS[i % ACCENT_COLORS.length]}
              trunc={trunc}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function AreaSparkline({
  rows,
  label,
  color,
  trunc,
}: {
  rows: SeriesRow[];
  label: string;
  color: string;
  trunc: string;
}) {
  const vals = rows.map((r) => r.cows ?? 0);
  const n = vals.length;
  const latest = n ? vals[n - 1] : 0;

  let peakIdx = 0;
  for (let i = 1; i < n; i++) if (vals[i] > vals[peakIdx]) peakIdx = i;
  const peakLabel = n ? fmtTime(rows[peakIdx].t, trunc) : "";

  const max = Math.max(...vals, 0.0001);
  const x = (i: number) => (n > 1 ? (i / (n - 1)) * W : 0);
  const y = (v: number) => PAD_T + (1 - v / max) * (H - PAD_T - PAD_B);
  const line =
    n > 1
      ? vals
          .map((v, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(2)},${y(v).toFixed(2)}`)
          .join(" ")
      : "";
  const area = n > 1 ? `${line} L${W},${H} L0,${H} Z` : "";
  const gradId = `areaTrendFill-${label.replace(/[^a-zA-Z0-9]/g, "")}-${color.replace("#", "")}`;

  return (
    <div>
      <div className="flex items-baseline justify-between">
        <span className="flex items-center gap-1.5 text-[12px] text-gray-mid">
          <span
            className="inline-block w-2 h-2 rounded-full"
            style={{ background: color }}
          />
          {label}
        </span>
        <span className="text-[12px] text-gray-tertiary tabular-nums">{latest}</span>
      </div>
      {n < 2 ? (
        <p className="text-[12px] text-gray-tertiary mt-1">Not enough data yet.</p>
      ) : (
        <>
          <svg
            viewBox={`0 0 ${W} ${H}`}
            preserveAspectRatio="none"
            className="w-full h-10 mt-1.5 overflow-visible"
          >
            <defs>
              <linearGradient id={gradId} x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity={0.22} />
                <stop offset="100%" stopColor={color} stopOpacity={0} />
              </linearGradient>
            </defs>
            <path d={area} fill={`url(#${gradId})`} />
            <path
              d={line}
              fill="none"
              stroke={color}
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              vectorEffect="non-scaling-stroke"
            />
            <circle
              cx={x(peakIdx)}
              cy={y(vals[peakIdx])}
              r={2.5}
              fill={color}
              vectorEffect="non-scaling-stroke"
            />
          </svg>
          <p className="text-[12px] text-gray-mid mt-1">
            Busiest around{" "}
            <span className="text-near-black font-medium">{peakLabel}</span>.
          </p>
        </>
      )}
    </div>
  );
}
