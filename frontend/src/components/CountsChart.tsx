import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import type { TooltipProps } from "recharts";
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

function CountsTooltip({ active, payload }: TooltipProps<number, string>) {
  if (!active || !payload || payload.length === 0) return null;
  const p = payload[0];
  const row = p.payload as CountRow & { _label: string };
  const boxStyle: CSSProperties = {
    background: "#264653",
    border: "1px solid #2e5a6a",
    borderRadius: 0,
    padding: "6px 10px",
  };
  return (
    <div style={boxStyle}>
      <div style={{ fontSize: 9, fontFamily: "ui-monospace, monospace", color: "#e76f51" }}>
        {row._label}
      </div>
      <div
        className="tabular-nums"
        style={{
          fontSize: 13,
          fontFamily: "ui-monospace, monospace",
          color: "#e9c46a",
          fontWeight: 300,
        }}
      >
        {typeof p.value === "number" ? p.value.toFixed(2) : p.value} cows/frame
      </div>
    </div>
  );
}

export default function CountsChart({ camera, trunc }: { camera: string; trunc: string }) {
  const [rows, setRows] = useState<CountRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getCounts(camera, trunc)
      .then((r) => {
        if (alive) setRows(r);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [camera, trunc]);

  if (error) {
    return (
      <p className="text-gray-tertiary font-mono text-[11px]">Couldn't load counts — {error}</p>
    );
  }

  const data = rows.map((r) => ({
    ...r,
    _label: fmtTime(r.t, trunc),
    _val: r.cows_per_frame ?? 0,
  }));

  const latest = rows.length ? rows[rows.length - 1].cows_per_frame ?? 0 : 0;
  const first = rows.length ? rows[0].cows_per_frame ?? 0 : 0;
  const delta = latest - first;
  const deltaStr = `${delta >= 0 ? "+" : ""}${delta.toFixed(2)}`;

  let maxVal = 0;
  let maxLabel = "";
  for (const d of data) {
    if (d._val > maxVal) {
      maxVal = d._val;
      maxLabel = d._label;
    }
  }

  return (
    <div className="bg-surface border border-border px-4 py-4 animate-fade-slide-in">
      <div className="flex items-end justify-between">
        <div className="flex items-baseline gap-1">
          <span
            className="text-4xl font-light tabular-nums"
            style={{ color: CHART_PRIMARY }}
          >
            {latest.toFixed(2)}
          </span>
          <span className="text-[11px] font-mono text-gray-tertiary">/frame</span>
        </div>
        <div className="text-gray-mid font-mono text-xs">Δ {deltaStr}</div>
      </div>

      <div className="mt-4">
        <ResponsiveContainer width="100%" height={260}>
          <AreaChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="countsGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={CHART_PRIMARY} stopOpacity={0.15} />
                <stop offset="100%" stopColor={CHART_PRIMARY} stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke="#2e5a6a" strokeOpacity={0.25} vertical={false} />
            <XAxis
              dataKey="_label"
              stroke="#2e5a6a"
              strokeWidth={1}
              tickLine={false}
              tick={{ fontSize: 10, fontFamily: "ui-monospace, monospace", fill: "#e76f51" }}
            />
            <YAxis
              axisLine={false}
              tickLine={false}
              tick={{ fontSize: 10, fontFamily: "ui-monospace, monospace", fill: "#e76f51" }}
              width={32}
            />
            <Tooltip content={<CountsTooltip />} cursor={{ stroke: "#2e5a6a" }} />
            <Area
              type="monotone"
              dataKey="_val"
              stroke={CHART_PRIMARY}
              strokeWidth={2}
              fill="url(#countsGrad)"
              dot={{ fill: CHART_PRIMARY, stroke: "#264653", strokeWidth: 1.5, r: 3 }}
              activeDot={{ r: 5, fill: "#264653", stroke: CHART_PRIMARY, strokeWidth: 2 }}
              isAnimationActive={true}
              animationDuration={900}
              animationEasing="ease-out"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      <p
        className="text-[10px] font-mono text-text mt-3 animate-fade-slide-in"
        style={{ animationDelay: "800ms" }}
      >
        Peak of {maxVal.toFixed(2)} cows/frame around {maxLabel || "—"}.
      </p>
    </div>
  );
}
