import { useCrossFilter, GROUPS, RATIOS } from "../lib/crossfilter";
import { featureByKey } from "../lib/features";
import {
  activePreset,
  groupPhrase,
  questionFor,
  ratioPhrase,
  usablePresets,
} from "../lib/crossfilterPresets";
import { useTimeline } from "../lib/timeline";
import { SectionLabel } from "./ui";
import { RatioStrips } from "./RatioStrips";

/**
 * Read the herd as conditional ratios: "of the cows in each X, how many are Y".
 *
 * SCOPED TO THE SCRUBBED INSTANT, not the whole day. The right rail carries the
 * day's aggregates and never moves while you scrub; this is the moment on
 * screen, under the map showing that same moment. Before, both were whole-day
 * and the rail literally mirrored these strips — the same bar twice, 400px
 * apart. Now the two together say something neither can alone: "38% lying right
 * now, against 25% across the day."
 *
 * THE CONTROLS ARE A SENTENCE. Two rows of eight chips plus a swap button
 * offered 64 pivots flat, with nothing marking which mean anything — and the
 * three that answer the study's own question looked exactly like the sixty that
 * do not. Now: four named questions as one-click chips, and below them the
 * pivot as a readable line with the two dimensions inline. Every combination is
 * still reachable; the swap button is gone because swapping is just changing
 * both dropdowns, and "⇄" never said which way round the result would read.
 */
export function CrossFilter({ cameras }: { cameras: string[] }) {
  const { group, ratio, setGroup, setRatio, data, loading, available, frame } =
    useCrossFilter();
  const { timeline } = useTimeline();

  const labelOf = (key: string) => featureByKey(key)?.label ?? key;
  const presets = usablePresets(available);
  const active = activePreset(group, ratio);

  // The wall-clock time of the scrubbed instant, so the scope is stated in the
  // reader's terms rather than as a bucket index.
  const when =
    frame == null || !timeline
      ? null
      : (() => {
          const i = timeline.frames.indexOf(frame);
          const iso = i >= 0 ? timeline.times[i] : null;
          return iso ? iso.slice(11, 16) : null;
        })();

  return (
    <div className="bg-surface border border-border px-4 py-3 mt-6 animate-fade-slide-in">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <SectionLabel>AT THIS MOMENT</SectionLabel>
        <span className="font-mono text-[11px] text-gray-tertiary">
          {when ? `${when} · summed across cameras` : "whole day · summed across cameras"}
        </span>
      </div>

      {/* The named questions. Quietest possible chrome — these are shortcuts,
          not the content, and the strips below are what the eye should land on. */}
      {presets.length > 0 ? (
        <div className="flex flex-wrap items-center gap-1.5 mt-3">
          {presets.map((p) => {
            const on = active?.key === p.key;
            return (
              <button
                key={p.key}
                type="button"
                onClick={() => {
                  setGroup(p.group);
                  setRatio(p.ratio);
                }}
                title={p.question}
                className={
                  "text-[12px] px-2.5 py-1 rounded-full border transition-colors duration-150 " +
                  (on
                    ? "border-accent text-near-black bg-surface-sunk"
                    : "border-border text-gray-tertiary hover:text-near-black")
                }
              >
                {p.label}
              </button>
            );
          })}
        </div>
      ) : null}

      {/* The pivot as a sentence. A <select> rather than a chip row: eight
          options each, and twenty chips is a wall, not a control. */}
      <p className="mt-3 mb-3 text-[13px] text-gray-mid leading-relaxed">
        Of the cows{" "}
        <Inline value={group} onChange={setGroup} options={GROUPS} available={available}
                render={(l) => groupPhrase(l)} />
        , what is the split by{" "}
        <Inline value={ratio} onChange={setRatio} options={RATIOS} available={available}
                render={(l) => ratioPhrase(l)} />
        ?
      </p>

      {data && data.total > 0 ? (
        <>
          <RatioStrips data={data} groupKey={group} ratioKey={ratio} cameras={cameras} />
          <p className="mt-3 text-[11px] text-gray-tertiary">
            {questionFor(group, ratio, labelOf)}{" "}
            {when ? `Counted at ${when}.` : "Counted across the whole day."}
          </p>
        </>
      ) : (
        <p className="text-gray-tertiary font-mono text-xs py-4">
          {loading
            ? "Loading…"
            : when
              ? `No cows in view at ${when}.`
              : "No detections for this breakdown."}
        </p>
      )}
    </div>
  );
}

/* One dimension, inline in the sentence. Styled as text with an underline
   rather than as a form control: it reads as part of the question, and the
   native <select> keeps the keyboard and touch behaviour a custom menu would
   have to rebuild badly. Unavailable dimensions stay in the list but disabled,
   so the reason a dimension is missing is visible rather than mysterious. */
function Inline({
  value,
  onChange,
  options,
  available,
  render,
}: {
  value: string;
  onChange: (key: string) => void;
  options: { key: string; label: string }[];
  available: Record<string, boolean>;
  render: (label: string) => string;
}) {
  const current = options.find((o) => o.key === value);
  return (
    <span className="relative inline-block">
      <span className="text-near-black border-b border-dashed border-gray-tertiary pb-0.5">
        {render(current?.label ?? value)}
        <span aria-hidden="true" className="ml-1 text-[9px] text-gray-tertiary">
          ▾
        </span>
      </span>
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
        aria-label="dimension"
      >
        {options.map((o) => (
          <option key={o.key} value={o.key} disabled={available[o.key] === false}>
            {o.label}
            {available[o.key] === false ? " (no data yet)" : ""}
          </option>
        ))}
      </select>
    </span>
  );
}
