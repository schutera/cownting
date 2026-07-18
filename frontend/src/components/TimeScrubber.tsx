import type { TimelineData } from "../lib/types";
import { SectionLabel } from "./ui";
import { CHART_MUTED } from "../lib/palette";

// Instant -> clock label, taken from the REAL per-frame timestamp the backend
// returns (times[] parallel to frames[]), not faked from the frame index. The
// naive ISO ("YYYY-MM-DDTHH:MM:SS") is sliced directly so the wall-clock shows
// as captured, independent of the viewer's timezone.
export function clockOf(iso: string | undefined): string {
  return iso ? iso.slice(11, 16) : "--:--";
}

/**
 * Day scrubber, laid out as a single header row so it can live in the sticky
 * header and be dragged from anywhere. Drag through the day to drive the
 * per-camera segmentations and the single-frame occupancy map. The strip behind
 * the slider shows total cows per minute (summed across cameras); the current
 * frame is lit.
 */
export function TimeScrubber({
  timeline,
  frame,
  onFrame,
}: {
  timeline: TimelineData;
  frame: number;
  onFrame: (frameIdx: number) => void;
}) {
  const frames = timeline.frames;
  if (frames.length === 0) return null;
  const index = Math.max(0, frames.indexOf(frame));
  const maxCount = Math.max(1, ...timeline.counts);

  return (
    <div className="border-t border-border px-6 sm:px-10 py-2.5">
      <div className="flex items-baseline gap-2">
        <SectionLabel>TIME OF DAY</SectionLabel>
        <span className="font-display text-xl text-near-black tabular-nums leading-none">
          {clockOf(timeline.times[index])}
        </span>
      </div>

      {/* activity strip — total cows per instant; click a bar to jump there */}
      <div className="mt-1.5 flex items-end gap-px h-9">
        {frames.map((f, i) => {
          const c = timeline.counts[i] ?? 0;
          const isCurrent = f === frame;
          return (
            <div
              key={f}
              onClick={() => onFrame(f)}
              className="flex-1 min-w-0 cursor-pointer"
              style={{
                height: `${8 + (c / maxCount) * 92}%`,
                background: isCurrent ? "#e76f51" : CHART_MUTED,
                opacity: isCurrent ? 1 : 0.3,
              }}
              title={`${clockOf(timeline.times[i])} · ${c} cows`}
            />
          );
        })}
      </div>

      <input
        type="range"
        min={0}
        max={frames.length - 1}
        value={index}
        onChange={(e) => onFrame(frames[Number(e.target.value)])}
        className="w-full mt-1 accent-[#e76f51]"
      />
    </div>
  );
}
