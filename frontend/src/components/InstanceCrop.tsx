import { useState } from "react";
import type { LabelItem } from "../lib/types";

/* How dark the context outside the ring goes. Dark enough that the eye lands on
   the ringed animal first — several cows usually share a crop and every answer is
   about exactly one of them — but not so dark that the surrounding ground is
   unreadable: "is that a panel shadow or wet ground?" is judged from the pixels
   AROUND the animal as much as from the animal itself. */
const SCRIM_OPACITY = 0.45;
/* Two strokes, dark under light, so the ring survives both a sunlit white flank
   and black panel shade. A single colour disappears against one of them, and a
   ring you cannot find is a wrong label. Both are non-scaling, so these are
   rendered px at any display size. */
const HALO_STROKE = 3.5;
const RING_STROKE = 1.75;

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

export interface InstanceCropProps {
  item: LabelItem;
  /** `H` held down: drop the ring AND the scrim, so occlusion can be judged on
      unobstructed pixels. Both go, not just the stroke — a scrim over unringed
      context is exactly the obstruction the affordance exists to lift. */
  hideRing?: boolean;
  /** The crop 404'd or the server refused it (a mostly-banner tile). Carries the
      key rather than nothing so a late error from an item already advanced past
      can be recognised and dropped by the caller. */
  onError?: (instanceKey: string) => void;
  className?: string;
}

/**
 * The instance under judgement: the server-built crop with the ring and an
 * even-odd spotlight scrim drawn OVER it as SVG.
 *
 * The ring is deliberately not baked into the image (§4.5): drawing it here is
 * what makes hold-to-hide cost no network, keeps the stroke a hairline at any
 * rendered size, and means restyling it does not invalidate every cached crop.
 * Geometry comes from the item — `ring` is already in crop-local pixels of a
 * `crop_w` x `crop_h` canvas — so nothing here needs the frame's dimensions.
 *
 * Responsive correctness is structural, not arithmetic: the image and the SVG are
 * both stretched into the same absolutely-positioned box, the image by its default
 * `object-fit: fill` and the SVG by `preserveAspectRatio="none"`. Those two map
 * source pixels to box pixels identically, so the ring tracks the animal at any
 * width and even if a caller squeezes the box out of the crop's aspect ratio. A
 * `meet`-fitted SVG over a letterboxed image is where that silently drifts.
 *
 * No `animate-fade-slide-in` here: a 600 ms fade firing every ~5 s for an hour is
 * nauseating and delays the pixels the annotator is waiting for. The caption
 * (day + camera, never the clock time) belongs to the page, not to the crop.
 */
export function InstanceCrop({ item, hideRing = false, onError, className }: InstanceCropProps) {
  // Keyed by URL rather than a boolean so the state cannot survive into the next
  // item and blank out a crop that is perfectly fine — the annotator would be
  // told the image is missing while the server is happily serving it.
  const [failedUrl, setFailedUrl] = useState<string | null>(null);
  const failed = failedUrl === item.crop_url;

  const w = Math.max(item.crop_w, 1);
  const h = Math.max(item.crop_h, 1);
  const [rx0, ry0, rx1, ry1] = item.ring;
  const rx = clamp(rx0, 0, w);
  const ry = clamp(ry0, 0, h);
  const rw = Math.max(clamp(rx1, 0, w) - rx, 1);
  const rh = Math.max(clamp(ry1, 0, h) - ry, 1);

  // Outer rectangle plus the ring rectangle as a second subpath. Even-odd makes
  // the inner one a hole regardless of winding direction, which is why the two
  // subpaths can both be emitted clockwise.
  const scrim = `M0 0H${w}V${h}H0Z M${rx} ${ry}H${rx + rw}V${ry + rh}H${rx}Z`;

  return (
    <div
      className={
        "relative overflow-hidden rounded-xl border border-border bg-surface-sunk" +
        (className ? " " + className : "")
      }
      // The box holds the crop's shape before the image lands, so the ring never
      // draws over a collapsed element and the layout does not jolt between items.
      style={{ aspectRatio: `${w} / ${h}` }}
    >
      {failed ? (
        <div className="absolute inset-0 grid place-items-center px-4 text-center font-mono text-[11px] text-gray-tertiary">
          crop unavailable
        </div>
      ) : (
        <img
          // Keyed so a new item mounts a fresh element instead of holding the
          // previous cow on screen until the next decode finishes. That stale
          // frame is answerable — the annotator would label the wrong animal.
          key={item.crop_url}
          src={item.crop_url}
          alt={`the ringed cow to label — ${item.camera_id}${item.day ? `, ${item.day}` : ""}`}
          className="absolute inset-0 w-full h-full"
          decoding="async"
          draggable={false}
          onError={() => {
            setFailedUrl(item.crop_url);
            onError?.(item.instance_key);
          }}
        />
      )}

      {failed ? null : (
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none transition-opacity duration-100"
          viewBox={`0 0 ${w} ${h}`}
          preserveAspectRatio="none"
          style={{ opacity: hideRing ? 0 : 1 }}
          aria-hidden="true"
        >
          <path d={scrim} fill="#000" fillOpacity={SCRIM_OPACITY} fillRule="evenodd" />
          <rect
            x={rx}
            y={ry}
            width={rw}
            height={rh}
            fill="none"
            stroke="#000"
            strokeOpacity={0.55}
            strokeWidth={HALO_STROKE}
            vectorEffect="non-scaling-stroke"
          />
          <rect
            x={rx}
            y={ry}
            width={rw}
            height={rh}
            fill="none"
            stroke="#fff"
            strokeWidth={RING_STROKE}
            vectorEffect="non-scaling-stroke"
          />
        </svg>
      )}
    </div>
  );
}
