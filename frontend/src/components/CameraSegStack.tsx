import { useEffect, useState } from "react";
import type { FrameRow } from "../lib/types";
import { getFrames, frameImg } from "../lib/api";
import { Panel, SectionLabel } from "./ui";

interface CamState {
  frame: FrameRow | null;
  error: string | null;
}

/**
 * The per-camera segmentation side panel. One representative overlay per
 * camera, stacked vertically. Clicking a tile focuses that camera (drives the
 * KPI trend). Built to scale to the 6-camera target — today there's one.
 */
export default function CameraSegStack({
  cameras,
  active,
  onSelect,
}: {
  cameras: string[];
  active: string;
  onSelect: (camera: string) => void;
}) {
  const [state, setState] = useState<Record<string, CamState>>({});

  useEffect(() => {
    let alive = true;
    cameras.forEach((cam) => {
      getFrames(cam)
        .then((rows) => {
          if (!alive) return;
          // Midday-ish frame: clearest (dawn frames have lens condensation).
          const frame = rows.length ? rows[Math.floor(rows.length / 2)] : null;
          setState((s) => ({ ...s, [cam]: { frame, error: null } }));
        })
        .catch((e: unknown) => {
          if (!alive) return;
          const msg = e instanceof Error ? e.message : String(e);
          setState((s) => ({ ...s, [cam]: { frame: null, error: msg } }));
        });
    });
    return () => {
      alive = false;
    };
  }, [cameras]);

  return (
    <Panel className="lg:sticky lg:top-24">
      <SectionLabel>CAMERAS</SectionLabel>
      <div className="font-display text-xl text-near-black leading-none mt-1">
        Segmentation
      </div>
      <p className="text-[12px] text-gray-tertiary mt-1.5">
        Masks per camera · midday frame
      </p>

      <div className="flex flex-col gap-3 mt-4">
        {cameras.map((cam) => {
          const cs = state[cam];
          const isActive = cam === active;
          const clickable = cameras.length > 1;
          return (
            <button
              key={cam}
              onClick={() => clickable && onSelect(cam)}
              className={
                "group relative block w-full overflow-hidden rounded-xl border text-left transition-colors duration-150 " +
                (clickable ? "cursor-pointer " : "cursor-default ") +
                (isActive ? "border-accent" : "border-border hover:border-accent")
              }
            >
              {cs?.frame ? (
                <img
                  src={frameImg(cam, cs.frame.frame_idx, "overlay")}
                  className="w-full block"
                  alt={`${cam} segmentation`}
                  loading="lazy"
                />
              ) : (
                <div className="aspect-video grid place-items-center bg-surface-sunk text-[11px] font-mono text-gray-tertiary">
                  {cs?.error ? "no frames" : "loading…"}
                </div>
              )}
              <div className="absolute inset-x-0 bottom-0 flex items-center justify-between px-2.5 py-1.5 bg-gradient-to-t from-black/55 to-transparent">
                <span className="font-mono text-[11px] text-white tracking-wide">
                  {cam}
                </span>
                {isActive && cameras.length > 1 && (
                  <span className="w-2 h-2 rounded-full bg-accent ring-2 ring-white/70" />
                )}
              </div>
            </button>
          );
        })}
      </div>
    </Panel>
  );
}
