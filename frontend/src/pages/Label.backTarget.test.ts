import { describe, expect, it } from "vitest";
import { backTarget } from "./Label";

/* What ArrowLeft means. Reported as a bug: a mis-click on question 1 handed the
   annotator to question 2 with no way back — ← moved the TAPE, so on the first
   item of a session it only shook and said the tape went no further, and on any
   later item it abandoned the animal instead of correcting it.

   The ordering of the three cases IS the behaviour, so it is pinned here rather
   than left to the component. */

const AT = (over: Partial<Parameters<typeof backTarget>[0]> = {}) =>
  backTarget({ step: 0, cursor: 0, inGeometry: false, outlineOpen: false, ...over });

describe("backTarget", () => {
  it("steps back a QUESTION when there is an earlier one on this item", () => {
    expect(AT({ step: 1 })).toBe("question");
    expect(AT({ step: 2, cursor: 5 })).toBe("question");
  });

  it("corrects question 1 even on the very first item of a session", () => {
    // The reported bug exactly: cursor 0, so there is no earlier item, but there
    // IS an earlier question. Before the fix this shook and refused.
    expect(AT({ step: 1, cursor: 0 })).toBe("question");
  });

  it("prefers the question over the item — correcting beats abandoning", () => {
    // With both available, stepping back within the animal is the one that
    // matches what the annotator meant by "go back".
    expect(AT({ step: 1, cursor: 3 })).toBe("question");
  });

  it("moves to the previous item once the first question is showing", () => {
    expect(AT({ step: 0, cursor: 3 })).toBe("item");
  });

  it("refuses only at the true start of the tape", () => {
    expect(AT({ step: 0, cursor: 0 })).toBe("none");
  });

  it("never steps back a question from the geometry step", () => {
    // Geometry precedes every question, so there is no earlier question to
    // reach; ← should walk the tape instead.
    expect(AT({ step: 0, cursor: 2, inGeometry: true })).toBe("item");
    expect(AT({ step: 1, cursor: 2, inGeometry: true })).toBe("item");
    expect(AT({ step: 1, cursor: 0, inGeometry: true })).toBe("none");
  });

  it("never steps back a question while the outline editor is open", () => {
    // The editor owns its keys and blocks the arrows outright, so this is
    // belt-and-braces: if one ever reaches here it must not silently reorder the
    // questions underneath an unsaved outline.
    expect(AT({ step: 1, cursor: 2, outlineOpen: true })).toBe("item");
  });
});
