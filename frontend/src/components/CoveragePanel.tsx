import { useEffect, useState } from "react";
import { getCameraCoverage } from "../lib/api";
import type { CameraCoverage } from "../lib/types";
import { Panel, SectionLabel } from "./ui";
import { cameraColor } from "../lib/palette";

/** HH:MM straight off an ISO ts (no timezone shift), for the coverage labels. */
function hhmm(iso: string | null): string {
  const m = iso?.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

/**
 * Dashboard coverage strip: one bar per camera showing which minutes it actually
 * has frames, aligned to the shared day axis. Cameras rarely record the same
 * window — one may run all day, another stop hours early — and that unevenness
 * silently skews any count that sums or compares across cameras. This makes it
 * visible, and warns when the spans differ a lot. Scoped to the selected day.
 */
export default function CoveragePanel({ cameras }: { cameras: string[] }) {
  const [cov, setCov] = useState<CameraCoverage | null>(null);

  useEffect(() => {
    let alive = true;
    getCameraCoverage()
      .then((c) => alive && setCov(c))
      .catch(() => {/* coverage is a nice-to-have; stay silent on failure */});
    return () => {
      alive = false;
    };
  }, []);

  if (!cov || cov.cameras.length === 0) return null;
  const total = cov.max_instant - cov.min_instant + 1;

  return (
    <Panel>
      <div className="flex items-end justify-between gap-3 flex-wrap">
        <div>
          <SectionLabel>COVERAGE</SectionLabel>
          <div className="font-display text-xl text-near-black leading-none mt-1">
            When each camera recorded
          </div>
        </div>
        <div className="font-mono text-[11px] text-gray-tertiary tabular-nums">
          {hhmm(cov.min_ts)} – {hhmm(cov.max_ts)}
        </div>
      </div>

      {cov.uneven ? (
        <p className="mt-3 text-[13px] text-warn bg-warn/10 border border-warn/30 rounded-xl px-3.5 py-2.5">
          ⚠ Cameras cover very different time ranges. Counts that sum across cameras only
          line up where the cameras overlap — trust comparisons inside the shared window,
          and read the bars below for where each camera actually contributes.
        </p>
      ) : null}

      <div className="mt-4 flex flex-col gap-2">
        {cov.cameras.map((c) => {
          const color = cameraColor(cameras, c.camera_id);
          return (
            <div key={c.camera_id} className="flex items-center gap-3">
              <div className="w-24 shrink-0 flex items-center gap-1.5 min-w-0">
                <span className="w-2 h-2 rounded-full shrink-0" style={{ background: color }} />
                <span className="font-mono text-[11px] text-near-black truncate">{c.camera_id}</span>
              </div>
              <div
                className="relative flex-1 h-4 rounded bg-surface-sunk overflow-hidden"
                title={`${c.camera_id}: ${c.n_frames.toLocaleString()} frames, ${hhmm(c.first_ts)}–${hhmm(c.last_ts)}`}
              >
                {c.segments.map(([s, e], i) => {
                  const left = ((s - cov.min_instant) / total) * 100;
                  const width = ((e - s + 1) / total) * 100;
                  return (
                    <div
                      key={i}
                      className="absolute top-0 bottom-0 rounded-[3px]"
                      style={{ left: `${left}%`, width: `${Math.max(width, 0.6)}%`, background: color, opacity: 0.85 }}
                    />
                  );
                })}
              </div>
              <div className="w-32 shrink-0 text-right font-mono text-[11px] text-gray-tertiary tabular-nums">
                {hhmm(c.first_ts)}–{hhmm(c.last_ts)} · {c.n_frames.toLocaleString()}
              </div>
            </div>
          );
        })}
      </div>

      <p className="mt-3 text-[11px] text-gray-tertiary">
        Each bar marks the minutes a camera has frames; gaps mean no footage then.
      </p>
    </Panel>
  );
}
