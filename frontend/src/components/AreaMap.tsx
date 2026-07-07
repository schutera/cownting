import { useEffect, useRef, useState } from "react";
import type { Areas, PostureBreakdown } from "../lib/types";
import { getAreas, getAreaCounts, orthoImg } from "../lib/api";
import {
  HEAT_HOT,
  SHELTER_COLOR,
  CHART_MUTED,
  REST_COLOR,
  ACTIVE_COLOR,
} from "../lib/palette";
import { Card } from "../components/ui";

const PULSE_MS = 650; // duration of the on-change pulse per area badge

/**
 * Occupancy map, count-area edition. Same orthophoto <img> + absolute <canvas>
 * overlay + ResizeObserver technique as the old Heatmap, but instead of raw
 * localized points it draws, for every count area across every camera, its
 * ortho polygon outline and a count badge at the polygon centroid. Behind each
 * badge sits an aura — a radial glow whose radius/opacity grow with the current
 * count and whose hue encodes the delta since the previous render (rising =>
 * warm heat, falling => cool teal, flat => neutral) with a subtle pulse on
 * change. The count is the cows present in each area AT THE CURRENT FRAME
 * (`frame`); it does not accumulate over a window.
 */
export function AreaMap({
  frame,
  cameras,
  hidden,
}: {
  frame?: number | null;
  cameras?: string[]; // caller-supplied camera order (kept for drop-in parity)
  hidden?: Set<string>; // cameras de-selected via the seg-stack bars; hidden here too
} = {}) {
  const [areas, setAreas] = useState<Areas | null>(null);
  const [counts, setCounts] = useState<Record<string, number>>({});
  // Per-area posture composition (standing/lying/unknown) driving the badge ring.
  const [postures, setPostures] = useState<Record<string, PostureBreakdown>>({});
  // Per-area cows under a panel — drawn as unit blocks stacked above the circle.
  const [sheltering, setSheltering] = useState<Record<string, number>>({});
  const containerRef = useRef<HTMLDivElement | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  // Previous counts + last delta sign per region_id, and the timestamp a region
  // last changed (drives the decaying pulse). All refs so they survive redraws.
  const prevCountsRef = useRef<Record<string, number>>({});
  const deltaRef = useRef<Record<string, number>>({});
  const pulseStartRef = useRef<Record<string, number>>({});
  const rafRef = useRef<number | null>(null);

  useEffect(() => {
    let alive = true;
    getAreas()
      .then((a) => alive && setAreas(a))
      .catch(() => alive && setAreas({}));
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    let alive = true;
    getAreaCounts(frame ?? undefined)
      .then((d) => {
        if (!alive) return;
        setCounts(d.counts ?? {});
        setPostures(d.postures ?? {});
        setSheltering(d.sheltering ?? {});
      })
      .catch(() => {
        /* counts are optional — the map still renders the area outlines */
      });
    return () => {
      alive = false;
    };
  }, [frame]);

  // On new counts, record the delta vs the previous render (colours the aura)
  // and stamp a pulse start for any region whose count actually moved.
  useEffect(() => {
    const prev = prevCountsRef.current;
    const now = performance.now();
    const keys = new Set([...Object.keys(prev), ...Object.keys(counts)]);
    keys.forEach((rid) => {
      const cur = counts[rid] ?? 0;
      const was = prev[rid];
      deltaRef.current[rid] = cur - (was ?? 0);
      // Only pulse on a genuine change (skip the very first appearance).
      if (was !== undefined && cur !== was) pulseStartRef.current[rid] = now;
    });
    prevCountsRef.current = { ...counts };
  }, [counts]);

  function draw(now: number) {
    const canvas = canvasRef.current;
    const img = imgRef.current;
    if (!canvas || !img || !areas) return;
    const w = img.clientWidth;
    const h = img.clientHeight;
    if (w === 0 || h === 0) return;
    const ow = img.naturalWidth;
    const oh = img.naturalHeight;
    if (ow === 0 || oh === 0) return;
    canvas.width = w;
    canvas.height = h;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    ctx.clearRect(0, 0, w, h);
    const sx = w / ow; // ortho-native px -> displayed px
    const sy = h / oh;
    const base = Math.min(w, h);

    // --- Pass 1: gather one drawable per visible area, anchored at its ortho
    // polygon centroid, plus its per-render pulse / delta hue (time-fixed for
    // this frame). `x/y` start at the anchor and get nudged apart below.
    type Item = {
      area: (typeof areas)[string][number];
      rid: string;
      poly: number[][];
      ax: number; // anchor (true centroid) — where the leader line starts
      ay: number;
      x: number; // drawn badge position (relaxed to avoid overlaps)
      y: number;
      count: number;
      pulse: number;
      auraColor: string;
    };
    const items: Item[] = [];
    Object.entries(areas).forEach(([cam, list]) => {
      if (hidden?.has(cam)) return;
      list.forEach((area) => {
        const poly = area.ortho_polygon;
        if (!poly || poly.length < 3) return;
        const rid = `${cam}::${area.id}`;
        let sxSum = 0;
        let sySum = 0;
        poly.forEach((p) => {
          sxSum += p[0];
          sySum += p[1];
        });
        const ax = (sxSum / poly.length) * sx;
        const ay = (sySum / poly.length) * sy;
        // Decaying pulse factor (0..1) for a short bump after a change.
        const start = pulseStartRef.current[rid];
        let pulse = 0;
        if (start !== undefined) {
          const t = (now - start) / PULSE_MS;
          if (t >= 0 && t < 1) pulse = Math.sin(Math.PI * t) * (1 - t);
        }
        // Aura hue by delta vs the previous render.
        const d = deltaRef.current[rid] ?? 0;
        const auraColor = d > 0 ? HEAT_HOT : d < 0 ? SHELTER_COLOR : CHART_MUTED;
        items.push({
          area,
          rid,
          poly,
          ax,
          ay,
          x: ax,
          y: ay,
          count: counts[rid] ?? 0,
          pulse,
          auraColor,
        });
      });
    });

    // Uniform badge footprint (badge disc + posture ring + a band for the name
    // label). Camera views overlap on the ortho, so several centroids can land
    // within a badge's width — relax the badge POSITIONS apart (outlines stay
    // put) so no two discs/labels collide, keeping each near its own area.
    const badgeR0 = base * 0.028 + 6;
    const ringW0 = Math.max(2.5, badgeR0 * 0.3);
    const ringOuter0 = badgeR0 + ringW0 + 3;
    const labelBand = base * 0.02 + 8;
    const sep = 2 * ringOuter0 + labelBand; // min center-to-center distance
    const margin = ringOuter0 + 4;
    for (let iter = 0; iter < 80; iter++) {
      for (let i = 0; i < items.length; i++) {
        for (let k = i + 1; k < items.length; k++) {
          const A = items[i];
          const B = items[k];
          const dx = B.x - A.x;
          const dy = B.y - A.y;
          let dist = Math.hypot(dx, dy);
          if (dist >= sep) continue;
          let nx: number;
          let ny: number;
          if (dist < 0.001) {
            // Coincident centroids — separate along a deterministic direction.
            const ang = i * 2.3999632; // golden angle, no RNG (RNG is unavailable)
            nx = Math.cos(ang);
            ny = Math.sin(ang);
            dist = 0;
          } else {
            nx = dx / dist;
            ny = dy / dist;
          }
          const push = (sep - dist) / 2;
          A.x -= nx * push;
          A.y -= ny * push;
          B.x += nx * push;
          B.y += ny * push;
        }
      }
      // Gentle spring back toward the true centroid + clamp inside the canvas,
      // so badges settle as close to their areas as the no-overlap rule allows.
      for (const it of items) {
        it.x += (it.ax - it.x) * 0.04;
        it.y += (it.ay - it.y) * 0.04;
        it.x = Math.max(margin, Math.min(w - margin, it.x));
        it.y = Math.max(margin, Math.min(h - margin, it.y));
      }
    }

    // --- Pass 2: auras (additive glow) for all areas first, so a neighbour's
    // glow never washes over another badge.
    ctx.globalCompositeOperation = "lighter";
    for (const it of items) {
      const auraR =
        (base * 0.05 + Math.sqrt(it.count) * base * 0.03) * (1 + 0.25 * it.pulse);
      const auraA = Math.min(0.55, 0.12 + it.count * 0.05) * (1 + 0.4 * it.pulse);
      if (it.count > 0 || it.pulse > 0) {
        const grad = ctx.createRadialGradient(it.x, it.y, 0, it.x, it.y, auraR);
        grad.addColorStop(0, hexToRgba(it.auraColor, auraA));
        grad.addColorStop(1, hexToRgba(it.auraColor, 0));
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(it.x, it.y, auraR, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    ctx.globalCompositeOperation = "source-over";

    // --- Pass 3: area outlines (dashed) at their true polygons.
    for (const it of items) {
      ctx.strokeStyle = hexToRgba(it.auraColor, 0.85);
      ctx.lineWidth = 1.5;
      ctx.setLineDash([5, 3]);
      ctx.beginPath();
      it.poly.forEach((p, i) => {
        const px = p[0] * sx;
        const py = p[1] * sy;
        if (i === 0) ctx.moveTo(px, py);
        else ctx.lineTo(px, py);
      });
      ctx.closePath();
      ctx.stroke();
      ctx.setLineDash([]);
    }

    // --- Pass 4: leader lines from each moved badge back to its centroid.
    for (const it of items) {
      const moved = Math.hypot(it.x - it.ax, it.y - it.ay);
      if (moved <= badgeR0 * 0.5) continue;
      ctx.strokeStyle = "rgba(255,255,255,0.4)";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 2]);
      ctx.beginPath();
      ctx.moveTo(it.ax, it.ay);
      ctx.lineTo(it.x, it.y);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.beginPath();
      ctx.arc(it.ax, it.ay, 2, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(255,255,255,0.55)";
      ctx.fill();
    }

    // --- Pass 5: badges (disc + posture ring + panel blocks + count + label).
    for (const it of items) {
      const { area, rid, pulse, auraColor, count } = it;
      const cx = it.x;
      const cy = it.y;

      // Count badge — dark disc, delta-tinted ring, integer count centred.
      const badgeR = (base * 0.028 + 6) * (1 + 0.15 * pulse);
      ctx.beginPath();
      ctx.arc(cx, cy, badgeR, 0, Math.PI * 2);
      ctx.fillStyle = "rgba(38,40,45,0.92)";
      ctx.fill();
      ctx.strokeStyle = hexToRgba(auraColor, 0.95);
      ctx.lineWidth = 2;
      ctx.stroke();

      // Posture composition ring around the badge: three arcs sized by the
      // standing / resting / unknown split for this area (reused proxy).
      const pb = postures[rid];
      const pbTotal = pb ? pb.standing + pb.lying + pb.unknown : 0;
      let ringOuter = badgeR; // used to place the name label clear of the ring
      if (pb && count > 0 && pbTotal > 0) {
        const segs = [
          { v: pb.standing, c: ACTIVE_COLOR }, // amber — standing / active
          { v: pb.lying, c: REST_COLOR }, // sage — lying / resting
          { v: pb.unknown, c: CHART_MUTED }, // muted — unclassified
        ].filter((s) => s.v > 0);
        const ringW = Math.max(2.5, badgeR * 0.3);
        const ringR = badgeR + ringW * 0.5 + 3;
        ringOuter = ringR + ringW * 0.5;
        const gap = segs.length > 1 ? 0.1 : 0; // small angular gap between arcs
        const span = Math.PI * 2 - gap * segs.length;
        let a0 = -Math.PI / 2 + gap / 2; // start at 12 o'clock
        ctx.lineWidth = ringW;
        ctx.lineCap = "butt";
        segs.forEach((s) => {
          const sweep = (s.v / pbTotal) * span;
          ctx.beginPath();
          ctx.strokeStyle = hexToRgba(s.c, 0.95);
          ctx.arc(cx, cy, ringR, a0, a0 + sweep);
          ctx.stroke();
          a0 += sweep + gap;
        });
      }

      // Panel indicator — a vertical stack of unit blocks above the circle, one
      // teal rectangle per cow under a panel in this area (this camera's shelter).
      const shel = sheltering[rid] ?? 0;
      if (shel > 0) {
        const blockW = badgeR * 1.25;
        const blockH = Math.max(2.5, base * 0.011);
        const blockGap = Math.max(1.5, blockH * 0.55);
        const maxBlocks = 16;
        const nBlocks = Math.min(shel, maxBlocks);
        let by = cy - ringOuter - 6 - blockH; // top edge of the lowest block
        for (let m = 0; m < nBlocks; m++) {
          ctx.fillStyle = hexToRgba(SHELTER_COLOR, 0.92);
          ctx.fillRect(cx - blockW / 2, by, blockW, blockH);
          by -= blockH + blockGap;
        }
        if (shel > maxBlocks) {
          ctx.save();
          ctx.shadowColor = "rgba(0,0,0,0.6)";
          ctx.shadowBlur = 3;
          ctx.fillStyle = hexToRgba(SHELTER_COLOR, 0.98);
          ctx.textAlign = "center";
          ctx.textBaseline = "bottom";
          ctx.font = `600 ${Math.round(Math.max(9, base * 0.014))}px ui-sans-serif, system-ui, sans-serif`;
          ctx.fillText(String(shel), cx, by - 1);
          ctx.restore();
        }
      }

      ctx.fillStyle = "#ffffff";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.font = `600 ${Math.round(
        badgeR * 0.95,
      )}px ui-sans-serif, system-ui, sans-serif`;
      ctx.fillText(String(count), cx, cy);

      // Area name label under the badge (clearing the ring), soft shadow.
      ctx.save();
      ctx.shadowColor = "rgba(0,0,0,0.6)";
      ctx.shadowBlur = 3;
      ctx.fillStyle = "rgba(255,255,255,0.92)";
      ctx.font = `500 ${Math.round(
        Math.max(10, base * 0.018),
      )}px ui-sans-serif, system-ui, sans-serif`;
      ctx.fillText(area.name, cx, cy + ringOuter + 9);
      ctx.restore();
    }
  }

  // Animation loop: keeps redrawing while any badge is mid-pulse, then parks.
  function loop() {
    const now = performance.now();
    draw(now);
    const active = Object.values(pulseStartRef.current).some(
      (s) => now - s < PULSE_MS,
    );
    if (active) rafRef.current = requestAnimationFrame(loop);
    else rafRef.current = null;
  }

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const ro = new ResizeObserver(() => draw(performance.now()));
    ro.observe(container);
    draw(performance.now());
    if (rafRef.current == null) rafRef.current = requestAnimationFrame(loop);
    return () => {
      ro.disconnect();
      if (rafRef.current != null) {
        cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [areas, counts, postures, sheltering, cameras, hidden]);

  if (!areas) {
    return (
      <Card>
        <span className="font-mono text-[13px] text-gray-mid">Loading…</span>
      </Card>
    );
  }

  const nAreas = Object.entries(areas).reduce(
    (acc, [cam, list]) => acc + (hidden?.has(cam) ? 0 : list.length),
    0,
  );
  const shownTotal = Object.entries(areas).reduce((acc, [cam, list]) => {
    if (hidden?.has(cam)) return acc;
    return (
      acc + list.reduce((s, a) => s + (counts[`${cam}::${a.id}`] ?? 0), 0)
    );
  }, 0);

  return (
    <div className="animate-fade-slide-in">
      <div ref={containerRef} className="relative border border-border bg-surface">
        <img
          ref={imgRef}
          src={orthoImg()}
          className="w-full block"
          onLoad={() => draw(performance.now())}
        />
        <canvas
          ref={canvasRef}
          className="absolute inset-0 pointer-events-none"
        />
      </div>
      <div className="text-[11px] font-mono text-gray-tertiary mt-2">
        {nAreas === 0
          ? "No count areas yet — add one on a camera to start counting"
          : `${shownTotal} cow${shownTotal === 1 ? "" : "s"} across ${nAreas} area${
              nAreas === 1 ? "" : "s"
            } · ${frame == null ? "peak, whole day" : "this frame"}`}
      </div>
      {nAreas > 0 ? (
        <div className="flex items-center gap-3 mt-1.5 text-[11px] text-gray-tertiary">
          <span className="text-gray-mid">ring:</span>
          <LegendDot color={ACTIVE_COLOR} label="standing" />
          <LegendDot color={REST_COLOR} label="resting" />
          <LegendDot color={CHART_MUTED} label="unknown" />
        </div>
      ) : null}
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className="inline-block w-2 h-2 rounded-full"
        style={{ background: color }}
      />
      {label}
    </span>
  );
}

function hexToRgba(hex: string, alpha: number): string {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}
