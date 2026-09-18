import { describe, expect, it } from "vitest";
import {
  PRESETS,
  activePreset,
  usablePresets,
  questionFor,
  groupPhrase,
} from "./crossfilterPresets";

/* THE PRESETS ARE THE ONLY SIGNPOST ON A 64-WAY CONTROL.

   The pivot accepts any pairing of eight dimensions, and almost none of those
   pairings mean anything: a dimension crossed with itself is a diagonal, and
   "under panels" crossed with "shade" restates the geometry. What is pinned:

     - a preset is never self-crossed, which would render one full-width bar
       per group and look like a bug;
     - a preset only offers itself when BOTH its dimensions have data, because
       an empty chart reads as breakage rather than as a missing input;
     - the active chip tracks the live pivot, so touching a dropdown visibly
       leaves the preset rather than leaving a stale chip lit;
     - every pivot gets a readable question, including the ones no preset
       covers — the caption is what makes a conditional proportion legible, and
       falling back to "" there would silently strip the meaning from exactly
       the combinations a user built by hand. */

describe("the preset set itself", () => {
  it("offers a question for each, not just a label", () => {
    for (const p of PRESETS) {
      expect(p.question.length).toBeGreaterThan(10);
      expect(p.question.endsWith("?")).toBe(true);
    }
  });

  it("never crosses a dimension with itself", () => {
    for (const p of PRESETS) expect(p.group).not.toBe(p.ratio);
  });

  it("has unique keys and unique pivots", () => {
    expect(new Set(PRESETS.map((p) => p.key)).size).toBe(PRESETS.length);
    expect(new Set(PRESETS.map((p) => `${p.group}/${p.ratio}`)).size).toBe(PRESETS.length);
  });

  it("leads with the question the study turns on", () => {
    // Shade use is why the cameras are on a solar farm; it must not be buried.
    expect(PRESETS[0].group).toBe("hour");
    expect(PRESETS[0].ratio).toBe("sun");
  });
});

describe("activePreset — the chip follows the pivot", () => {
  it("lights up on an exact match", () => {
    expect(activePreset("hour", "sun")?.key).toBe("shade-by-hour");
  });

  it("goes dark the moment either dropdown moves", () => {
    expect(activePreset("hour", "behaviour")?.key).not.toBe("shade-by-hour");
    expect(activePreset("region", "sun")).toBeNull();
  });

  it("is not fooled by a swapped pivot, which is a different question", () => {
    // "of each hour, how much shade" and "of each light condition, which hours"
    // are genuinely different readings; only the first is the preset.
    expect(activePreset("sun", "hour")).toBeNull();
  });
});

describe("usablePresets — never offer a chip that renders empty", () => {
  it("keeps everything when every dimension has data", () => {
    expect(usablePresets({}).length).toBe(PRESETS.length);
  });

  it("drops a preset whose GROUP dimension has no data", () => {
    const got = usablePresets({ region: false });
    expect(got.some((p) => p.group === "region")).toBe(false);
    expect(got.some((p) => p.key === "shade-by-hour")).toBe(true);
  });

  it("drops a preset whose RATIO dimension has no data", () => {
    const got = usablePresets({ sun: false });
    expect(got.some((p) => p.ratio === "sun")).toBe(false);
    // ...but a preset merely mentioning another dimension survives
    expect(got.some((p) => p.key === "panel-by-hour")).toBe(true);
  });

  it("can drop everything without throwing", () => {
    expect(usablePresets({ hour: false, sun: false, region: false, behaviour: false, panel: false }))
      .toEqual([]);
  });
});

describe("questionFor — every pivot reads as a sentence", () => {
  const labelOf = (k: string) =>
    ({ hour: "Hour of day", sun: "Sun exposure", posture: "Posture", region: "Count area" })[k] ?? k;

  it("uses the preset's own wording when one matches", () => {
    expect(questionFor("hour", "sun", labelOf)).toBe(PRESETS[0].question);
  });

  it("still builds a question for a hand-made pivot no preset covers", () => {
    const q = questionFor("region", "posture", labelOf);
    expect(q).toContain("count area");
    expect(q).toContain("posture");
    expect(q.endsWith("?")).toBe(true);
  });

  it("lowercases the dimension inside the sentence", () => {
    expect(groupPhrase("Hour of day")).toBe("in each hour of day");
  });
});
