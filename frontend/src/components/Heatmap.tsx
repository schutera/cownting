import { useEffect, useRef, useState } from "react";
import type { HeatmapData } from "../lib/types";
import { getHeatmap, orthoImg } from "../lib/api";
import { CHART_PRIMARY } from "../lib/palette";
import { Card } from "../components/ui";

export function Heatmap() {
  const [data, setData] = useState<HeatmapData | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    let alive = true;
    getHeatmap().then((d) => {
      if (alive) setData(d);
    });
    return () => {
      alive = false;
    };
  }, []);

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

    const radius = Math.max(w, h) * 0.05;
    ctx.globalCompositeOperation = "lighter";

    for (const pt of data.points) {
      const cx = (pt[0] / orthoW) * w;
      const cy = (pt[1] / orthoH) * h;
      const grad = ctx.createRadialGradient(cx, cy, 0, cx, cy, radius);
      grad.addColorStop(0, hexToRgba(CHART_PRIMARY, 0.35));
      grad.addColorStop(1, hexToRgba(CHART_PRIMARY, 0));
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(cx, cy, radius, 0, Math.PI * 2);
      ctx.fill();
    }

    ctx.globalCompositeOperation = "source-over";
  }

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => draw());
    ro.observe(container);
    draw();
    return () => ro.disconnect();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [data]);

  if (!data) {
    return (
      <Card>
        <span className="font-mono text-[13px] text-gray-mid">Loading…</span>
      </Card>
    );
  }

  if (!data.orthophoto || data.points.length === 0) {
    return (
      <div className="bg-surface border border-border p-6 animate-fade-slide-in">
        <div className="text-gray-mid font-sans">No localized detections yet.</div>
        <div className="text-[11px] font-mono text-gray-tertiary mt-2">
          Calibrate a camera, then the heatmap fills in.
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
        <canvas ref={canvasRef} className="absolute inset-0 pointer-events-none" />
      </div>
      <div className="text-[11px] font-mono text-gray-tertiary mt-2">
        {data.points.length} localized detections
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
