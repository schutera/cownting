import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDataset } from "../lib/dataset";
import { useAuth, canManageData } from "../lib/auth";
import type { FrameRow } from "../lib/types";
import { getFrames, frameImg } from "../lib/api";
import { CogIcon, Panel, SectionLabel } from "./ui";
import { cameraColor } from "../lib/palette";
import { CoverageBar, UnevenNotice, hhmm, useCameraCoverage } from "./CameraCoverage";

interface CamState {
  frame: FrameRow | null;
  error: string | null;
}

// Honest placeholder for a tile with no image to show. Without this, a camera
// that simply has no footage at the selected slider instant (shownIdx null while
// a `frame` instant is picked — e.g. camera_02 after it stopped early) would read
// "loading…" forever. Order matters: a real fetch error wins; then "no frame at
// this instant"; then a not-yet-loaded state; else the camera has no frames at all.
function placeholderText(
  cs: CamState | undefined,
  frame: number | null | undefined,
  shownIdx: number | undefined,
): string {
  if (cs?.error) return "no frames";
  if (frame != null && shownIdx == null) return "no frame now";
  if (cs === undefined) return "loading…";
  return "no frames";
}

/**
 * Left panel: one segmentation overlay per camera. Each tile has a colour bar
 * (matching its heatmap dots) that toggles the camera in/out of the heatmap;
 * clicking the image enlarges it in the heatmap's centre real estate (via
 * `onExpand`) and focuses the camera. `focused` marks the tile shown there.
 *
 * Each tile also carries that camera's coverage bar — when it actually recorded
 * on the day's shared axis — so the "which cameras contribute when" question is
 * answered on the camera itself rather than in a separate panel.
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
  const { user } = useAuth();
  const canManage = canManageData(user);
  const cov = useCameraCoverage();

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
      <div className="flex items-center gap-2">
        <SectionLabel>CAMERAS</SectionLabel>
        {dataset && canManage ? (
          <button
            onClick={() => navigate(`/data/${dataset}/cameras`)}
            title="Manage cameras — add, replace, or delete a stream for this day"
            aria-label="manage cameras"
            className="grid place-items-center w-6 h-6 rounded-full text-gray-tertiary hover:text-accent hover:bg-accent-soft transition-colors"
          >
            <CogIcon className="w-3.5 h-3.5" />
          </button>
        ) : null}
      </div>
      <div className="font-display text-xl text-near-black leading-none mt-1">
        Segmentation
      </div>
      <p className="text-[12px] text-gray-tertiary mt-1.5">
        {frame != null ? "Masks per camera · at slider time" : "Masks per camera · midday frame"}
        <br />
        Colour bar = heatmap colour · click it to hide/show that camera.
      </p>

      {/* Coverage context for the per-tile bars: the day's shared axis extent,
          plus the warning when the cameras' recording windows barely overlap. */}
      {cov && cov.cameras.length > 0 ? (
        <div className="mt-2 flex items-baseline justify-between gap-2 flex-wrap text-[11px] text-gray-tertiary">
          <span>Bars = when each camera recorded</span>
          <span className="font-mono tabular-nums">
            {hhmm(cov.min_ts)}–{hhmm(cov.max_ts)}
          </span>
        </div>
      ) : null}
      {cov?.uneven ? <UnevenNotice /> : null}

      <div className="flex flex-col gap-3 mt-4">
        {cameras.map((cam) => {
          const cs = state[cam];
          const isActive = cam === active;
          const isShown = cam === focused;
          const off = hidden?.has(cam) ?? false;
          const color = cameraColor(cameras, cam);
          const shownIdx = shownIdxFor(cam);
          return (
            <div key={cam} className="flex flex-col gap-1.5">
              <div className="flex items-stretch">
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
                      {placeholderText(cs, frame, shownIdx)}
                    </div>
                  )}
                  {/* gear — opens the count-area editor for this camera. Editing
                      is poweruser-gated (saving 403s and the route bounces), so
                      viewers don't get a door that only leads to a wall. */}
                  {canManage && dataset ? (
                    <span
                      role="button"
                      tabIndex={0}
                      onClick={(e) => {
                        e.stopPropagation();
                        e.preventDefault();
                        navigate(`/count-area/${dataset}/${cam}`);
                      }}
                      onKeyDown={(e) => {
                        if (e.key === "Enter" || e.key === " ") {
                          e.stopPropagation();
                          e.preventDefault();
                          navigate(`/count-area/${dataset}/${cam}`);
                        }
                      }}
                      title={`Edit count areas for ${cam}`}
                      className="absolute top-2 right-2 grid place-items-center w-7 h-7 rounded-lg bg-black/45 text-white opacity-0 group-hover:opacity-100 hover:bg-black/70 transition-opacity duration-150 cursor-pointer"
                    >
                      <CogIcon className="w-4 h-4" />
                    </span>
                  ) : null}
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

              {/* when this camera actually recorded, on the day's shared axis */}
              {cov ? <CoverageBar cov={cov} camera={cam} color={color} dimmed={off} /> : null}
            </div>
          );
        })}
      </div>

      {cov && cov.cameras.length > 0 ? (
        <p className="mt-3 text-[11px] leading-snug text-gray-tertiary">
          A gap in a bar means no footage at that time.
          {canManage ? " Trim an over-long stream from the camera manager (⚙ above)." : ""}
        </p>
      ) : null}
    </Panel>
  );
}
