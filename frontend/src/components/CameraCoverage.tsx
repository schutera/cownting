import { useEffect, useState } from "react";
import { getCameraCoverage } from "../lib/api";
import type { CameraCoverage } from "../lib/types";

/** HH:MM straight off an ISO ts (no timezone shift), for the coverage labels. */
export function hhmm(iso: string | null): string {
  const m = iso?.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

/**
 * Per-camera frame coverage for the selected day, fetched once for the camera
 * card. Silent on failure: coverage decorates the camera tiles, so a missing
 * fetch must never take the card down with it — callers get null and skip the
 * bars. The dashboard subtree remounts per day, so mount-time is per day.
 */
export function useCameraCoverage(): CameraCoverage | null {
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

  return cov;
}

/**
 * One camera's recording window on the day's shared axis: filled runs are the
 * minutes it has frames, gaps are minutes without. Cameras rarely record the
 * same window — one may run all day, another stop hours early — and that
 * unevenness silently skews any count that sums or compares across cameras, so
 * each camera tile carries its own bar right under the image.
 */
export function CoverageBar({
  cov,
  camera,
  color,
  dimmed = false,
}: {
  cov: CameraCoverage;
  camera: string;
  color: string;
  dimmed?: boolean; // camera hidden from the map — fade its bar with the tile
}) {
  // A camera with no frames this day has no row: render nothing rather than an
  // empty wrapper, so the tile keeps its own spacing.
  const c = cov.cameras.find((x) => x.camera_id === camera);
  if (!c) return null;
  const total = cov.max_instant - cov.min_instant + 1;
  const range = `${hhmm(c.first_ts)}–${hhmm(c.last_ts)}`;

  return (
    <div
      // pl-3.5 lines the bar up with the tile image, clear of its colour bar.
      className={"flex items-center gap-2 pl-3.5 pr-0.5" + (dimmed ? " opacity-50" : "")}
      title={`${camera}: ${c.n_frames.toLocaleString()} frames, ${range}`}
    >
      <div className="relative h-1.5 flex-1 rounded-full bg-surface-sunk overflow-hidden">
        {c.segments.map(([s, e], i) => {
          const left = ((s - cov.min_instant) / total) * 100;
          const width = ((e - s + 1) / total) * 100;
          return (
            <div
              key={i}
              className="absolute top-0 bottom-0 rounded-full"
              style={{
                left: `${left}%`,
                width: `${Math.max(width, 0.8)}%`,
                background: color,
                opacity: 0.85,
              }}
            />
          );
        })}
      </div>
      <span className="shrink-0 font-mono text-[10px] text-gray-tertiary tabular-nums">
        {range}
      </span>
    </div>
  );
}

/**
 * Trust warning shown above the tiles when the shortest-covered camera spans
 * well under the longest: totals summed across cameras only line up inside the
 * window they share.
 */
export function UnevenNotice() {
  return (
    <p className="mt-3 text-[11px] leading-snug text-warn bg-warn/10 border border-warn/30 rounded-xl px-2.5 py-2">
      ⚠ Cameras cover very different time ranges — counts summed across cameras only
      line up where they overlap. Read each camera's bar for where it contributes.
    </p>
  );
}
