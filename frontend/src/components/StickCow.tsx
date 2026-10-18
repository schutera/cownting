import type { PointDraft } from "../lib/pointSteps";

/* The fixed side-view cow in the body-points panel (docs/roadmap/M6_body_points.md
 * §3.2). It is the only place the labeler learns WHERE a named point is — "top
 * of the shoulders" in words alone gets clicked on the neck, the hip and the
 * head — so the five dots sit on the anatomy they name, in the order they are
 * asked for, numbered as the dots on the photo are numbered.
 *
 * Drawn, not an image asset: the deployment's CSP forbids external assets, and
 * a figure in this file cannot drift from the order in lib/pointSteps.ts
 * without the diff showing it.
 */

const W = 168;
const H = 100;

// Where each point sits on the figure, in POINT_STEPS order. Facing left, like
// most cows on the screen do not — the direction is irrelevant to the labeler,
// and the numbers carry the order.
const ANCHORS: readonly [number, number][] = [
  [11, 57], // nose
  [74, 24], // top of the shoulders
  [147, 28], // tail base
  [74, 93], // front hoof
  [142, 93], // hind hoof
];

// The NECK is drawn as its own shape on purpose. Without one the head ran
// straight into the body, and the shoulder dot sat where the two met — it read
// as "the end of the head" as easily as "the top of the shoulders". With a neck
// between them, the shoulder dot is plainly on the back, above the front legs.
const BODY = "M66 28 Q106 20 146 26 Q156 31 154 47 Q152 62 142 64 L78 64 Q65 62 62 50 Z";
const NECK = "M70 28 L47 35 L46 52 L63 54 Z";
const HEAD = "M49 33 L31 37 Q17 43 9 54 Q9 61 17 61 L33 56 L49 52 Z";
const EAR = "M45 34 L41 26 L51 32 Z";
// Four legs as strokes: two front, two hind, the near pair a little lower.
const LEGS: readonly [number, number, number, number][] = [
  [74, 62, 74, 92],
  [84, 62, 84, 90],
  [134, 62, 134, 90],
  [142, 61, 142, 92],
];
const TAIL = "M152 30 Q161 44 159 70";
// Where each number sits relative to its dot. The front hoof's goes to the LEFT
// of its leg: on the right it lands on the second front leg and disappears.
const LABEL_DX: readonly number[] = [0, 7, 7, -7, 7];
const LABEL_DY: readonly number[] = [-8, -6, -6, -3, -3];

export interface StickCowProps {
  draft: PointDraft;
  /** The index being asked for, or null when all five are given. */
  next: number | null;
  accent: string;
  ink: string;
  dim: string;
}

export function StickCow({ draft, next, accent, ink, dim }: StickCowProps) {
  return (
    <svg
      width={W}
      height={H}
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="where each body point sits on a cow"
    >
      <line x1={4} y1={95} x2={W - 4} y2={95} stroke={dim} strokeOpacity={0.5} strokeWidth={1} />
      {LEGS.map(([x1, y1, x2, y2], i) => (
        <line
          key={i}
          x1={x1}
          y1={y1}
          x2={x2}
          y2={y2}
          stroke={dim}
          strokeWidth={4}
          strokeLinecap="round"
          strokeOpacity={0.7}
        />
      ))}
      <path d={TAIL} fill="none" stroke={dim} strokeWidth={2} strokeLinecap="round" strokeOpacity={0.7} />
      <path d={NECK} fill={dim} fillOpacity={0.18} stroke={dim} strokeWidth={1.25} />
      <path d={BODY} fill={dim} fillOpacity={0.18} stroke={dim} strokeWidth={1.25} />
      <path d={HEAD} fill={dim} fillOpacity={0.18} stroke={dim} strokeWidth={1.25} />
      <path d={EAR} fill={dim} fillOpacity={0.3} stroke={dim} strokeWidth={1} />

      {ANCHORS.map(([x, y], i) => {
        const given = i < draft.length;
        const hidden = given && draft[i] === null;
        const isNext = i === next;
        return (
          <g key={i}>
            {isNext ? (
              <circle cx={x} cy={y} r={8} fill="none" stroke={accent} strokeWidth={2} />
            ) : null}
            {hidden ? (
              // "Can't see": a cross, so a hidden point never reads as a placed one.
              <path
                d={`M${x - 4} ${y - 4}L${x + 4} ${y + 4}M${x + 4} ${y - 4}L${x - 4} ${y + 4}`}
                stroke={dim}
                strokeWidth={2}
                strokeLinecap="round"
              />
            ) : (
              <circle
                cx={x}
                cy={y}
                r={4.5}
                fill={given ? accent : isNext ? accent : "none"}
                fillOpacity={given || isNext ? 1 : 0}
                stroke={given || isNext ? "#14161A" : dim}
                strokeWidth={1.25}
              />
            )}
            <text
              x={x + LABEL_DX[i]}
              y={y + LABEL_DY[i]}
              fontSize={10}
              fontFamily="ui-monospace, monospace"
              fill={isNext ? accent : given ? ink : dim}
              textAnchor={LABEL_DX[i] < 0 ? "end" : LABEL_DX[i] === 0 ? "middle" : "start"}
            >
              {i + 1}
            </text>
          </g>
        );
      })}
    </svg>
  );
}
