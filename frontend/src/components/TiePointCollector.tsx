import { useState } from "react";
import type { TiePoint, ImgMeta } from "../lib/types";
import { Button, SectionLabel } from "./ui";
import { ImageClicker } from "./ImageClicker";
import { refImg } from "../lib/api";

/**
 * Cross-camera tie-point collector. There are no fence corners shared between the
 * cameras, so the cameras have nothing to couple their calibrations — this is how
 * cross-camera constraints get created. The user clicks the SAME physical ground
 * feature in every camera that sees it, and those sightings become one "tie point"
 * (a `TiePoint` = list of per-camera `TieObs`). Each tie point is a free
 * bundle-adjustment landmark that ties the involved cameras together during Joint
 * calibrate.
 *
 * Contract:
 * - Click where the feature MEETS THE GROUND (a rock, a mark, the corner of a
 *   footing). Never the top of a post — that point is elevated and won't map
 *   through the ground-plane homography.
 * - A tie point needs the same ground point marked in ≥2 cameras (3+ is stronger).
 *   "Add shared point" stays disabled until at least two cameras are marked.
 * - Points are stored in each camera's natural/full-res px. Only cameras that have
 *   a reference image (an entry in `refs`) are shown.
 */
export function TiePointCollector({
  cameras,
  refs,
  tiePoints,
  onChange,
}: {
  cameras: string[];
  refs: Record<string, ImgMeta>; // per-camera reference image meta (may be missing for some cams)
  tiePoints: TiePoint[];
  onChange: (tp: TiePoint[]) => void;
}) {
  // The in-progress tie point's observation per camera (camera id → natural px).
  const [current, setCurrent] = useState<Record<string, [number, number]>>({});

  // Only cameras with a reference image can be clicked.
  const shown = cameras.filter((cam) => refs[cam]);

  const marked = Object.keys(current);
  const ready = marked.length >= 2;

  function addShared() {
    if (!ready) return;
    onChange([
      ...tiePoints,
      Object.entries(current).map(([camera, pt]) => ({ camera, pt })),
    ]);
    setCurrent({});
  }

  function clearCurrent() {
    setCurrent({});
  }

  function deleteTie(i: number) {
    onChange(tiePoints.filter((_, idx) => idx !== i));
  }

  const banner = ready
    ? `${marked.length} cameras marked (${marked.join(", ")}) — ready to add.`
    : `Mark the same ground point in ≥2 cameras (${marked.length} so far).`;

  return (
    <div>
      <p className="text-[13px] text-text mb-3 max-w-3xl">
        Click the <strong>same point on the ground</strong> — where a feature meets
        the ground (a rock, a mark, the corner of a footing;{" "}
        <strong>not</strong> the top of a post, which is elevated and won't map) —
        in each camera that can see it. You need <strong>≥2</strong> cameras; 3+ is
        better. These shared points are used only when you run Joint calibrate.
      </p>

      <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
        {shown.map((cam) => (
          <ImageClicker
            key={cam}
            title={cam}
            src={refImg(cam)}
            naturalWidth={refs[cam].width}
            naturalHeight={refs[cam].height}
            points={current[cam] ? [current[cam]] : []}
            onPlace={(pt) => setCurrent((c) => ({ ...c, [cam]: pt }))}
          />
        ))}
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
        <Button variant="primary" disabled={!ready} onClick={addShared}>
          Add shared point
        </Button>
        <Button variant="ghost" disabled={marked.length === 0} onClick={clearCurrent}>
          Clear current
        </Button>
      </div>

      {tiePoints.length > 0 ? (
        <div className="mt-5">
          <div className="mb-2">
            <SectionLabel>Shared points</SectionLabel>
          </div>
          <div className="flex flex-col gap-2">
            {tiePoints.map((tp, i) => (
              <div
                key={i}
                className="flex items-center gap-3 bg-surface border border-border px-3 py-2"
              >
                <span className="font-mono text-[13px] text-near-black w-8 text-center">
                  #{i + 1}
                </span>
                <span className="font-mono text-[11px] text-text">
                  {tp.map((o) => o.camera).join(", ")}
                </span>
                <span className="font-mono text-[11px] text-accent">
                  {tp.length} cams
                </span>
                <div className="ml-auto flex gap-2">
                  <button
                    onClick={() => deleteTie(i)}
                    className="font-mono text-[11px] px-2 py-1 border border-border text-gray-tertiary hover:border-[#e76f51] hover:text-[#e76f51]"
                  >
                    delete
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="mt-4 font-mono text-[11px] text-gray-tertiary">
        {tiePoints.length} shared point(s) · used by Joint calibrate
      </div>
    </div>
  );
}
