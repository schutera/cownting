import { useEffect, useState } from "react";
import type { CSSProperties } from "react";
import {
  ResponsiveContainer,
  BarChart,
  Bar,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
} from "recharts";
import type { TooltipProps } from "recharts";
import type { PostureRow } from "../lib/types";
import { getPosture } from "../lib/api";
import { ACCENT_COLORS } from "../lib/palette";
import { SectionLabel } from "../components/ui";

function fmtTime(iso: string, trunc: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const pad = (n: number) => String(n).padStart(2, "0");
  if (trunc === "hour") return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function num(v: unknown): number {
  return typeof v === "number" ? v : Number(v) || 0;
}

function PostureTooltip({ active, payload }: TooltipProps<number, string>) {
  if (!active || !payload || payload.length === 0) return null;
  const boxStyle: CSSProperties = {
    background: "#264653",
    border: "1px solid #2e5a6a",
    borderRadius: 0,
    padding: "6px 10px",
  };
  const label = (payload[0].payload as { _label?: string })._label ?? "";
  return (
    <div style={boxStyle}>
      <div style={{ fontSize: 9, fontFamily: "ui-monospace, monospace", color: "#e76f51" }}>
        {label}
      </div>
      {payload.map((p) => (
        <div
          key={p.dataKey as string}
          className="tabular-nums"
          style={{
            fontSize: 13,
            fontFamily: "ui-monospace, monospace",
            color: "#e9c46a",
            fontWeight: 300,
          }}
        >
          {p.dataKey as string}: {typeof p.value === "number" ? p.value : num(p.value)}
        </div>
      ))}
    </div>
  );
}

export default function PostureChart({ camera, trunc }: { camera: string; trunc: string }) {
  const [rows, setRows] = useState<PostureRow[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getPosture(camera, trunc)
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
      <p className="text-gray-tertiary font-mono text-[11px]">Couldn't load posture — {error}</p>
    );
  }

  const postureKeys = rows.length
    ? Object.keys(rows[0]).filter((k) => k !== "t")
    : [];

  const data = rows.map((r) => {
    const row: Record<string, number | string> = { _label: fmtTime(r.t, trunc) };
    let total = 0;
    for (const k of postureKeys) {
      const v = num(r[k]);
      row[k] = v;
      total += v;
    }
    row._total = total;
    return row;
  });

  const latestTotal = data.length ? num(data[data.length - 1]._total) : 0;

  let maxTotal = 0;
  let maxLabel = "";
  for (const d of data) {
    const t = num(d._total);
    if (t > maxTotal) {
      maxTotal = t;
      maxLabel = String(d._label);
    }
  }

  const barSize = typeof window !== "undefined" && window.innerWidth < 768 ? 20 : 30;

  return (
    <div className="bg-surface border border-border px-4 py-4 animate-fade-slide-in">
      <div className="flex items-end justify-between">
        <div className="flex items-baseline gap-1">
          <span
            className="text-4xl font-light tabular-nums text-near-black"
          >
            {latestTotal}
          </span>
          <span className="text-[11px] font-mono text-gray-tertiary">postures</span>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          {postureKeys.map((k, i) => (
            <span key={k} className="flex items-center gap-1.5">
              <span
                className="inline-block w-2.5 h-2.5"
                style={{ background: ACCENT_COLORS[i % ACCENT_COLORS.length] }}
              />
              <SectionLabel>{k}</SectionLabel>
            </span>
          ))}
        </div>
      </div>

      <div className="mt-4">
        <ResponsiveContainer width="100%" height={260}>
          <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
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
            <Tooltip content={<PostureTooltip />} cursor={{ fill: "#2e5a6a", fillOpacity: 0.15 }} />
            {postureKeys.map((k, i) => (
              <Bar
                key={k}
                dataKey={k}
                stackId="a"
                fill={ACCENT_COLORS[i % ACCENT_COLORS.length]}
                barSize={barSize}
                isAnimationActive={true}
                animationDuration={900}
                animationEasing="ease-out"
              />
            ))}
          </BarChart>
        </ResponsiveContainer>
      </div>

      <p
        className="text-[10px] font-mono text-text mt-3 animate-fade-slide-in"
        style={{ animationDelay: "800ms" }}
      >
        Busiest window: {maxTotal} postures around {maxLabel || "—"}.
      </p>
    </div>
  );
}
