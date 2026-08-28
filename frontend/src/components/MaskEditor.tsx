import { useCallback, useEffect, useRef, useState } from "react";
import type { LabelItem } from "../lib/types";

/* The instance-outline editor (docs/roadmap/M4a_instance_mask_fixup.md §5.2):
 * the model's mask drawn over the crop as a polygon whose nodes the annotator
 * drags onto the animal's real edge.
 *
 * IT IS THE SAME STRETCH CONTRACT AS InstanceCrop, deliberately. The image and
 * the SVG are both stretched into one absolutely-positioned box — the image by
 * `object-fit: fill`, the SVG by `preserveAspectRatio="none"` over a
 * `crop_w x crop_h` viewBox — so source px map to box px identically and the
 * polygon tracks the animal at any rendered size, even if a caller squeezes the
 * box out of the crop's aspect ratio. A `meet`-fitted SVG over a filled image is
 * where that silently shears, which on THIS component would mean a saved
 * polygon that does not match what the annotator drew.
 *
 * WHY HIT-TESTING IS IN SCREEN PX, NOT CROP PX. A 40px-wide crop of a distant
 * cow blown up to a 420px box has a scale factor of ~10: a grab radius of 8
 * crop px would be 80px on screen (every node grabs everything) and on a 3000px
 * crop it would be under a pixel (nothing is grabbable at all). So the pointer
 * position is converted to crop space, and the RADII are converted the other
 * way — the node markers are counter-scaled for exactly the same reason, which
 * is the vertex idiom ImageClicker already proved on the count-area editor.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO: zoom, pan, multi-polygon, freehand. The
 * crop IS the zoom (hold-Space still shows the whole frame), one instance has
 * one outline, and a freehand trace produces the 4000-vertex blob the plan's
 * server-side simplification exists to reject. M4's full-frame MaskCanvas is
 * where those belong.
 */

// Node marker radius and the grab radius around it, in SCREEN px (see header).
// The grab radius is deliberately larger than the dot: a node you can see but
// not catch reads as a broken drag.
const NODE_R = 5;
const GRAB_R = 11;
// How near a segment a click has to land to insert a node there, screen px.
// Smaller than GRAB_R so a click near a node moves that node rather than
// spawning a second one on top of it.
const EDGE_R = 8;
// Below this many nodes a polygon is not a shape. The server enforces the same
// floor; refusing here means the annotator finds out at the gesture, not at the
// save.
const MIN_NODES = 3;

const STROKE = "#F0B460";
const STROKE_HALO = "rgba(12,14,16,0.85)";
const FILL = "rgba(240,180,96,0.16)";
const NODE_FILL = "#F2F0EC";

export interface MaskEditorProps {
  item: LabelItem;
  /** The polygon being edited, crop-local px. Owned by the page so Revert,
      dirty-tracking and the save payload all read one array. */
  polygon: [number, number][];
  onChange: (next: [number, number][]) => void;
  /** Hold-H: hide the overlay so the bare pixels can be judged mid-edit — the
      same affordance the ring has, and the only honest way to check whether the
      outline is actually on the animal. */
  hidden?: boolean;
  /** A node count that cannot be reduced further, so the caller can shake+hint
      rather than let a double-click silently do nothing. */
  onRefusedDelete?: () => void;
  className?: string;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** Distance from p to segment ab, and where along ab the foot lands. */
function distToSegment(
  px: number,
  py: number,
  ax: number,
  ay: number,
  bx: number,
  by: number,
): number {
  const dx = bx - ax;
  const dy = by - ay;
  const len2 = dx * dx + dy * dy;
  // A degenerate segment (two coincident nodes) would divide by zero; the
  // distance to it is just the distance to the point.
  const t = len2 === 0 ? 0 : clamp(((px - ax) * dx + (py - ay) * dy) / len2, 0, 1);
  const fx = ax + t * dx;
  const fy = ay + t * dy;
  return Math.hypot(px - fx, py - fy);
}

export function MaskEditor({
  item,
  polygon,
  onChange,
  hidden = false,
  onRefusedDelete,
  className,
}: MaskEditorProps) {
  const w = Math.max(item.crop_w, 1);
  const h = Math.max(item.crop_h, 1);
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  /* CROP px per SCREEN px, measured. The markers are drawn in viewBox units, so
     this is what keeps a node the same size on screen whether the crop is 40px
     of a distant animal blown up to 420, or a 3000px close-up scaled down —
     `vectorEffect` fixes strokes but has no say over a radius. The two axes are
     tracked separately because `preserveAspectRatio="none"` lets them differ,
     which is why the markers are ellipses: one radius would render an egg the
     moment a caller squeezes the box. */
  const [scale, setScale] = useState<{ x: number; y: number }>({ x: 1, y: 1 });
  useEffect(() => {
    const svg = svgRef.current;
    if (svg === null) return;
    const measure = () => {
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      setScale({ x: w / rect.width, y: h / rect.height });
    };
    measure();
    // The crop box is sized by viewport arithmetic (Label's CROP_CHROME), so it
    // changes without this component re-rendering. ResizeObserver catches that;
    // a window listener alone would miss a panel opening beside it.
    const ro = new ResizeObserver(measure);
    ro.observe(svg);
    return () => ro.disconnect();
  }, [h, w]);

  /** Client px -> crop px, plus the scale needed to convert the screen-space
      radii into crop space. One measurement per gesture, read from the live
      element so a resized window cannot leave a stale factor behind. */
  const geometry = useCallback((): { sx: number; sy: number; rect: DOMRect } | null => {
    const svg = svgRef.current;
    if (svg === null) return null;
    const rect = svg.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) return null;
    return { sx: w / rect.width, sy: h / rect.height, rect };
  }, [h, w]);

  const toCrop = useCallback(
    (e: { clientX: number; clientY: number }): [number, number] | null => {
      const g = geometry();
      if (g === null) return null;
      return [
        clamp((e.clientX - g.rect.left) * g.sx, 0, w),
        clamp((e.clientY - g.rect.top) * g.sy, 0, h),
      ];
    },
    [geometry, h, w],
  );

  /** The node under the pointer, or null. Radii are screen px converted into
      crop space via the mean scale — the two axes differ only when a caller
      squeezes the box, and picking one keeps the grab zone a circle on screen. */
  const nodeAt = useCallback(
    (cx: number, cy: number, radiusPx: number): number | null => {
      const g = geometry();
      if (g === null) return null;
      const r = radiusPx * ((g.sx + g.sy) / 2);
      let best: number | null = null;
      let bestD = Infinity;
      for (let i = 0; i < polygon.length; i += 1) {
        const d = Math.hypot(polygon[i][0] - cx, polygon[i][1] - cy);
        if (d <= r && d < bestD) {
          best = i;
          bestD = d;
        }
      }
      return best;
    },
    [geometry, polygon],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      if (hidden) return;
      const pt = toCrop(e);
      if (pt === null) return;
      const [cx, cy] = pt;

      const hit = nodeAt(cx, cy, GRAB_R);
      if (hit !== null) {
        // Pointer capture on the SVG, not the circle: a fast drag leaves the
        // 10px node behind within one frame, and without capture the pointerup
        // lands on the page and the node sticks to the cursor.
        e.currentTarget.setPointerCapture(e.pointerId);
        setDragIndex(hit);
        return;
      }

      // Not on a node: is it near an edge? Insert there. The polygon is closed,
      // so the last segment wraps to node 0 — an outline is most often wrong at
      // exactly one end, and without the wrap that seam is the one place a node
      // cannot be added.
      const g = geometry();
      if (g === null) return;
      const edgeR = EDGE_R * ((g.sx + g.sy) / 2);
      let bestAt = -1;
      let bestD = Infinity;
      for (let i = 0; i < polygon.length; i += 1) {
        const a = polygon[i];
        const b = polygon[(i + 1) % polygon.length];
        const d = distToSegment(cx, cy, a[0], a[1], b[0], b[1]);
        if (d <= edgeR && d < bestD) {
          bestAt = i;
          bestD = d;
        }
      }
      if (bestAt >= 0) {
        const next: [number, number][] = [...polygon];
        next.splice(bestAt + 1, 0, [cx, cy]);
        onChange(next);
        e.currentTarget.setPointerCapture(e.pointerId);
        setDragIndex(bestAt + 1); // drag the new node straight away
      }
    },
    [geometry, hidden, nodeAt, onChange, polygon, toCrop],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      const pt = toCrop(e);
      if (pt === null) return;
      const [cx, cy] = pt;
      if (dragIndex === null) {
        setHoverIndex(hidden ? null : nodeAt(cx, cy, GRAB_R));
        return;
      }
      const next: [number, number][] = [...polygon];
      // Clamped to the crop box: a vertex dragged off the image is out of the
      // frame the server will validate against, and the annotator cannot see
      // where it went.
      next[dragIndex] = [cx, cy];
      onChange(next);
    },
    [dragIndex, hidden, nodeAt, onChange, polygon, toCrop],
  );

  const endDrag = useCallback((e: React.PointerEvent<SVGSVGElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) {
      e.currentTarget.releasePointerCapture(e.pointerId);
    }
    setDragIndex(null);
  }, []);

  const onDoubleClick = useCallback(
    (e: React.MouseEvent<SVGSVGElement>) => {
      if (hidden) return;
      const pt = toCrop(e);
      if (pt === null) return;
      const hit = nodeAt(pt[0], pt[1], GRAB_R);
      if (hit === null) return;
      // A dead gesture must be SEEN, never a silent no-op — the page's standing
      // rule for keys applies to the mouse just as much.
      if (polygon.length <= MIN_NODES) {
        onRefusedDelete?.();
        return;
      }
      onChange(polygon.filter((_, i) => i !== hit));
    },
    [hidden, nodeAt, onChange, onRefusedDelete, polygon, toCrop],
  );

  const d =
    polygon.length === 0
      ? ""
      : `M${polygon.map(([x, y]) => `${x.toFixed(2)} ${y.toFixed(2)}`).join("L")}Z`;

  return (
    <div
      className={
        "relative overflow-hidden rounded-xl border border-border bg-surface-sunk" +
        (className ? " " + className : "")
      }
      style={{ aspectRatio: `${w} / ${h}` }}
    >
      <img
        key={item.crop_url}
        src={item.crop_url}
        alt={`the cow whose outline you are correcting — ${item.camera_id}`}
        className="absolute inset-0 w-full h-full"
        decoding="async"
        draggable={false}
      />
      <svg
        ref={svgRef}
        className="absolute inset-0 w-full h-full"
        viewBox={`0 0 ${w} ${h}`}
        preserveAspectRatio="none"
        style={{
          opacity: hidden ? 0 : 1,
          touchAction: "none", // or a drag scrolls the page on a touch screen
          cursor: dragIndex !== null || hoverIndex !== null ? "grabbing" : "crosshair",
        }}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        onDoubleClick={onDoubleClick}
      >
        {/* Two strokes, dark under light, for the reason InstanceCrop's ring
            has two: one colour vanishes against either a sunlit flank or panel
            shade, and an outline you cannot see is an outline you cannot fix. */}
        <path d={d} fill={FILL} stroke={STROKE_HALO} strokeWidth={4} vectorEffect="non-scaling-stroke" />
        <path d={d} fill="none" stroke={STROKE} strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {polygon.map(([x, y], i) => (
          <ellipse
            key={i}
            cx={x}
            cy={y}
            // Counter-scaled: NODE_R screen px expressed in viewBox units, per
            // axis (see the `scale` comment above).
            rx={NODE_R * scale.x}
            ry={NODE_R * scale.y}
            fill={i === dragIndex || i === hoverIndex ? STROKE : NODE_FILL}
            stroke={STROKE_HALO}
            strokeWidth={1.5}
            vectorEffect="non-scaling-stroke"
          />
        ))}
      </svg>
    </div>
  );
}
