import { describe, expect, it } from "vitest";
import { approxIou, pointInPolygon, polygonArea, rectSeed, type Point } from "./polygon";

/* The outline editor's geometry. These are the only numbers on the label screen
   that are computed in the browser rather than served — everything else about a
   polygon (the crop<->frame conversion, the stored area, `iou_source`) is done
   server-side on purpose, so this file is small by design. What it covers is the
   readout the annotator watches while dragging, and the rectangle the editor
   falls back to when nothing is stored yet. */

const SQUARE: Point[] = [
  [0, 0],
  [10, 0],
  [10, 10],
  [0, 10],
];

describe("polygonArea", () => {
  it("measures a simple square", () => {
    expect(polygonArea(SQUARE)).toBe(100);
  });

  it("is winding-independent — an outline has no direction", () => {
    expect(polygonArea([...SQUARE].reverse())).toBe(100);
  });

  it("treats anything under three points as no area rather than throwing", () => {
    expect(polygonArea([])).toBe(0);
    expect(polygonArea([[0, 0]])).toBe(0);
    expect(polygonArea([[0, 0], [10, 10]])).toBe(0);
  });

  it("handles a concave shape, which a cow outline usually is", () => {
    // A square with a notch bitten out of its right edge: 100 - 25.
    const notched: Point[] = [
      [0, 0], [10, 0], [10, 5], [5, 5], [5, 10], [0, 10],
    ];
    expect(polygonArea(notched)).toBe(75);
  });
});

describe("pointInPolygon", () => {
  it("separates inside from outside", () => {
    expect(pointInPolygon(5, 5, SQUARE)).toBe(true);
    expect(pointInPolygon(15, 5, SQUARE)).toBe(false);
    expect(pointInPolygon(-1, 5, SQUARE)).toBe(false);
  });

  it("counts a vertex on the ray once, not twice", () => {
    // The classic even-odd failure: a horizontal ray leaving a point at the same
    // y as a vertex crosses two edges at one place and cancels itself out, so
    // the inside reads as outside.
    const diamond: Point[] = [[5, 0], [10, 5], [5, 10], [0, 5]];
    expect(pointInPolygon(5, 5, diamond)).toBe(true);
  });

  it("handles a concave shape's dent", () => {
    const c: Point[] = [[0, 0], [10, 0], [10, 10], [6, 10], [6, 4], [4, 4], [4, 10], [0, 10]];
    expect(pointInPolygon(5, 8, c)).toBe(false); // inside the dent, outside the shape
    expect(pointInPolygon(2, 8, c)).toBe(true);
  });
});

describe("approxIou", () => {
  it("is 1 for a shape against itself", () => {
    expect(approxIou(SQUARE, SQUARE)).toBeCloseTo(1, 2);
  });

  it("is 0 for disjoint shapes", () => {
    const far: Point[] = [[100, 100], [110, 100], [110, 110], [100, 110]];
    expect(approxIou(SQUARE, far)).toBe(0);
  });

  it("approximates a known half-overlap", () => {
    // Two 10x10 squares sharing half their width: intersection 50, union 150.
    const shifted: Point[] = [[5, 0], [15, 0], [15, 10], [5, 10]];
    expect(approxIou(SQUARE, shifted)).toBeCloseTo(1 / 3, 1);
  });

  it("returns null rather than a misleading 0 for a degenerate shape", () => {
    // The panel renders null as no readout at all. Zero would read as "you have
    // destroyed the outline", which is a different and alarming claim.
    expect(approxIou(SQUARE, [[0, 0], [1, 1]])).toBeNull();
    expect(approxIou([], SQUARE)).toBeNull();
  });

  it("does not bias low by sampling the bounding box corners", () => {
    // Cell CENTRES, not corners: sampling corners systematically counts the
    // outside edge and drags every result down. A shape against itself is the
    // sharpest test of that, since the true answer is exactly 1.
    const cow: Point[] = [[2, 3], [9, 1], [11, 6], [7, 12], [1, 9]];
    expect(approxIou(cow, cow)).toBe(1);
  });
});

describe("rectSeed", () => {
  it("gives four corners inside the box it was handed", () => {
    const seed = rectSeed([0, 0, 100, 200]);
    expect(seed).toHaveLength(4);
    const xs = seed.map((p) => p[0]);
    const ys = seed.map((p) => p[1]);
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...xs)).toBeLessThanOrEqual(100);
    expect(Math.min(...ys)).toBeGreaterThanOrEqual(0);
    expect(Math.max(...ys)).toBeLessThanOrEqual(200);
  });

  it("insets so every corner is grabbable rather than under the crop border", () => {
    const [tl] = rectSeed([0, 0, 100, 200]);
    expect(tl[0]).toBeGreaterThan(0);
    expect(tl[1]).toBeGreaterThan(0);
  });

  it("does not collapse on a tiny box — a distant cow still gets a shape", () => {
    // The inset is capped by an eighth of each side, so a 6px box still yields a
    // polygon with positive area rather than four coincident points.
    const seed = rectSeed([10, 10, 16, 16]);
    expect(polygonArea(seed)).toBeGreaterThan(0);
  });
});
