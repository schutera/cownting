import type { TimelineData } from "../lib/types";
import { SectionLabel } from "./ui";
import { CHART_MUTED } from "../lib/palette";

// Frame axis -> clock. The Brinno time-lapse runs ~1 frame/minute and frame 0
// reads ~06:00 in the burn-in, so map frame_idx -> 06:00 + idx minutes. (These
// are assumptions until the burned-in timestamp is OCR'd; easy to change here.)
const DAY_START_MIN = 6 * 60;
const MIN_PER_FRAME = 1;

export function clockOfFrame(frameIdx: number): string {
  const total = DAY_START_MIN + frameIdx * MIN_PER_FRAME;
  const hh = Math.floor(total / 60) % 24;
  const mm = Math.round(total % 60);
  return `${String(hh).padStart(2, "0")}:${String(mm).padStart(2, "0")}`;
}

/**
 * Day scrubber: drag through the day to drive the per-camera segmentations and
 * the single-frame occupancy map. The strip behind the slider shows total cows
 * per minute (summed across cameras); the current frame is lit.
 */
export function TimeScrubber({
  timeline,
  frame,
  onFrame,
  allDay,
  onAllDay,
}: {
  timeline: TimelineData;
  frame: number;
  onFrame: (frameIdx: number) => void;
  allDay: boolean;
  onAllDay: (v: boolean) => void;
}) {
  const frames = timeline.frames;
  if (frames.length === 0) return null;
  const index = Math.max(0, frames.indexOf(frame));
  const maxCount = Math.max(1, ...timeline.counts);

  return (
    <div className="bg-surface border border-border px-4 py-3 animate-fade-slide-in">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-baseline gap-3">
          <SectionLabel>TIME OF DAY</SectionLabel>
          <span className="font-display text-2xl text-near-black tabular-nums">
            {clockOfFrame(frame)}
          </span>
          <span className="font-mono text-[11px] text-gray-tertiary">
            frame {frame}
          </span>
        </div>
        <label className="flex items-center gap-2 font-mono text-[11px] text-gray-tertiary cursor-pointer select-none">
          <input
            type="checkbox"
            checked={allDay}
            onChange={(e) => onAllDay(e.target.checked)}
          />
          whole day
        </label>
      </div>

      {/* activity strip — total cows per minute; click a bar to jump there */}
      <div className="mt-3 flex items-end gap-px h-10">
        {frames.map((f, i) => {
          const c = timeline.counts[i] ?? 0;
          const isCurrent = !allDay && f === frame;
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
              title={`${clockOfFrame(f)} · ${c} cows`}
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
        className="w-full mt-2 accent-[#e76f51]"
      />
      <div className="flex justify-between font-mono text-[10px] text-gray-tertiary">
        <span>{clockOfFrame(frames[0])}</span>
        <span>{clockOfFrame(frames[frames.length - 1])}</span>
      </div>
    </div>
  );
}
