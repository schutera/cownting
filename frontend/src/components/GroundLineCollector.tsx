import { useState } from "react";
import type { GroundLine, Line } from "../lib/types";
import { Button, SectionLabel } from "./ui";
import { ImageClicker } from "./ImageClicker";

const MIN_VERTS = 2;

/**
 * Ground-line correspondence collector. The user traces a STRAIGHT ground
 * feature (a curb, a concrete footing, a painted line, a road/track edge) as a
 * polyline in the CAMERA frame, then traces the SAME physical line in the
 * ORTHOPHOTO. Each pairing becomes a `GroundLine` = `[camLine, orthoLine]`.
 *
 * Unlike the fence collector, this is a LENGTH-AGNOSTIC point-on-line
 * constraint: the backend only uses each camera vertex's perpendicular distance
 * to the (infinite) ortho line, so the two polylines merely have to mark the
 * SAME physical line. Consequences:
 * - The endpoints, length, and position do NOT need to correspond.
 * - There is NO equal-vertex-count requirement — each side just needs ≥2
 *   vertices to define a direction; the counts do NOT need to match.
 * - There is NO snapping — clicks are placed raw on both panes.
 *
 * Contract:
 * - Trace ON THE GROUND — where the feature meets the ground (a curb, footing,
 *   painted line), not an elevated rail or the top of a wall.
 * - Use lines in ≥2 DIFFERENT orientations (not all parallel), or the fit
 *   cannot be pinned down.
 */
export function GroundLineCollector({
  camSrc,
  camW,
  camH,
  orthoSrc,
  orthoW,
  orthoH,
  lines,
  onChange,
  residuals,
}: {
  camSrc: string;
  camW: number;
  camH: number;
  orthoSrc: string;
  orthoW: number;
  orthoH: number;
  lines: GroundLine[];
  onChange: (lines: GroundLine[]) => void;
  residuals?: number[]; // optional per-line perpendicular residual (px), aligned to `lines`
}) {
  // The two in-progress polylines (not yet committed to `lines`). Their vertex
  // counts are independent — they only need to trace the same physical line.
  const [curCam, setCurCam] = useState<Line>([]);
  const [curOrtho, setCurOrtho] = useState<Line>([]);

  const ready = curCam.length >= MIN_VERTS && curOrtho.length >= MIN_VERTS;

  function addGroundLine() {
    if (!ready) return;
    onChange([...lines, [curCam, curOrtho]]);
    setCurCam([]);
    setCurOrtho([]);
  }

  function undoCam() {
    setCurCam((c) => c.slice(0, -1));
  }

  function undoOrtho() {
    setCurOrtho((c) => c.slice(0, -1));
  }

  function deleteLine(i: number) {
    onChange(lines.filter((_, idx) => idx !== i));
  }

  const worst = residuals && residuals.length ? Math.max(...residuals) : 0;
  const flagThresh = Math.max(worst * 0.6, 8); // px — badges above this go red

  const banner = ready
    ? "Ready — add this ground line."
    : `Camera: ${curCam.length} · ortho: ${curOrtho.length} — trace ≥2 points on each, along the same physical line.`;

  return (
    <div>
      <p className="text-[13px] text-text mb-3 max-w-3xl">
        Trace a straight line <strong>on the ground</strong> — a curb, concrete
        footing, or painted line where it meets the ground, not an elevated rail
        — in the camera frame, then the same line in the orthophoto. The
        endpoints and length do <strong>not</strong> need to match; just follow
        the same physical line. Use lines in <strong>≥2 different orientations</strong>{" "}
        (not all parallel), or the fit can't be pinned down.
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ImageClicker
          title="Camera frame — trace a straight ground line"
          src={camSrc}
          naturalWidth={camW}
          naturalHeight={camH}
          mode="polyline"
          points={curCam}
          lines={lines.map((l) => l[0])}
          onPlace={(pt) => setCurCam((c) => [...c, pt])}
        />
        <ImageClicker
          title="Orthophoto — trace the SAME line"
          src={orthoSrc}
          naturalWidth={orthoW}
          naturalHeight={orthoH}
          mode="polyline"
          points={curOrtho}
          lines={lines.map((l) => l[1])}
          onPlace={(pt) => setCurOrtho((c) => [...c, pt])}
        />
      </div>

      <div
        className={
          "mt-4 px-3 py-2 border font-mono text-[12px] " +
          (ready ? "border-accent text-accent" : "border-[#e76f51] text-[#e76f51]")
        }
      >
        {banner}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" disabled={!ready} onClick={addGroundLine}>
          Add ground line
        </Button>
        <Button variant="ghost" disabled={curCam.length === 0} onClick={undoCam}>
          Undo camera vertex
        </Button>
        <Button variant="ghost" disabled={curOrtho.length === 0} onClick={undoOrtho}>
          Undo ortho vertex
        </Button>
      </div>

      {lines.length > 0 ? (
        <div className="mt-5">
          <div className="mb-2">
            <SectionLabel>Ground lines</SectionLabel>
          </div>
          <div className="flex flex-col gap-2">
            {lines.map((link, i) => {
              const res = residuals?.[i];
              const bad = res !== undefined && res >= flagThresh;
              return (
                <div
                  key={i}
                  className="flex items-center gap-3 bg-surface border border-border px-3 py-2"
                >
                  <span className="font-mono text-[13px] text-near-black w-8 text-center">
                    #{i + 1}
                  </span>
                  <span className="font-mono text-[11px] text-accent">
                    {link[0].length}+{link[1].length} pts
                  </span>
                  {res !== undefined ? (
                    <span
                      className={
                        "font-mono text-[10px] px-2 py-0.5 border " +
                        (bad
                          ? "border-[#e76f51] text-[#e76f51]"
                          : "border-accent text-accent")
                      }
                      title="Perpendicular residual (px)"
                    >
                      {res.toFixed(1)} px{bad ? " ⚠" : ""}
                    </span>
                  ) : null}
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

      <div className="mt-4 font-mono text-[11px] text-gray-tertiary">
        {lines.length} ground line{lines.length === 1 ? "" : "s"}
      </div>
    </div>
  );
}
