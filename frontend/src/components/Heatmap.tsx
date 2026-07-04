import { useEffect, useRef, useState } from "react";
import type { HeatmapData, Panel } from "../lib/types";
import { getHeatmap, orthoImg } from "../lib/api";
import { ACCENT_COLORS, SHELTER_COLOR } from "../lib/palette";
import { Card } from "../components/ui";

export function Heatmap({
  frame,
  windowMin = 30,
  fence,
  panels,
  cameras,
  hidden,
}: {
  frame?: number | null;
  windowMin?: number;
  fence?: number[][] | null;
  panels?: Panel[] | null; // ortho panel centre lines, drawn as shade overlay
  cameras?: string[];
  hidden?: Set<string>; // cameras de-selected via the seg-stack bars; hidden from the heatmap
} = {}) {
  const [data, setData] = useState<HeatmapData | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    let alive = true;
    getHeatmap(frame, windowMin).then((d) => {
      if (alive) setData(d);
    });
    return () => {
      alive = false;
    };
  }, [frame, windowMin]);

  // Stable colour ordering: prefer the caller-supplied `cameras` order, else
  // fall back to the sorted unique set of cams actually in the data.
  const order =
    cameras && cameras.length
      ? cameras
      : Array.from(new Set(data?.cams ?? [])).sort();
  function colorFor(cam?: string): string {
    const idx = cam ? order.indexOf(cam) : -1;
    const i = idx >= 0 ? idx : 0;
    return ACCENT_COLORS[i % ACCENT_COLORS.length];
  }

  function draw() {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img || !data || !data.orthophoto) return;
    const w = img.clientWidth;
    const h = img.clientHeight;
    if (w === 0 || h === 0) return;
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, w, h);

    const orthoW = data.orthophoto.width;
    const orthoH = data.orthophoto.height;
    if (orthoW === 0 || orthoH === 0) return;

    // Smaller footprint so individual positions read clearly (esp. in the
    // narrow ±window view) instead of merging into one blob. Each point is
    // tinted by the camera that produced it, so misplaced clusters are
    // traceable to a source camera.
    const radius = Math.max(4, Math.min(w, h) * 0.018);
    ctx.globalCompositeOperation = "lighter";
    data.points.forEach((pt, i) => {
      const cam = data.cams?.[i];
      if (cam && hidden?.has(cam)) return;
      const cx = (pt[0] / orthoW) * w;
      const cy = (pt[1] / orthoH) * h;
      const color = colorFor(cam);
      const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
      grad.addColorStop(0, hexToRgba(color, 0.5));
      grad.addColorStop(1, hexToRgba(color, 0));
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.fill();
    });

    // Crisp core dot per detection for precise placement readout.
    ctx.globalCompositeOperation = "source-over";
    data.points.forEach((pt, i) => {
      const cam = data.cams?.[i];
      if (cam && hidden?.has(cam)) return;
      const cx = (pt[0] / orthoW) * w;
      const cy = (pt[1] / orthoH) * h;
      ctx.fillStyle = hexToRgba(colorFor(cam), 0.9);
      ctx.beginPath();
      ctx.arc(cx, cy, 1.6, 0, Math.PI * 2);
      ctx.fill();
    });

    // Current-frame highlight: cows at the exact slider minute get a prominent
    // camera-coloured marker on top, so the user gets live "right now" feedback.
    if (frame != null) {
      data.points.forEach((pt, i) => {
        if (data.frames?.[i] !== frame) return;
        const cam = data.cams?.[i];
        if (cam && hidden?.has(cam)) return;
        const cx = (pt[0] / orthoW) * w;
        const cy = (pt[1] / orthoH) * h;
        ctx.beginPath();
        ctx.arc(cx, cy, 5, 0, Math.PI * 2);
        ctx.fillStyle = colorFor(cam);
        ctx.fill();
        ctx.strokeStyle = "rgba(255,255,255,0.95)";
        ctx.lineWidth = 1.5;
        ctx.stroke();
      });
    }

    // Fenced enclosure outline (site-wide), for context.
    if (fence && fence.length >= 3) {
      ctx.strokeStyle = "rgba(231,111,81,0.9)";
      ctx.lineWidth = 1.5;
      ctx.setLineDash([6, 4]);
      ctx.beginPath();
      fence.forEach((p, i) => {
        const fx = (p[0] / orthoW) * w;
        const fy = (p[1] / orthoH) * h;
        if (i === 0) ctx.moveTo(fx, fy);
        else ctx.lineTo(fx, fy);
      });
      ctx.closePath();
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // Solar-panel centre lines (site-wide), so shelter clusters read against the
    // panels they sit under. Drawn as OPEN polylines (no closePath/fill). Teal +
    // dashed keeps them distinct from the fence's terracotta outline. Same
    // coord-scaling as the fence.
    if (panels && panels.length) {
      ctx.strokeStyle = hexToRgba(SHELTER_COLOR, 0.9);
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 3]);
      panels.forEach((panel) => {
        const line = panel.centerline;
        if (!line || line.length < 2) return;
        ctx.beginPath();
        line.forEach((p, i) => {
          const px = (p[0] / orthoW) * w;
          const py = (p[1] / orthoH) * h;
          if (i === 0) ctx.moveTo(px, py);
          else ctx.lineTo(px, py);
        });
        ctx.stroke();
      });
      ctx.setLineDash([]);
    }
  }

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(container);
    draw();
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data, fence, panels, frame, cameras, hidden]);

  if (!data) {
    return (
      <Card>
        <span className="font-mono text-[13px] text-gray-mid">Loading…</span>
      </Card>
    );
  }

  const windowed = frame !== undefined && frame !== null;

  // Points remaining after camera de-selection (drives the caption count).
  const shownCount = data.points.filter((_, i) => {
    const cam = data.cams?.[i];
    return !(cam && hidden?.has(cam));
  }).length;

  // Only fall back to a text card when there's no orthophoto to show at all.
  // Otherwise always render the map — an empty heatmap still shows the ortho.
  if (!data.orthophoto) {
    return (
      <div className="bg-surface border border-border p-6 animate-fade-slide-in">
        <div className="text-gray-mid font-sans">No orthophoto configured.</div>
        <div className="text-[11px] font-mono text-gray-tertiary mt-2">
          Set paths.orthophoto in the site config to enable the heatmap.
        </div>
      </div>
    );
  }

  return (
    <div className="animate-fade-slide-in">
      <div ref={containerRef} className="relative border border-border bg-surface">
        <img
          ref={imgRef}
          src={orthoImg()}
          className="w-full block"
          onLoad={draw}
        />
        <canvas
          ref={canvasRef}
          className="absolute inset-0 pointer-events-none"
          style={{ opacity: 0.7 }}
        />
      </div>
      <div className="text-[11px] font-mono text-gray-tertiary mt-2">
        {shownCount === 0
          ? windowed
            ? `No cows in the last ${windowMin} min`
            : "No localized detections yet — calibrate a camera"
          : `${shownCount} of ${data.points.length} localized detections${
              windowed ? ` · last ${windowMin} min` : " · whole day"
            }`}
      </div>
    </div>
  );
}

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
