import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDataset } from "../lib/dataset";
import type { FrameRow } from "../lib/types";
import { getFrames, frameImg } from "../lib/api";
import { Panel, SectionLabel } from "./ui";
import { cameraColor } from "../lib/palette";

interface CamState {
  frame: FrameRow | null;
  error: string | null;
}

/**
 * Left panel: one segmentation overlay per camera. Each tile has a colour bar
 * (matching its heatmap dots) that toggles the camera in/out of the heatmap;
 * clicking the image enlarges it in the heatmap's centre real estate (via
 * `onExpand`) and focuses the camera. `focused` marks the tile shown there.
 */
export default function CameraSegStack({
  cameras,
  active,
  onSelect,
  onExpand,
  focused,
  frame,
  frameMap,
  hidden,
  onToggleHidden,
}: {
  cameras: string[];
  active: string;
  onSelect: (camera: string) => void;
  onExpand?: (camera: string) => void;
  focused?: string | null;
  frame?: number | null;
  // Per-camera frame_idx for the selected instant (frame). `frame` is a timestamp
  // bucket, not a frame_idx; a camera absent here has no footage at that instant.
  frameMap?: Record<string, number>;
  hidden?: Set<string>;
  onToggleHidden?: (camera: string) => void;
}) {
  const [state, setState] = useState<Record<string, CamState>>({});
  const navigate = useNavigate();
  const { dataset } = useDataset();

  const shownIdxFor = (cam: string): number | undefined =>
    frame != null ? frameMap?.[cam] : state[cam]?.frame?.frame_idx;

  useEffect(() => {
    let alive = true;
    cameras.forEach((cam) => {
      getFrames(cam)
        .then((rows) => {
          if (!alive) return;
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
        {frame != null ? "Masks per camera · at slider time" : "Masks per camera · midday frame"}
        <br />
        Colour bar = heatmap colour · click it to hide/show that camera.
      </p>

      <div className="flex flex-col gap-3 mt-4">
        {cameras.map((cam) => {
          const cs = state[cam];
          const isActive = cam === active;
          const isShown = cam === focused;
          const off = hidden?.has(cam) ?? false;
          const color = cameraColor(cameras, cam);
          const shownIdx = shownIdxFor(cam);
          return (
            <div key={cam} className="flex items-stretch">
              {/* colour bar — toggles this camera in/out of the heatmap */}
              <button
                type="button"
                onClick={(e) => {
                  e.stopPropagation();
                  onToggleHidden?.(cam);
                }}
                aria-pressed={!off}
                title={off ? `${cam} hidden from heatmap — click to show` : `${cam} in heatmap — click to hide`}
                className="w-3 shrink-0 rounded-l-xl border border-r-0 border-border"
                style={{ background: color, opacity: off ? 0.2 : 1 }}
              />
              <button
                onClick={() => {
                  if (cameras.length > 1) onSelect(cam);
                  onExpand?.(cam);
                }}
                aria-pressed={isShown}
                className={
                  "group relative block flex-1 overflow-hidden rounded-r-xl border text-left transition-colors duration-150 cursor-pointer " +
                  (isShown || isActive ? "border-accent" : "border-border hover:border-accent") +
                  (off ? " opacity-50" : "")
                }
              >
                {shownIdx != null ? (
                  <img
                    src={frameImg(cam, shownIdx, "overlay")}
                    className="w-full block"
                    alt={`${cam} segmentation`}
                    loading="lazy"
                  />
                ) : (
                  <div className="aspect-video grid place-items-center bg-surface-sunk text-[11px] font-mono text-gray-tertiary">
                    {cs?.error ? "no frames" : "loading…"}
                  </div>
                )}
                {/* gear — opens the count-area editor for this camera */}
                <span
                  role="button"
                  tabIndex={0}
                  onClick={(e) => {
                    e.stopPropagation();
                    e.preventDefault();
                    if (dataset) navigate(`/count-area/${dataset}/${cam}`);
                  }}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.stopPropagation();
                      e.preventDefault();
                      if (dataset) navigate(`/count-area/${dataset}/${cam}`);
                    }
                  }}
                  title={`Edit count areas for ${cam}`}
                  className="absolute top-2 right-2 grid place-items-center w-7 h-7 rounded-lg bg-black/45 text-white opacity-0 group-hover:opacity-100 hover:bg-black/70 transition-opacity duration-150 cursor-pointer"
                >
                  <svg
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="2"
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className="w-4 h-4"
                    aria-hidden="true"
                  >
                    <circle cx="12" cy="12" r="3" />
                    <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
                  </svg>
                </span>
                <div className="absolute inset-x-0 bottom-0 flex items-center gap-1.5 px-2.5 py-1.5 bg-gradient-to-t from-black/55 to-transparent">
                  <span className="w-2 h-2 rounded-full" style={{ background: color }} />
                  <span className="font-mono text-[11px] text-white tracking-wide">
                    {cam}
                    {off ? " · hidden" : isShown ? " · shown" : ""}
                  </span>
                  {isShown && (
                    <span className="ml-auto w-2 h-2 rounded-full bg-accent ring-2 ring-white/70" />
                  )}
                </div>
              </button>
            </div>
          );
        })}
      </div>
    </Panel>
  );
}
