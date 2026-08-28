/* Polygon helpers for the outline editor (M4a §5).
 *
 * These are CLIENT-SIDE READOUT ONLY. The server recomputes area and IoU in
 * full-frame space when it stores the edit — the plan is explicit that
 * `iou_source` is a QC statistic the client must not be able to flatter. What
 * is computed here exists so the annotator can see a "correction" turning into
 * a redraw while it is happening.
 */

export type Point = [number, number];

/** Shoelace area, always positive — winding direction is not meaningful here. */
export function polygonArea(poly: readonly Point[]): number {
  if (poly.length < 3) return 0;
  let acc = 0;
  for (let i = 0; i < poly.length; i += 1) {
    const [x1, y1] = poly[i];
    const [x2, y2] = poly[(i + 1) % poly.length];
    acc += x1 * y2 - x2 * y1;
  }
  return Math.abs(acc) / 2;
}

/** Even-odd ray cast. Used by the IoU sampler below. */
export function pointInPolygon(x: number, y: number, poly: readonly Point[]): boolean {
  let inside = false;
  for (let i = 0, j = poly.length - 1; i < poly.length; j = i, i += 1) {
    const [xi, yi] = poly[i];
    const [xj, yj] = poly[j];
    // The `!==` on the y-comparisons is what makes a vertex exactly on the ray
    // count once rather than twice.
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) {
      inside = !inside;
    }
  }
  return inside;
}

/**
 * Approximate IoU by sampling a grid over the union's bounding box.
 *
 * Exact polygon clipping (Sutherland-Hodgman on convex pieces, or a full
 * Weiler-Atherton) is a few hundred lines and a pile of degenerate cases, for a
 * number that is redrawn on every pointermove and rendered rounded to a whole
 * percent. A 64x64 sample is ~4k point-in-polygon tests — imperceptible at drag
 * rate — and is accurate to well under the one percent that is displayed.
 *
 * Returns null when either polygon is degenerate, which the panel renders as no
 * readout at all rather than as "0%".
 */
export function approxIou(a: readonly Point[], b: readonly Point[], samples = 64): number | null {
  if (a.length < 3 || b.length < 3) return null;
  const xs = [...a, ...b].map((p) => p[0]);
  const ys = [...a, ...b].map((p) => p[1]);
  const x0 = Math.min(...xs);
  const x1 = Math.max(...xs);
  const y0 = Math.min(...ys);
  const y1 = Math.max(...ys);
  if (x1 - x0 <= 0 || y1 - y0 <= 0) return null;

  let inter = 0;
  let union = 0;
  for (let i = 0; i < samples; i += 1) {
    // Cell CENTRES, not corners: sampling the corners of the bounding box
    // systematically counts the outside edge and biases every result low.
    const x = x0 + ((i + 0.5) / samples) * (x1 - x0);
    for (let j = 0; j < samples; j += 1) {
      const y = y0 + ((j + 0.5) / samples) * (y1 - y0);
      const inA = pointInPolygon(x, y, a);
      const inB = pointInPolygon(x, y, b);
      if (inA && inB) inter += 1;
      if (inA || inB) union += 1;
    }
  }
  return union === 0 ? null : inter / union;
}

/** The bbox-seeded starting outline: the ring, inset slightly so every node is
    visible and grabbable rather than sitting under the crop's rounded border. */
export function rectSeed(ring: [number, number, number, number]): Point[] {
  const [x0, y0, x1, y1] = ring;
  const inset = Math.min(4, Math.abs(x1 - x0) / 8, Math.abs(y1 - y0) / 8);
  const ax = x0 + inset;
  const ay = y0 + inset;
  const bx = x1 - inset;
  const by = y1 - inset;
  return [
    [ax, ay],
    [bx, ay],
    [bx, by],
    [ax, by],
  ];
}
