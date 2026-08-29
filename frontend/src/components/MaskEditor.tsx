import { useCallback, useEffect, useRef, useState } from "react";
import type { CropLevel, LabelItem } from "../lib/types";

/* The instance-outline editor (docs/roadmap/M4a_instance_mask_fixup.md §5.2):
 * the model's mask drawn over the crop as a polygon whose nodes the annotator
 * drags onto the animal's real edge.
 *
 * IT WORKS IN FULL-FRAME PIXELS, and that is the design decision everything else
 * follows from. The crop on screen is a VIEWPORT, not a coordinate system: its
 * source square is the SVG viewBox, so zooming to a wider crop changes which
 * pixels are visible and leaves every stored coordinate untouched. A point
 * dragged at one zoom is the same point at another, the polygon needs no
 * conversion when the level changes, and the submit needs none either — which
 * removes the whole class of bug where an outline drawn in one crop basis is
 * stored against a different one and lands, sheared, somewhere near the animal.
 *
 * The zoom LADDER is computed server-side (labeling.zoom_levels) and rides on
 * the item. That is what keeps crop_geometry out of TypeScript: the client can
 * only occupy viewports the server has already described, so it can never
 * invent a basis the server cannot invert.
 *
 * WHY HIT-TESTING IS IN SCREEN PX, NOT SOURCE PX. A distant cow in a tight crop
 * and the same cow in a 2.5-pad crop differ by an order of magnitude in scale.
 * A grab radius fixed in source px would be unusably large in one and
 * untouchable in the other. So the pointer is converted into source space and
 * the RADII are converted the other way — the node markers are counter-scaled
 * for the same reason, which is the vertex idiom ImageClicker proved on the
 * count-area editor.
 *
 * WHAT THIS DELIBERATELY DOES NOT DO: pan, multi-polygon, freehand. Zoom is
 * bounded by the ladder and always centred on the animal, so there is nothing to
 * pan to; one instance has one outline; and a freehand trace produces the
 * thousand-vertex blob the server's simplification exists to reject.
 */

// Node marker radius and the grab radius around it, in SCREEN px. The grab
// radius is deliberately larger than the dot: a node you can see but not catch
// reads as a broken drag.
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
  /** The polygon being edited, FULL-FRAME px. Owned by the page so Revert,
      dirty-tracking and the save payload all read one array. */
  polygon: [number, number][];
  onChange: (next: [number, number][]) => void;
  /** The zoom ladder and where we are on it. Lifted to the page so the level
      survives leaving and re-entering the editor on the same animal. */
  levels: CropLevel[];
  levelIndex: number;
  onLevelChange: (next: number) => void;
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

/** Distance from p to segment ab. */
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
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

export function MaskEditor({
  item,
  polygon,
  onChange,
  levels,
  levelIndex,
  onLevelChange,
  hidden = false,
  onRefusedDelete,
  className,
}: MaskEditorProps) {
  const level = levels[clamp(levelIndex, 0, Math.max(0, levels.length - 1))];
  const svgRef = useRef<SVGSVGElement | null>(null);
  const [dragIndex, setDragIndex] = useState<number | null>(null);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  // The viewport, in full-frame px: the square the server cut this crop from.
  const [vx0, vy0, vx1, vy1] = level?.src ?? [0, 0, 1, 1];
  const vw = Math.max(vx1 - vx0, 1);
  const vh = Math.max(vy1 - vy0, 1);

  /* SOURCE px per SCREEN px, measured. Markers are drawn in viewBox units, so
     this is what keeps a node the same size on screen at every zoom level —
     `vectorEffect` fixes strokes but has no say over a radius. Tracked per axis
     because `preserveAspectRatio="none"` lets them differ, which is why the
     markers are ellipses. */
  const [scale, setScale] = useState<{ x: number; y: number }>({ x: 1, y: 1 });
  useEffect(() => {
    const svg = svgRef.current;
    if (svg === null) return;
    const measure = () => {
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      setScale({ x: vw / rect.width, y: vh / rect.height });
    };
    measure();
    // The crop box is sized by viewport arithmetic (Label's CROP_CHROME) and the
    // viewBox changes on every zoom, so both need re-measuring.
    const ro = new ResizeObserver(measure);
    ro.observe(svg);
    return () => ro.disconnect();
  }, [vw, vh]);

  /** Client px -> full-frame px. */
  const toFrame = useCallback(
    (e: { clientX: number; clientY: number }): [number, number] | null => {
      const svg = svgRef.current;
      if (svg === null) return null;
      const rect = svg.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return null;
      return [
        vx0 + ((e.clientX - rect.left) / rect.width) * vw,
        vy0 + ((e.clientY - rect.top) / rect.height) * vh,
      ];
    },
    [vh, vw, vx0, vy0],
  );

  /** The node under the pointer, or null. `radiusPx` is screen px converted into
      source space via the mean scale — the two axes differ only when a caller
      squeezes the box, and picking one keeps the grab zone a circle on screen. */
  const nodeAt = useCallback(
    (cx: number, cy: number, radiusPx: number): number | null => {
      const r = radiusPx * ((scale.x + scale.y) / 2);
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
    [polygon, scale],
  );

  const onPointerDown = useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      if (hidden) return;
      const pt = toFrame(e);
      if (pt === null) return;
      const [cx, cy] = pt;

      const hit = nodeAt(cx, cy, GRAB_R);
      if (hit !== null) {
        // Pointer capture on the SVG, not the node: a fast drag leaves a 10px
        // circle behind within one frame, and without capture the pointerup
        // lands on the page and the node sticks to the cursor.
        e.currentTarget.setPointerCapture(e.pointerId);
        setDragIndex(hit);
        return;
      }

      // Not on a node: near an edge? Insert there. The polygon is closed, so the
      // last segment wraps to node 0 — an outline is most often wrong at exactly
      // one end, and without the wrap that seam is the one place a node cannot
      // be added.
      const edgeR = EDGE_R * ((scale.x + scale.y) / 2);
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
    [hidden, nodeAt, onChange, polygon, scale, toFrame],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      const pt = toFrame(e);
      if (pt === null) return;
      const [cx, cy] = pt;
      if (dragIndex === null) {
        setHoverIndex(hidden ? null : nodeAt(cx, cy, GRAB_R));
        return;
      }
      const next: [number, number][] = [...polygon];
      // Clamped to the FRAME, not to the current viewport: a node dragged to the
      // edge of a tight crop is still a legitimate point of the animal, and
      // zooming out must be able to reach it again.
      const fw = item.frame_w ?? 0;
      const fh = item.frame_h ?? 0;
      next[dragIndex] = [
        fw > 0 ? clamp(cx, 0, fw) : cx,
        fh > 0 ? clamp(cy, 0, fh) : cy,
      ];
      onChange(next);
    },
    [dragIndex, hidden, item.frame_h, item.frame_w, nodeAt, onChange, polygon, toFrame],
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
      const pt = toFrame(e);
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
    [hidden, nodeAt, onChange, onRefusedDelete, polygon, toFrame],
  );

  /* THE WHEEL IS THE ZOOM, because that is what a wheel means over an image
     everywhere else. It steps the server's ladder — up for a tighter crop, down
     for more surroundings — so an outline that needs to reach past the animal
     has somewhere to reach from.

     preventDefault matters: without it the page scrolls under the annotator
     while they are trying to zoom, which is the single most common way a
     wheel-zoom feels broken. React's synthetic wheel listener is passive, so the
     handler is attached natively with { passive: false }. */
  const wheelRef = useRef<(e: WheelEvent) => void>(() => {});
  wheelRef.current = (e: WheelEvent) => {
    if (hidden || levels.length < 2) return;
    e.preventDefault();
    const dir = e.deltaY < 0 ? -1 : 1; // wheel up = zoom in = a tighter pad
    const next = clamp(levelIndex + dir, 0, levels.length - 1);
    if (next !== levelIndex) onLevelChange(next);
  };
  useEffect(() => {
    const svg = svgRef.current;
    if (svg === null) return;
    const handler = (e: WheelEvent) => wheelRef.current(e);
    svg.addEventListener("wheel", handler, { passive: false });
    return () => svg.removeEventListener("wheel", handler);
  }, []);

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
    >
      <img
        // Keyed by URL so a zoom mounts a fresh element rather than holding the
        // previous crop on screen until the next decode finishes — a stale crop
        // under a live viewBox is an outline drawn over the wrong pixels.
        key={level?.url}
        src={level?.url}
        alt={`the cow whose outline you are correcting — ${item.camera_id}`}
        className="absolute inset-0 w-full h-full"
        decoding="async"
        draggable={false}
      />
      <svg
        ref={svgRef}
        className="absolute inset-0 w-full h-full"
        viewBox={`${vx0} ${vy0} ${vw} ${vh}`}
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
        {/* Two strokes, dark under light, for the reason InstanceCrop's ring has
            two: one colour vanishes against either a sunlit flank or panel
            shade, and an outline you cannot see is an outline you cannot fix. */}
        <path d={d} fill={FILL} stroke={STROKE_HALO} strokeWidth={4} vectorEffect="non-scaling-stroke" />
        <path d={d} fill="none" stroke={STROKE} strokeWidth={2} vectorEffect="non-scaling-stroke" />
        {polygon.map(([x, y], i) => (
          <ellipse
            key={i}
            cx={x}
            cy={y}
            // NODE_R screen px expressed in viewBox units, per axis.
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
