import { useEffect, useRef, useState } from "react";
import type { CSSProperties, PointerEvent as RPointerEvent } from "react";
import { SectionLabel } from "../components/ui";
import { CHART_PRIMARY } from "../lib/palette";

const MIN_SCALE = 1;
const MAX_SCALE = 16;
const DRAG_THRESHOLD = 4; // px of pointer travel before a press counts as a pan

function clamp(v: number, lo: number, hi: number) {
  return Math.min(hi, Math.max(lo, v));
}

/**
 * Zoom/pan image viewer with click-to-place.
 * - scroll wheel: zoom toward the cursor
 * - drag: pan (only once it exceeds a small threshold — otherwise it's a click)
 * - click: place a point (mapped to full-resolution/natural pixels)
 *
 * Placed markers keep a constant on-screen size regardless of zoom so precise
 * placement stays readable when zoomed in.
 */
export function ImageClicker({
  src,
  naturalWidth,
  naturalHeight,
  points,
  onPlace,
  interactive = true,
  activeIndex,
  title,
}: {
  src: string;
  naturalWidth: number;
  naturalHeight: number;
  points: number[][];
  onPlace?: (pt: [number, number]) => void;
  interactive?: boolean;
  activeIndex?: number;
  title?: string;
}) {
  const viewportRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement>(null);

  // View transform. Refs mirror state so wheel/pan math never reads stale values.
  const [view, setView] = useState({ scale: 1, tx: 0, ty: 0 });
  const viewRef = useRef(view);
  viewRef.current = view;

  const drag = useRef<{
    startX: number;
    startY: number;
    baseTx: number;
    baseTy: number;
    moved: boolean;
  } | null>(null);
  const [dragging, setDragging] = useState(false);

  // Reset the view whenever the image changes.
  useEffect(() => {
    setView({ scale: 1, tx: 0, ty: 0 });
  }, [src]);

  // Keep content covering the viewport (content fills it exactly at scale 1).
  function apply(scale: number, tx: number, ty: number) {
    const vp = viewportRef.current;
    if (!vp) return setView({ scale, tx, ty });
    const w = vp.clientWidth;
    const h = vp.clientHeight;
    setView({
      scale,
      tx: clamp(tx, w * (1 - scale), 0),
      ty: clamp(ty, h * (1 - scale), 0),
    });
  }

  // Non-passive wheel listener so we can preventDefault (stops page scroll).
  useEffect(() => {
    const vp = viewportRef.current;
    if (!vp) return;
    function onWheel(e: WheelEvent) {
      e.preventDefault();
      const rect = vp!.getBoundingClientRect();
      const cx = e.clientX - rect.left;
      const cy = e.clientY - rect.top;
      const { scale, tx, ty } = viewRef.current;
      const factor = e.deltaY < 0 ? 1.2 : 1 / 1.2;
      const next = clamp(scale * factor, MIN_SCALE, MAX_SCALE);
      // Keep the content point under the cursor fixed while zooming.
      const contentX = (cx - tx) / scale;
      const contentY = (cy - ty) / scale;
      apply(next, cx - contentX * next, cy - contentY * next);
    }
    vp.addEventListener("wheel", onWheel, { passive: false });
    return () => vp.removeEventListener("wheel", onWheel);
  }, []);

  function place(clientX: number, clientY: number) {
    if (!interactive || !onPlace) return;
    const img = imgRef.current;
    if (!img) return;
    const rect = img.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return;
    const ratioX = clamp((clientX - rect.left) / rect.width, 0, 1);
    const ratioY = clamp((clientY - rect.top) / rect.height, 0, 1);
    onPlace([ratioX * naturalWidth, ratioY * naturalHeight]);
  }

  function onPointerDown(e: RPointerEvent<HTMLDivElement>) {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = {
      startX: e.clientX,
      startY: e.clientY,
      baseTx: view.tx,
      baseTy: view.ty,
      moved: false,
    };
  }

  function onPointerMove(e: RPointerEvent<HTMLDivElement>) {
    const d = drag.current;
    if (!d) return;
    const dx = e.clientX - d.startX;
    const dy = e.clientY - d.startY;
    if (!d.moved && Math.hypot(dx, dy) < DRAG_THRESHOLD) return;
    if (!d.moved) setDragging(true);
    d.moved = true;
    apply(viewRef.current.scale, d.baseTx + dx, d.baseTy + dy);
  }

  function onPointerUp(e: RPointerEvent<HTMLDivElement>) {
    const d = drag.current;
    drag.current = null;
    setDragging(false);
    if (d && !d.moved) place(e.clientX, e.clientY);
  }

  const { scale, tx, ty } = view;
  const invScale = 1 / scale;
  const zoomed = scale > 1.001;

  return (
    <div className="animate-fade-slide-in">
      {title ? (
        <div className="mb-2 flex items-center justify-between">
          <SectionLabel>{title}</SectionLabel>
          <span className="font-mono text-[10px] text-gray-tertiary">
            scroll to zoom · drag to pan{interactive ? " · click to place" : ""}
          </span>
        </div>
      ) : null}
      <div
        ref={viewportRef}
        className={
          "relative overflow-hidden border bg-surface select-none " +
          (interactive ? "border-accent cursor-crosshair" : "border-border") +
          (dragging ? " cursor-grabbing" : "")
        }
        style={{
          aspectRatio: `${naturalWidth} / ${naturalHeight}`,
          maxHeight: "68vh",
          margin: "0 auto",
          touchAction: "none",
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={() => {
          drag.current = null;
          setDragging(false);
        }}
      >
        <div
          className="absolute top-0 left-0 w-full"
          style={{
            transform: `translate(${tx}px, ${ty}px) scale(${scale})`,
            transformOrigin: "0 0",
          }}
        >
          <img ref={imgRef} src={src} className="w-full block" draggable={false} />
          <div className="absolute inset-0 pointer-events-none">
            {points.map((p, i) => (
              <div
                key={i}
                className="absolute"
                style={{
                  left: `${(p[0] / naturalWidth) * 100}%`,
                  top: `${(p[1] / naturalHeight) * 100}%`,
                  transform: `translate(-50%, -50%) scale(${invScale})`,
                  transformOrigin: "center",
                }}
              >
                <div
                  className="absolute font-mono text-[11px] text-near-black"
                  style={{
                    left: "50%",
                    bottom: "100%",
                    transform: "translateX(-50%)",
                    marginBottom: 3,
                    lineHeight: 1,
                  }}
                >
                  {i + 1}
                </div>
                <div style={markerStyle(i === activeIndex)} />
              </div>
            ))}
          </div>
        </div>

        {zoomed ? (
          <button
            onPointerDown={(e) => e.stopPropagation()}
            onPointerUp={(e) => e.stopPropagation()}
            onClick={() => setView({ scale: 1, tx: 0, ty: 0 })}
            className="absolute bottom-2 right-2 font-mono text-[10px] px-2 py-1 bg-near-black/70 text-white border border-border hover:border-accent"
          >
            {scale.toFixed(1)}× · reset
          </button>
        ) : null}
      </div>
    </div>
  );
}

function markerStyle(active: boolean): CSSProperties {
  return {
    width: active ? 14 : 10,
    height: active ? 14 : 10,
    background: active ? "#e76f51" : CHART_PRIMARY,
    border: `2px solid ${active ? "#fff" : "#05261a"}`,
    borderRadius: "9999px",
    boxShadow: active ? "0 0 0 2px #e76f51" : "none",
  };
}
