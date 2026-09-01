import { describe, expect, it } from "vitest";
import { classBlocks } from "./LabelProgress";
import type { LabelClass, LabelClassCount, LabelGroup, LabelGroupConsensus } from "../lib/types";

/* THE CLASS BLOCK IS A JOIN, AND JOINS ARE WHERE THE PANEL CAN LIE.

   Two independent sources meet here: the LIVE taxonomy (what a poweruser is
   offering annotators right now) and a corpus count computed server-side over
   answers going back to the first session. They disagree by design — a class is
   archived after work was done against it, a class is created before anyone has
   picked it — and every one of those disagreements has a right answer that is
   not "drop the row".

   What is pinned below is exactly the set of behaviours a reader of the panel
   relies on but cannot verify by looking at it:
     - a class at zero is DRAWN, because the starved class is the reason the
       block exists and an inner join would delete it;
     - a class the taxonomy has retired but the corpus still holds is DRAWN,
       because tidying the taxonomy must not delete evidence of real work;
     - a retired class with nothing behind it is NOT drawn, because a row that
       can only ever say zero is noise;
     - bars scale against the biggest CLASS, so the comparison the block is for
       (which class is starving) is the comparison the eye actually makes;
     - the group header comes from the server's roll-up and is NOT a sum over
       the rows, because a contested instance is on no row and a multi-select
       instance is on several. */

const cls = (over: Partial<LabelClass> & { class_key: string }): LabelClass => ({
  group_key: "sun_exposure",
  name: over.class_key,
  description: "d",
  icon: "",
  sort_order: 100,
  is_escape: false,
  active: true,
  ...over,
});

const group = (classes: LabelClass[], over: Partial<LabelGroup> = {}): LabelGroup => ({
  group_key: "sun_exposure",
  name: "Sun exposure",
  description: null,
  sort_order: 10,
  multi_select: false,
  required: true,
  active: true,
  classes,
  ...over,
});

const count = (
  class_key: string,
  instances: number,
  confirmed = 0,
  over: Partial<LabelClassCount> = {},
): LabelClassCount => ({
  class_key,
  group_key: "sun_exposure",
  class_name: class_key,
  active: true,
  instances,
  confirmed,
  ...over,
});

const total = (over: Partial<LabelGroupConsensus> = {}): LabelGroupConsensus => ({
  group_key: "sun_exposure",
  instances: 0,
  agreed: 0,
  contested: 0,
  confirmed: 0,
  ...over,
});

describe("classBlocks", () => {
  it("draws a class nobody has ever picked, at zero", () => {
    // The row an inner join would have silently dropped — and the only row that
    // tells anyone the class is starving.
    const g = group([
      cls({ class_key: "shaded", sort_order: 10 }),
      cls({ class_key: "direct_sun", sort_order: 20 }),
    ]);
    const [b] = classBlocks([g], [count("shaded", 12)], [total({ instances: 12, agreed: 12 })]);
    expect(b.rows.map((r) => [r.class_key, r.instances])).toEqual([
      ["shaded", 12],
      ["direct_sun", 0],
    ]);
    expect(b.rows[1].fraction).toBe(0);
  });

  it("keeps an archived class that still holds instances, and drops an empty one", () => {
    const g = group([cls({ class_key: "shaded", sort_order: 10 })]);
    const counts = [
      count("shaded", 12),
      count("retired_with_work", 4, 0, { active: false, class_name: "Head probing" }),
      count("retired_empty", 0, 0, { active: false }),
    ];
    const [b] = classBlocks([g], counts, [total({ instances: 16, agreed: 16 })]);
    expect(b.rows.map((r) => r.class_key)).toEqual(["shaded", "retired_with_work"]);
    const retired = b.rows[1];
    expect(retired.archived).toBe(true);
    // Named from the stored snapshot: the live taxonomy no longer offers it, so
    // there is no LabelClass to read a name off.
    expect(retired.name).toBe("Head probing");
  });

  it("scales bars against the largest class, not the group total", () => {
    const g = group([
      cls({ class_key: "a", sort_order: 10 }),
      cls({ class_key: "b", sort_order: 20 }),
      cls({ class_key: "c", sort_order: 30 }),
    ]);
    const counts = [count("a", 80), count("b", 40), count("c", 2)];
    const [b] = classBlocks([g], counts, [total({ instances: 122, agreed: 122 })]);
    // Against the total, the winner would be 66% and every bar would read as
    // short; against the largest class the winner is full and "c" is visibly a
    // sliver, which is the comparison the block is for.
    expect(b.rows[0].fraction).toBe(1);
    expect(b.rows[1].fraction).toBeCloseTo(0.5);
    expect(b.rows[2].fraction).toBeCloseTo(0.025);
  });

  it("expresses confirmed as a share of its own class, so it nests inside the bar", () => {
    const g = group([cls({ class_key: "a", sort_order: 10 }), cls({ class_key: "b", sort_order: 20 })]);
    const counts = [count("a", 80, 20), count("b", 40, 40)];
    const [b] = classBlocks([g], counts, [total({ instances: 120, agreed: 120, confirmed: 60 })]);
    expect(b.rows[0].confirmedFraction).toBeCloseTo(0.25);
    // Fully double-labeled: the inner segment fills its bar exactly, never past it.
    expect(b.rows[1].confirmedFraction).toBe(1);
    // ...and a class at zero must not divide by zero on the way to NaN, which
    // would render as a bar of width "NaN%" and quietly disappear.
    const [empty] = classBlocks([group([cls({ class_key: "z" })])], [], [total()]);
    expect(empty.rows[0].confirmedFraction).toBe(0);
    expect(empty.rows[0].fraction).toBe(0);
  });

  it("takes the group header from the roll-up, not from a sum over the rows", () => {
    // 2 contested instances sit on NO class row. Summing the rows would print 12
    // under a header the server calls 14, and the two missing instances would
    // look like a bug rather than a disagreement.
    const g = group([cls({ class_key: "shaded", sort_order: 10 })]);
    const [b] = classBlocks(
      [g],
      [count("shaded", 12, 3)],
      [total({ instances: 14, agreed: 12, contested: 2, confirmed: 3 })],
    );
    expect(b.instances).toBe(14);
    expect(b.contested).toBe(2);
    expect(b.confirmed).toBe(3);
  });

  it("orders by count, breaking ties on the taxonomy's own display order", () => {
    // Ties are the common case for a fresh class (everything a poweruser adds
    // defaults to sort_order 100), and a tie that resolved differently between
    // renders would make the block jitter while an annotator reads it.
    const g = group([
      cls({ class_key: "first", sort_order: 10 }),
      cls({ class_key: "second", sort_order: 20 }),
      cls({ class_key: "big", sort_order: 30 }),
    ]);
    const counts = [count("first", 5), count("second", 5), count("big", 9)];
    const [b] = classBlocks([g], counts, [total({ instances: 19, agreed: 19 })]);
    expect(b.rows.map((r) => r.class_key)).toEqual(["big", "first", "second"]);
  });

  it("hides archived groups and archived classes with no work behind them", () => {
    const live = group([cls({ class_key: "shaded", sort_order: 10 })]);
    const gone = group([cls({ class_key: "old", sort_order: 10, group_key: "retired" })], {
      group_key: "retired",
      active: false,
    });
    const blocks = classBlocks([live, gone], [count("shaded", 3)], [total({ instances: 3, agreed: 3 })]);
    expect(blocks.map((b) => b.group_key)).toEqual(["sun_exposure"]);
  });
});
