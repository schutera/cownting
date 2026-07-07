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
    <div className="border-t border-border px-6 sm:px-10 py-2.5">
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-baseline gap-2">
          <SectionLabel>TIME OF DAY</SectionLabel>
          <span className="font-display text-xl text-near-black tabular-nums leading-none">
            {clockOfFrame(frame)}
          </span>
          <span className="font-mono text-[10px] text-gray-tertiary hidden sm:inline">
            frame {frame}
          </span>
        </div>
        <label className="flex items-center gap-2 font-mono text-[10px] text-gray-tertiary cursor-pointer select-none">
          <input
            type="checkbox"
            checked={allDay}
            onChange={(e) => onAllDay(e.target.checked)}
          />
          whole day
        </label>
      </div>

      {/* activity strip — total cows per minute; click a bar to jump there */}
      <div className="mt-1.5 flex items-end gap-px h-9">
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
        className="w-full mt-1 accent-[#e76f51]"
      />
    </div>
  );
}
