import { useEffect, useState } from "react";
import type { DaySeries as Series } from "../lib/types";
import { getDaySeries } from "../lib/api";
import { SectionLabel } from "./ui";
import { clockOf } from "./TimeScrubber";
import { ACTIVE_COLOR, REST_COLOR, SHELTER_COLOR, OPEN_COLOR } from "../lib/palette";

const CURRENT = "#e76f51"; // terracotta — matches the scrubber's lit frame

/**
 * Time-of-day trend strips sharing the hero scrubber's clock axis. Each is a
 * stacked strip normalized to full height (share / proportion) so the split
 * reads at a glance regardless of herd size: standing vs resting, and
 * under-panels vs open. Hover any strip for the exact values at that minute;
 * click to scrub the whole dashboard there.
 */
export default function DaySeries({
  frame,
  onFrame,
}: {
  frame: number | null;
  onFrame: (f: number) => void;
}) {
  const [series, setSeries] = useState<Series | null>(null);

  useEffect(() => {
    let alive = true;
    getDaySeries()
      .then((s) => alive && setSeries(s))
      .catch(() => alive && setSeries(null));
    return () => {
      alive = false;
    };
  }, []);

  if (!series || series.frames.length === 0) return null;

  return (
    <div className="bg-surface border border-border px-4 py-3 mt-6 animate-fade-slide-in">
      <div className="flex items-baseline justify-between">
        <SectionLabel>TIME OF DAY</SectionLabel>
        <span className="font-mono text-[11px] text-gray-tertiary">summed across cameras</span>
      </div>

      <div className="mt-3 flex flex-col gap-4">
        <ShareStrip
          title="POSTURE"
          frames={series.frames}
          times={series.times}
          a={{ values: series.standing, color: ACTIVE_COLOR, label: "standing" }}
          b={{ values: series.lying, color: REST_COLOR, label: "resting" }}
          emptyText="no posture data"
          frame={frame}
          onFrame={onFrame}
        />
        <ShareStrip
          title="UNDER PANELS"
          frames={series.frames}
          times={series.times}
          a={{ values: series.sheltering, color: SHELTER_COLOR, label: "under panels" }}
          b={{ values: series.open, color: OPEN_COLOR, label: "open" }}
          emptyText="no panel areas drawn yet"
          frame={frame}
          onFrame={onFrame}
        />
      </div>

      <div className="flex justify-between font-mono text-[10px] text-gray-tertiary mt-2">
        <span>{clockOf(series.times[0])}</span>
        <span>{clockOf(series.times[series.times.length - 1])}</span>
      </div>
    </div>
  );
}

/** Index of the frame under the pointer, from the pointer x over the strip. */
function idxFromEvent(e: React.MouseEvent<HTMLDivElement>, n: number): number {
  const r = e.currentTarget.getBoundingClientRect();
  const t = r.width > 0 ? (e.clientX - r.left) / r.width : 0;
  return Math.max(0, Math.min(n - 1, Math.round(t * (n - 1))));
}

/** Floating value tooltip + hairline at the hovered column. */
function Hover({ i, n, children }: { i: number; n: number; children: React.ReactNode }) {
  const left = `${n > 1 ? (i / (n - 1)) * 100 : 0}%`;
  return (
    <>
      <div
        className="absolute top-0 bottom-0 w-px bg-near-black/25 pointer-events-none"
        style={{ left }}
      />
      <div
        className="absolute bottom-full mb-1.5 -translate-x-1/2 z-10 pointer-events-none whitespace-nowrap rounded bg-near-black px-1.5 py-1 text-[10px] font-mono text-white shadow"
        style={{ left }}
      >
        {children}
      </div>
    </>
  );
}

/**
 * Two-part stacked share strip: segment `a` (bottom) over `b` (top), each column
 * normalized to full height so the split reads as a proportion regardless of
 * herd size. Drives both standing-vs-resting and under-panels-vs-open.
 */
function ShareStrip({
  title,
  frames,
  times,
  a,
  b,
  emptyText,
  frame,
  onFrame,
}: {
  title: string;
  frames: number[];
  times: string[];
  a: { values: number[]; color: string; label: string };
  b: { values: number[]; color: string; label: string };
  emptyText: string;
  frame: number | null;
  onFrame: (f: number) => void;
}) {
  const [hover, setHover] = useState<number | null>(null);
  const n = frames.length;
  const dayA = a.values.reduce((s, v) => s + (v ?? 0), 0);
  const dayB = b.values.reduce((s, v) => s + (v ?? 0), 0);
  const dayTot = dayA + dayB;
  const aPct = dayTot ? Math.round((dayA / dayTot) * 100) : 0;

  const ha = hover !== null ? a.values[hover] ?? 0 : 0;
  const hb = hover !== null ? b.values[hover] ?? 0 : 0;

  return (
    <div>
      <div className="flex items-baseline justify-between gap-2 flex-wrap mb-1.5">
        <span className="flex items-center gap-2 text-[12px] text-gray-mid">
          {title}
          <span className="flex items-center gap-1.5 text-[11px] text-gray-tertiary">
            <Dot color={a.color} /> {a.label}
            <Dot color={b.color} /> {b.label}
          </span>
        </span>
        <span className="text-[11px] text-gray-tertiary tabular-nums">
          {dayTot ? `${aPct}% ${a.label} · ${100 - aPct}% ${b.label}` : emptyText}
        </span>
      </div>

      <div className="relative">
        {hover !== null ? (
          <Hover i={hover} n={n}>
            {ha + hb > 0
              ? `${clockOf(times[hover])} · ${ha} ${a.label} · ${hb} ${b.label}`
              : `${clockOf(times[hover])} · no cows`}
          </Hover>
        ) : null}
        <div
          className="flex items-stretch gap-px h-24 cursor-pointer"
          onMouseMove={(e) => setHover(idxFromEvent(e, n))}
          onMouseLeave={() => setHover(null)}
          onClick={() => hover !== null && onFrame(frames[hover])}
        >
          {frames.map((f, i) => {
            const av = a.values[i] ?? 0;
            const bv = b.values[i] ?? 0;
            const tot = av + bv;
            const isCurrent = f === frame;
            return (
              <div
                key={f}
                className="flex-1 min-w-0 flex flex-col justify-end"
                style={{ borderBottom: `2px solid ${isCurrent ? CURRENT : "transparent"}` }}
              >
                {tot > 0 ? (
                  <div
                    className="flex flex-col-reverse"
                    style={{ height: "100%", gap: "1px", opacity: isCurrent ? 1 : 0.62 }}
                  >
                    {av > 0 ? (
                      <div style={{ flexGrow: av, minHeight: "1px", background: a.color }} />
                    ) : null}
                    {bv > 0 ? (
                      <div style={{ flexGrow: bv, minHeight: "1px", background: b.color }} />
                    ) : null}
                  </div>
                ) : null}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

function Dot({ color }: { color: string }) {
  return <span className="inline-block w-2 h-2 rounded-full" style={{ background: color }} />;
}
