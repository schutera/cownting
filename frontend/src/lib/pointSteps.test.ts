import { describe, expect, it } from "vitest";
import {
  POINT_COUNT,
  POINT_STEPS,
  canHide,
  hide,
  insideBounds,
  isComplete,
  nextIndex,
  place,
  placedCount,
  sameDraft,
  undo,
  type PointDraft,
} from "./pointSteps";

/* The body-points step's rules. The page only wires keys and clicks to these
   functions, so the behaviour of the step is what is pinned here. */

const P: [number, number] = [10, 20];

function full(): PointDraft {
  let d: PointDraft = [];
  for (let i = 0; i < POINT_COUNT; i += 1) d = place(d, [i, i]);
  return d;
}

describe("the vocabulary", () => {
  it("is the server's POINT_NAMES, in the same order", () => {
    // labels_db.POINT_NAMES. A different order here would store every point
    // under the wrong name without any error.
    expect(POINT_STEPS.map((s) => s.name)).toEqual([
      "nose",
      "shoulders",
      "tail_base",
      "front_hoof",
      "hind_hoof",
    ]);
  });
});

describe("placing", () => {
  it("asks for the points in order, then for nothing", () => {
    let d: PointDraft = [];
    for (let i = 0; i < POINT_COUNT; i += 1) {
      expect(nextIndex(d)).toBe(i);
      d = place(d, [i, i]);
    }
    expect(nextIndex(d)).toBeNull();
    expect(isComplete(d)).toBe(true);
  });

  it("refuses a sixth point, returning the same draft", () => {
    const d = full();
    expect(place(d, P)).toBe(d);
  });

  it("never mutates the draft it is given", () => {
    const d: PointDraft = [];
    place(d, P);
    expect(d).toEqual([]);
  });
});

describe("can't see", () => {
  it("stores null for the next point and moves on", () => {
    const d = hide(place([], P));
    expect(d).toEqual([P, null]);
    expect(nextIndex(d)).toBe(2);
    expect(placedCount(d)).toBe(1);
  });

  it("refuses the fifth can't-see when nothing was placed", () => {
    // Five nulls is not a set of body points; the route 400s it. S (too small)
    // and F (flag) are the honest answers for a cow with nothing visible.
    let d: PointDraft = [];
    for (let i = 0; i < POINT_COUNT - 1; i += 1) d = hide(d);
    expect(d).toEqual([null, null, null, null]);
    expect(canHide(d)).toBe(false);
    expect(hide(d)).toBe(d);
  });

  it("allows the fifth can't-see once any point was placed", () => {
    const d = hide(hide(hide(place([], P))));
    expect(canHide(d)).toBe(true);
    expect(isComplete(hide(d))).toBe(true);
  });

  it("refuses can't-see once all five are given", () => {
    const d = full();
    expect(hide(d)).toBe(d);
  });
});

describe("undo", () => {
  it("takes back the last point, placed or hidden", () => {
    expect(undo([P, null])).toEqual([P]);
    expect(undo([P])).toEqual([]);
  });

  it("re-opens the last point after the step was complete", () => {
    const d = undo(full());
    expect(isComplete(d)).toBe(false);
    expect(nextIndex(d)).toBe(POINT_COUNT - 1);
  });

  it("refuses on an empty draft, returning the same draft", () => {
    const d: PointDraft = [];
    expect(undo(d)).toBe(d);
  });
});

describe("sameDraft", () => {
  it("is true for equal drafts and false for any difference", () => {
    expect(sameDraft([P, null], [[10, 20], null])).toBe(true);
    expect(sameDraft([P, null], [P])).toBe(false);
    expect(sameDraft([P, null], [P, P])).toBe(false);
    expect(sameDraft([P], [[10, 21]])).toBe(false);
  });
});

describe("insideBounds", () => {
  const B: [number, number, number, number] = [100, 100, 200, 200];

  it("accepts a point inside the route's bound, edges included", () => {
    expect(insideBounds([150, 150], B)).toBe(true);
    expect(insideBounds([100, 200], B)).toBe(true);
  });

  it("refuses a point the route would refuse", () => {
    expect(insideBounds([99, 150], B)).toBe(false);
    expect(insideBounds([150, 201], B)).toBe(false);
  });

  it("refuses a point outside the photo when the frame size is known", () => {
    expect(insideBounds([-1, 50], undefined, 1920, 1080)).toBe(false);
    expect(insideBounds([50, 1081], undefined, 1920, 1080)).toBe(false);
    expect(insideBounds([50, 50], undefined, 1920, 1080)).toBe(true);
  });

  it("refuses a non-finite point", () => {
    expect(insideBounds([Number.NaN, 1], B)).toBe(false);
  });
});
