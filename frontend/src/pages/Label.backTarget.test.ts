import { describe, expect, it } from "vitest";
import { backTarget, forwardTarget, type FlowState } from "./Label";

/* THE ARROWS WALK THE ITEM. An item is a sequence of steps — [1/4] outline,
   [2/4] body points, then one per question — and left/right move along it.

   Two bugs got us here, both from the pair being asymmetric. First ArrowLeft
   moved the TAPE rather than the step, so a mis-click on question 1 was
   uncorrectable: on the first item of a session it shook and refused, and on any
   later item it abandoned the animal. Then, with back fixed, forward still was
   not its mirror — it jumped out of review or committed the whole item, so a
   question could be reached from one side and not the other.

   The ORDER of the cases in each direction is the behaviour, which is why it is
   pinned here rather than left inside the component. */

/** Mid-item default: outline and points steps passed, standing on question 1
    of 2, at the head of the tape, nothing answered yet. */
const S = (over: Partial<FlowState> = {}): FlowState => ({
  step: 0,
  stepCount: 2,
  cursor: 0,
  frontier: 0,
  inGeometry: false,
  geomDone: true,
  hasPoints: true,
  inPoints: false,
  pointsDone: true,
  pointsReady: true,
  pointsOnly: false,
  outlineOpen: false,
  complete: false,
  ...over,
});

/** Standing on the points step, outline passed. */
const ON_POINTS: Partial<FlowState> = { inPoints: true };

const back = (over: Partial<FlowState> = {}) => backTarget(S(over));
const fwd = (over: Partial<FlowState> = {}) => forwardTarget(S(over));

describe("backTarget", () => {
  it("steps back a QUESTION when there is an earlier one", () => {
    expect(back({ step: 1 })).toBe("question");
  });

  it("corrects question 1 even on the very first item of a session", () => {
    // The originally reported bug: cursor 0, so there is no earlier item — but
    // there is an earlier step. This used to shake and refuse.
    expect(back({ step: 1, cursor: 0 })).toBe("question");
  });

  it("prefers the question over the item — correcting beats abandoning", () => {
    expect(back({ step: 1, cursor: 3 })).toBe("question");
  });

  it("returns to the POINTS step from the first question", () => {
    // Step 2 of the item, not a different animal.
    expect(back({ step: 0 })).toBe("points");
  });

  it("returns to the OUTLINE step from the points step", () => {
    expect(back({ ...ON_POINTS })).toBe("geometry");
    // ...whether or not the points were given yet: looking back at the outline
    // mid-draft is allowed, and the draft survives it.
    expect(back({ ...ON_POINTS, pointsDone: false, pointsReady: false })).toBe("geometry");
  });

  it("returns to the OUTLINE step from the first question when there is no points step", () => {
    // A server that predates M6: the item is the two-step one it always was.
    expect(back({ step: 0, hasPoints: false })).toBe("geometry");
  });

  it("does not offer the outline step before it has been passed", () => {
    // We are already standing on it; there is nothing behind it but the tape.
    expect(back({ step: 0, inGeometry: true, geomDone: false, cursor: 2 })).toBe("item");
    expect(back({ step: 0, inGeometry: true, geomDone: false, cursor: 0 })).toBe("none");
  });

  it("moves to the previous item from the outline step", () => {
    expect(back({ inGeometry: true, geomDone: true, cursor: 3 })).toBe("item");
  });

  it("refuses only at the true start of the tape", () => {
    expect(back({ inGeometry: true, geomDone: false, cursor: 0 })).toBe("none");
  });

  it("never reorders questions under an open outline editor", () => {
    expect(back({ step: 1, cursor: 2, outlineOpen: true })).toBe("item");
    expect(back({ step: 1, cursor: 0, outlineOpen: true })).toBe("none");
  });
});

describe("forwardTarget", () => {
  it("steps forward a QUESTION when there is a later one", () => {
    expect(fwd({ step: 0, stepCount: 2 })).toBe("question");
  });

  it("leaves the outline step for the POINTS once it has been judged", () => {
    expect(fwd({ inGeometry: true, geomDone: true })).toBe("points");
  });

  it("leaves the outline step for the questions when there is no points step", () => {
    expect(fwd({ inGeometry: true, geomDone: true, hasPoints: false })).toBe("question");
  });

  it("REFUSES to leave the outline step before it is judged", () => {
    // The gate is the point of the step; an arrow must not be the hole in it.
    expect(fwd({ inGeometry: true, geomDone: false })).toBe("blocked");
  });

  it("REFUSES to leave the points step before all five are given", () => {
    expect(fwd({ ...ON_POINTS, pointsDone: false, pointsReady: false })).toBe("blocked");
  });

  it("REFUSES to leave a revisited points step mid-edit", () => {
    // Stored, then a point taken back: the draft is not a saved state, so the
    // arrow does not pretend it is.
    expect(fwd({ ...ON_POINTS, pointsDone: true, pointsReady: false })).toBe("blocked");
  });

  it("never leaves the points step on a draft that was not written", () => {
    expect(fwd({ ...ON_POINTS, pointsDone: false, pointsReady: true })).toBe("blocked");
  });

  it("leaves a revisited points step unchanged for the questions", () => {
    expect(fwd({ ...ON_POINTS })).toBe("question");
  });

  it("commits on the last question once every answer is in", () => {
    expect(fwd({ step: 1, stepCount: 2, complete: true })).toBe("commit");
  });

  it("refuses to commit an unfinished item", () => {
    expect(fwd({ step: 1, stepCount: 2, complete: false })).toBe("blocked");
  });

  it("leaves review for the head of the tape rather than crawling forward", () => {
    // Walking one answered cow at a time through work already done is not what
    // the annotator meant by "forward".
    expect(fwd({ step: 1, stepCount: 2, cursor: 2, frontier: 7, complete: true }))
      .toBe("frontier");
  });

  it("is inert while the outline editor is open — it owns its own keys", () => {
    expect(fwd({ step: 0, outlineOpen: true })).toBe("blocked");
  });
});

describe("points-only mode", () => {
  // The points are the last step: there is no question to walk on to.
  const PO: Partial<FlowState> = { inPoints: true, pointsOnly: true, stepCount: 0 };

  it("never walks forward onto a question", () => {
    expect(fwd({ ...PO, cursor: 0, frontier: 0 })).toBe("blocked");
  });

  it("leaves a saved, unchanged revisit for the head of the tape", () => {
    expect(fwd({ ...PO, cursor: 2, frontier: 5 })).toBe("frontier");
  });

  it("still refuses to leave unsaved points", () => {
    expect(fwd({ ...PO, cursor: 2, frontier: 5, pointsReady: false })).toBe("blocked");
  });

  it("walks forward from the outline onto the points", () => {
    expect(fwd({ ...PO, inPoints: false, inGeometry: true, geomDone: true })).toBe("points");
  });

  it("walks back from the points to the outline, then to the previous cow", () => {
    expect(back({ ...PO })).toBe("geometry");
    expect(back({ ...PO, inPoints: false, inGeometry: true, cursor: 3 })).toBe("item");
  });
});

describe("the two directions are mirrors", () => {
  it("forward then back returns to the same step", () => {
    const mid = S({ step: 0, stepCount: 3 });
    expect(forwardTarget(mid)).toBe("question");           // 0 -> 1
    expect(backTarget({ ...mid, step: 1 })).toBe("question"); // 1 -> 0
  });

  it("every question is reachable from BOTH sides", () => {
    // The asymmetry bug in one sentence: question 1 could be left but not
    // returned to. Standing on it, both arrows must offer a move.
    const onFirstQuestion = S({ step: 0, stepCount: 2, geomDone: true });
    expect(backTarget(onFirstQuestion)).toBe("points");
    expect(forwardTarget(onFirstQuestion)).toBe("question");
  });

  it("the passed points step is reachable from BOTH sides", () => {
    const onPoints = S({ ...ON_POINTS });
    expect(backTarget(onPoints)).toBe("geometry");
    expect(forwardTarget(onPoints)).toBe("question");
  });
});
