import { useEffect, useState } from "react";
import type { FrameDetection, ImgMeta } from "../lib/types";
import { frameImg, getFrameDetections } from "../lib/api";
import { AttrBadges, AttrLegend, scoredCount } from "./AttrBadges";
import { ImageClicker } from "./ImageClicker";
import { Chip } from "./ui";

/**
 * Enlarged single-camera view, rendered in the heatmap's centre real estate
 * (not a pop-over) so the scrubber and KPIs stay in context. Overlay/Raw
 * toggle; when the camera's reference dimensions are known it reuses the
 * ImageClicker viewer for scroll-to-zoom / drag-to-pan mask inspection.
 *
 * The crop model's verdicts are a separate, client-drawn layer over whichever
 * image is showing (AttrBadges): fetched per frame from /api/frame-detections,
 * never burned into the PNG, so a re-score is a DB update and a style change
 * costs no re-render. The layer only offers itself once the frame has scored
 * rows — on an unscored day there is no chip and nothing to explain.
 */
export default function CameraDetail({
  camera,
  frameIdx,
  meta,
  poseEnabled = false,
  onClose,
}: {
  camera: string;
  frameIdx: number | null;
  meta?: ImgMeta; // reference dims → zoom/pan viewer; falls back to a scaled <img>
  poseEnabled?: boolean; // show the Pose (keypoint skeleton) layer toggle
  onClose: () => void;
}) {
  const [kind, setKind] = useState<"overlay" | "raw" | "pose">("overlay");
  const [showAttrs, setShowAttrs] = useState(true);
  const [dets, setDets] = useState<FrameDetection[]>([]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  // The verdicts for this frame. A failure is an empty layer, never an error
  // state: the image is the product, the badges are annotation on it.
  useEffect(() => {
    let alive = true;
    setDets([]);
    if (frameIdx == null) return;
    getFrameDetections(camera, frameIdx)
      .then((rows) => {
        if (alive) setDets(rows);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [camera, frameIdx]);

  const src = frameIdx != null ? frameImg(camera, frameIdx, kind) : null;
  const scored = scoredCount(dets);
  const unsure = dets.filter((d) => d.attr_model_rev !== null && d.attr_behaviour === null).length;
  const attrsOn = showAttrs && scored > 0;

  return (
    <div className="animate-fade-slide-in">
      <div className="mb-3 flex items-center gap-2">
        <div className="flex items-center gap-1.5">
          <Chip active={kind === "overlay"} onClick={() => setKind("overlay")}>
            Overlay
          </Chip>
          <Chip active={kind === "raw"} onClick={() => setKind("raw")}>
            Raw
          </Chip>
          {poseEnabled ? (
            <Chip active={kind === "pose"} onClick={() => setKind("pose")}>
              Pose
            </Chip>
          ) : null}
          {scored > 0 ? (
            <>
              <span className="mx-1 h-4 w-px bg-border" aria-hidden="true" />
              <Chip active={showAttrs} onClick={() => setShowAttrs((v) => !v)}>
                Attributes
              </Chip>
            </>
          ) : null}
        </div>
        <button
          onClick={onClose}
          className="ml-auto font-mono text-[12px] text-gray-tertiary hover:text-near-black transition-colors duration-150"
        >
          ← Map
        </button>
      </div>

      {src == null ? (
        <div className="grid aspect-video place-items-center rounded-2xl border border-border bg-surface-sunk font-mono text-[11px] text-gray-tertiary">
          no frame at this time
        </div>
      ) : meta ? (
        <ImageClicker
          src={src}
          naturalWidth={meta.width}
          naturalHeight={meta.height}
          points={[]}
          interactive={false}
          title={`${camera} · ${kind}`}
          overlay={
            attrsOn
              ? (invScale, screenScale) => (
                  <svg
                    className="absolute inset-0 w-full h-full"
                    viewBox={`0 0 ${meta.width} ${meta.height}`}
                    preserveAspectRatio="none"
                  >
                    <AttrBadges
                      dets={dets}
                      naturalWidth={meta.width}
                      invScale={invScale}
                      screenScale={screenScale}
                    />
                  </svg>
                )
              : undefined
          }
        />
      ) : (
        <PlainFrame src={src} alt={`${camera} ${kind}`} dets={attrsOn ? dets : []} />
      )}

      {attrsOn ? <AttrLegend unsure={unsure} total={scored} /> : null}
    </div>
  );
}

/* The no-reference-dimensions fallback: a scaled <img> with the badge layer
   sized off the image's own natural size once it has loaded. Without a zoom
   there is nothing to counter-scale, but the SVG is CSS-scaled with the image,
   so badges are sized in natural px at (natural / displayed) to land at their
   intended on-screen size. */
function PlainFrame({ src, alt, dets }: { src: string; alt: string; dets: FrameDetection[] }) {
  const [dims, setDims] = useState<{ w: number; h: number; cssW: number } | null>(null);
  return (
    <div className="relative mx-auto" style={{ maxHeight: "68vh", maxWidth: "100%", width: "fit-content" }}>
      <img
        src={src}
        alt={alt}
        className="block border border-border"
        style={{ maxHeight: "68vh", maxWidth: "100%" }}
        onLoad={(e) => {
          const el = e.currentTarget;
          setDims({ w: el.naturalWidth, h: el.naturalHeight, cssW: el.clientWidth });
        }}
      />
      {dims && dets.length > 0 ? (
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none"
          viewBox={`0 0 ${dims.w} ${dims.h}`}
          preserveAspectRatio="none"
        >
          <AttrBadges dets={dets} naturalWidth={dims.w} screenScale={dims.w / Math.max(1, dims.cssW)} />
        </svg>
      ) : null}
    </div>
  );
}
