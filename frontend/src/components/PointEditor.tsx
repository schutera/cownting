import { useCallback, useEffect, useRef, useState } from "react";
import type { PointDraft } from "../lib/pointSteps";
import type { CropLevel, LabelItem } from "../lib/types";

/* The body-points overlay on the crop (docs/roadmap/M6_body_points.md §3.3): the
 * labeler clicks, the page places the next point there.
 *
 * IT WORKS IN FULL-FRAME PIXELS, for MaskEditor's reason: the crop is a
 * VIEWPORT, its source square is the SVG viewBox, so a click converts straight
 * into the frame coordinates that are stored, and zooming through the server's
 * ladder changes what is visible and nothing else. A point placed at one zoom
 * is the same point at another, and nothing on this path converts between
 * spaces — the class of bug where a stored point shears away from the pixels it
 * was placed on cannot happen here.
 *
 * The component only reports clicks. Whether a click is allowed (all five
 * given, outside the route's bound) is the page's decision, because only the
 * page can shake and say why.
 */

// Dot radius in SCREEN px, counter-scaled into viewBox units as MaskEditor's
// nodes are, so a dot is the same size at every zoom level.
const DOT_R = 6;
const LABEL_PX = 12;
const ACCENT = "#F0B460";
const HALO = "rgba(12,14,16,0.85)";
// The scrim outside the box is lighter than the classify view's: the labeler is
// placing hooves, which sit at and below the box edge, and a dark scrim over
// the grass is a dark scrim over the thing being clicked.
const SCRIM_OPACITY = 0.25;

// The skeleton drawn between given points, as POINT_STEPS indices: the back
// line (nose - shoulders - tail base) and one leg from each end of it.
const BONES: readonly [number, number][] = [
  [0, 1],
  [1, 2],
  [1, 3],
  [2, 4],
];

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** The box to ring, full-frame px: the extent of the labeler's corrected
    outline once they made one, else the detector's box. Display only — the
    stored bbox is key material and never moves. The same rule FramePeek and
    InstanceCrop.ringFor apply, so all three views ring the same animal. */
function frameBoxFor(item: LabelItem): [number, number, number, number] {
  const poly = item.mask_seed === "edit" ? item.mask_frame : null;
  if (poly && poly.length >= 3) {
    const xs = poly.map((p) => p[0]);
    const ys = poly.map((p) => p[1]);
    return [Math.min(...xs), Math.min(...ys), Math.max(...xs), Math.max(...ys)];
  }
  return item.bbox;
}

export interface PointEditorProps {
  item: LabelItem;
  points: PointDraft;
  levels: CropLevel[];
  levelIndex: number;
  onLevelChange: (next: number) => void;
  /** A click on the photo, in FULL-FRAME px. */
  onPlace: (xy: [number, number]) => void;
  /** Hold-H: hide every mark, and take no clicks, so the bare pixels can be judged. */
  hidden?: boolean;
  /** The item's OWN crop could not be loaded; carries the key like
      InstanceCrop's does. Reported only for the default zoom level — see
      onLevelFailed. */
  onError?: (instanceKey: string) => void;
  /** Another zoom level could not be loaded. The editor has already gone back
      to the default level; the page only has to say so. */
  onLevelFailed?: () => void;
  className?: string;
}

export function PointEditor({
  item,
  points,
  levels,
  levelIndex,
  onLevelChange,
  onPlace,
  hidden = false,
  onError,
  onLevelFailed,
  className,
}: PointEditorProps) {
  const level = levels[clamp(levelIndex, 0, Math.max(0, levels.length - 1))];
  const svgRef = useRef<SVGSVGElement | null>(null);

  const [vx0, vy0, vx1, vy1] = level?.src ?? [0, 0, 1, 1];
  const vw = Math.max(vx1 - vx0, 1);
  const vh = Math.max(vy1 - vy0, 1);

  // SOURCE px per SCREEN px, measured — see MaskEditor. It keeps the dots and
  // their numbers the same size on screen at every zoom.
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
    const ro = new ResizeObserver(measure);
    ro.observe(svg);
    return () => ro.disconnect();
  }, [vw, vh]);

  const onPointerDown = useCallback(
    (e: React.PointerEvent<SVGSVGElement>) => {
      if (hidden || e.button !== 0) return;
      const rect = e.currentTarget.getBoundingClientRect();
      if (rect.width === 0 || rect.height === 0) return;
      onPlace([
        vx0 + ((e.clientX - rect.left) / rect.width) * vw,
        vy0 + ((e.clientY - rect.top) / rect.height) * vh,
      ]);
    },
    [hidden, onPlace, vh, vw, vx0, vy0],
  );

  // The wheel steps the zoom ladder, attached natively so it can preventDefault
  // (React's wheel listener is passive) — MaskEditor's rule, for its reason.
  const wheelRef = useRef<(e: WheelEvent) => void>(() => {});
  wheelRef.current = (e: WheelEvent) => {
    if (hidden || levels.length < 2) return;
    e.preventDefault();
    const dir = e.deltaY < 0 ? -1 : 1;
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

  const [bx0, by0, bx1, by1] = frameBoxFor(item);
  const scrim =
    `M${vx0} ${vy0}H${vx0 + vw}V${vy0 + vh}H${vx0}Z ` +
    `M${bx0} ${by0}H${bx1}V${by1}H${bx0}Z`;
  const r = { x: DOT_R * scale.x, y: DOT_R * scale.y };

  return (
    <div
      className={
        "relative overflow-hidden rounded-xl border border-border bg-surface-sunk" +
        (className ? " " + className : "")
      }
    >
      <img
        key={level?.url}
        src={level?.url}
        alt={`the cow to place body points on — ${item.camera_id}`}
        className="absolute inset-0 w-full h-full"
        decoding="async"
        draggable={false}
        onError={() => {
          // Only the item's OWN crop failing means "this cow cannot be shown".
          // Another rung can fail on its own — a tighter crop of a cow low in
          // the frame is refused for the timestamp band while the default one
          // renders — and treating that as a dead crop blocked the whole cow
          // behind a flag. So a failed rung just goes back to the default.
          const home = clamp(item.crop_level ?? 0, 0, Math.max(0, levels.length - 1));
          if (clamp(levelIndex, 0, Math.max(0, levels.length - 1)) === home) {
            onError?.(item.instance_key);
          } else {
            onLevelChange(home);
            onLevelFailed?.();
          }
        }}
      />
      <svg
        ref={svgRef}
        className="absolute inset-0 w-full h-full"
        viewBox={`${vx0} ${vy0} ${vw} ${vh}`}
        preserveAspectRatio="none"
        style={{ touchAction: "none", cursor: hidden ? "default" : "crosshair" }}
        onPointerDown={onPointerDown}
      >
        <g style={{ opacity: hidden ? 0 : 1 }}>
          <path d={scrim} fill="#000" fillOpacity={SCRIM_OPACITY} fillRule="evenodd" />
          <rect
            x={bx0}
            y={by0}
            width={Math.max(bx1 - bx0, 1)}
            height={Math.max(by1 - by0, 1)}
            fill="none"
            stroke="#fff"
            strokeOpacity={0.7}
            strokeWidth={1.25}
            vectorEffect="non-scaling-stroke"
          />
          {BONES.map(([a, b]) => {
            const pa = points[a];
            const pb = points[b];
            if (!pa || !pb) return null;
            return (
              <g key={`${a}-${b}`}>
                <line
                  x1={pa[0]}
                  y1={pa[1]}
                  x2={pb[0]}
                  y2={pb[1]}
                  stroke={HALO}
                  strokeWidth={3.5}
                  vectorEffect="non-scaling-stroke"
                />
                <line
                  x1={pa[0]}
                  y1={pa[1]}
                  x2={pb[0]}
                  y2={pb[1]}
                  stroke={ACCENT}
                  strokeWidth={1.5}
                  vectorEffect="non-scaling-stroke"
                />
              </g>
            );
          })}
          {points.map((p, i) =>
            p === null ? null : (
              <g key={i}>
                <ellipse
                  cx={p[0]}
                  cy={p[1]}
                  rx={r.x}
                  ry={r.y}
                  fill={ACCENT}
                  stroke={HALO}
                  strokeWidth={1.5}
                  vectorEffect="non-scaling-stroke"
                />
                <text
                  x={p[0] + r.x * 1.4}
                  y={p[1] - r.y * 1.1}
                  fontSize={LABEL_PX * scale.y}
                  fontFamily="ui-monospace, monospace"
                  fontWeight={700}
                  fill="#F2F0EC"
                  stroke={HALO}
                  strokeWidth={3}
                  paintOrder="stroke"
                  vectorEffect="non-scaling-stroke"
                >
                  {i + 1}
                </text>
              </g>
            ),
          )}
        </g>
      </svg>
    </div>
  );
}
