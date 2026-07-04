import { useState } from "react";
import type { Line } from "../lib/types";
import { Button, SectionLabel } from "./ui";
import { ImageClicker } from "./ImageClicker";

const MIN_PTS_PER_LINE = 5;

/**
 * Fisheye plumb-line collector. The user traces polylines along edges that are
 * straight in reality but bowed in the image; the backend fits a distortion
 * model that minimizes each line's straightness residual.
 *
 * Clicks append vertices to the *current* line. "Finish line" closes it (if it
 * has ≥1 vertex) and starts a fresh one. Lines are `[x,y]` vertices in camera px.
 */
export function LineCollector({
  src,
  naturalWidth,
  naturalHeight,
  lines,
  onChange,
}: {
  src: string;
  naturalWidth: number;
  naturalHeight: number;
  lines: Line[];
  onChange: (lines: Line[]) => void;
}) {
  // Vertices of the line currently being drawn (not yet committed to `lines`).
  const [current, setCurrent] = useState<Line>([]);

  function place(pt: [number, number]) {
    setCurrent((c) => [...c, pt]);
  }

  function finishLine() {
    if (current.length < 2) return;
    onChange([...lines, current]);
    setCurrent([]);
  }

  function undoVertex() {
    if (current.length > 0) {
      setCurrent((c) => c.slice(0, -1));
    } else if (lines.length > 0) {
      // Reopen the last finished line for editing.
      const last = lines[lines.length - 1];
      onChange(lines.slice(0, -1));
      setCurrent(last);
    }
  }

  function deleteLine(i: number) {
    onChange(lines.filter((_, idx) => idx !== i));
  }

  const okLines = lines.filter((l) => l.length >= MIN_PTS_PER_LINE).length;

  return (
    <div>
      <div className="grid grid-cols-1 lg:grid-cols-[1fr_240px] gap-4">
        <ImageClicker
          title="Camera frame — trace real-straight edges"
          src={src}
          naturalWidth={naturalWidth}
          naturalHeight={naturalHeight}
          mode="polyline"
          lines={lines}
          points={current}
          onPlace={place}
        />

        <div className="flex flex-col gap-3">
          <div className="px-3 py-2 border border-accent text-accent font-mono text-[12px]">
            {current.length === 0
              ? `Click along a real-straight edge (torque tube, roof line, curb). ${lines.length} line${lines.length === 1 ? "" : "s"} so far.`
              : `Line in progress: ${current.length} vertex${current.length === 1 ? "" : "es"} (need ≥${MIN_PTS_PER_LINE}). Add more, then finish.`}
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              disabled={current.length < 2}
              onClick={finishLine}
            >
              Finish line
            </Button>
            <Button variant="ghost" onClick={undoVertex}>
              Undo vertex
            </Button>
          </div>

          <div className="font-mono text-[11px] text-gray-tertiary">
            {okLines} of {lines.length} finished line{lines.length === 1 ? "" : "s"} have ≥
            {MIN_PTS_PER_LINE} points.
          </div>
        </div>
      </div>

      {lines.length > 0 ? (
        <div className="mt-5">
          <div className="mb-2">
            <SectionLabel>Traced lines</SectionLabel>
          </div>
          <div className="flex flex-col gap-2">
            {lines.map((l, i) => {
              const short = l.length < MIN_PTS_PER_LINE;
              return (
                <div
                  key={i}
                  className="flex items-center gap-3 bg-surface border border-border px-3 py-2"
                >
                  <span className="font-mono text-[13px] text-near-black w-6 text-center">
                    {i + 1}
                  </span>
                  <span
                    className={
                      "font-mono text-[11px] " +
                      (short ? "text-[#e76f51]" : "text-accent")
                    }
                  >
                    {l.length} pts{short ? ` ⚠ need ≥${MIN_PTS_PER_LINE}` : ""}
                  </span>
                  <div className="ml-auto flex gap-2">
                    <button
                      onClick={() => deleteLine(i)}
                      className="font-mono text-[11px] px-2 py-1 border border-border text-gray-tertiary hover:border-[#e76f51] hover:text-[#e76f51]"
                    >
                      delete
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}
    </div>
  );
}
