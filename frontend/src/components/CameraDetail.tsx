import { useEffect, useState } from "react";
import type { ImgMeta } from "../lib/types";
import { frameImg } from "../lib/api";
import { ImageClicker } from "./ImageClicker";
import { Chip } from "./ui";

/**
 * Enlarged single-camera view, rendered in the heatmap's centre real estate
 * (not a pop-over) so the scrubber and KPIs stay in context. Overlay/Raw
 * toggle; when the camera's reference dimensions are known it reuses the
 * ImageClicker viewer for scroll-to-zoom / drag-to-pan mask inspection.
 */
export default function CameraDetail({
  camera,
  frameIdx,
  meta,
  onClose,
}: {
  camera: string;
  frameIdx: number | null;
  meta?: ImgMeta; // reference dims → zoom/pan viewer; falls back to a scaled <img>
  onClose: () => void;
}) {
  const [kind, setKind] = useState<"overlay" | "raw">("overlay");

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const src = frameIdx != null ? frameImg(camera, frameIdx, kind) : null;

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
        </div>
        <button
          onClick={onClose}
          className="ml-auto font-mono text-[12px] text-gray-tertiary hover:text-near-black transition-colors duration-150"
        >
          ← Heatmap
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
        />
      ) : (
        <img
          src={src}
          alt={`${camera} ${kind}`}
          className="mx-auto block border border-border"
          style={{ maxHeight: "68vh", maxWidth: "100%" }}
        />
      )}
    </div>
  );
}
