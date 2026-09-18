import { describe, expect, it } from "vitest";
import { ringFor, lightTintFor, ringLegend } from "./areaAttrs";
import type { AreaAttrs } from "./api";
import type { PostureBreakdown } from "./types";

/* THE MAP HAS TWO SOURCES FOR THE SAME ANIMALS, AND A BADGE CAN ONLY SHOW ONE.

   `posture` is the segmenter's geometric proxy, on every row since M1.
   `attrs` is the crop model's read, on the rows it has scored. They overlap,
   they disagree, and one of them is absent on any day the model has not run —
   so the ring's job is to pick, and the failure modes are all silent ones:

     - an unscored day must keep the ring it has always had, not go blank,
       because a blank ring reads as "no cows" rather than "no model";
     - a scored day must NOT keep the proxy, or the whole point of the model
       (feeding, which elongation cannot see) is invisible;
     - `unsure` must be drawn, because a ring that hides the share the model
       declined to call overstates how much of the area is understood;
     - the legend must name every class the source can show, not only the ones
       that happen to be non-zero right now, or the key changes as cows move.

   The aura's hue is the other decision: it used to mean "the count went up or
   down", it now means "this is the light these animals are in", and on a tie
   it must prefer a real reading over "could not tell". */

const attrs = (over: Partial<AreaAttrs> = {}): AreaAttrs => ({
  scored: 0,
  feeding: 0,
  lying: 0,
  standing: 0,
  unsure: 0,
  direct_sun: 0,
  shaded: 0,
  sun_nv: 0,
  ...over,
});

const posture = (over: Partial<PostureBreakdown> = {}): PostureBreakdown => ({
  standing: 0,
  lying: 0,
  unknown: 0,
  ...over,
});

describe("ringFor — which source the badge ring shows", () => {
  it("prefers the crop model wherever it has scored the area", () => {
    const r = ringFor(
      attrs({ scored: 10, feeding: 5, lying: 3, standing: 2 }),
      posture({ standing: 7, lying: 3 }),
    );
    expect(r.source).toBe("model");
    expect(r.total).toBe(10);
    expect(r.segments.map((s) => s.key)).toEqual(["feeding", "lying", "standing"]);
    expect(r.segments.map((s) => s.value)).toEqual([5, 3, 2]);
  });

  it("falls back to the posture proxy on a day the model has not scored", () => {
    const r = ringFor(attrs({ scored: 0 }), posture({ standing: 4, lying: 1, unknown: 2 }));
    expect(r.source).toBe("posture");
    expect(r.total).toBe(7);
    expect(r.segments.map((s) => s.key)).toEqual(["standing", "lying", "unknown"]);
  });

  it("falls back when the model block is missing entirely (older API, no attrs)", () => {
    const r = ringFor(undefined, posture({ standing: 2 }));
    expect(r.source).toBe("posture");
    expect(r.segments).toHaveLength(1);
  });

  it("draws the unsure share rather than hiding it", () => {
    const r = ringFor(attrs({ scored: 10, standing: 4, unsure: 6 }), undefined);
    expect(r.source).toBe("model");
    const unsure = r.segments.find((s) => s.key === "unsure");
    expect(unsure?.value).toBe(6);
    // and it is visually distinct from every posture colour
    const colors = new Set(r.segments.map((s) => s.color));
    expect(colors.size).toBe(r.segments.length);
  });

  it("drops empty classes so the ring has no zero-width arcs", () => {
    const r = ringFor(attrs({ scored: 3, lying: 3 }), undefined);
    expect(r.segments.map((s) => s.key)).toEqual(["lying"]);
  });

  it("draws nothing when neither source has anything — never a divide by zero", () => {
    expect(ringFor(undefined, undefined)).toEqual({ source: "none", segments: [], total: 0 });
    expect(ringFor(attrs(), posture())).toEqual({ source: "none", segments: [], total: 0 });
  });

  it("does not trust a scored count with no classes behind it", () => {
    // Cannot come from the API, but the ring must not divide by a total it
    // cannot fill; it degrades to the proxy instead of drawing a broken arc.
    const r = ringFor(attrs({ scored: 9 }), posture({ standing: 1 }));
    expect(r.source).toBe("posture");
  });
});

describe("lightTintFor — what the aura's colour means", () => {
  it("takes the dominant light reading", () => {
    const t = lightTintFor(attrs({ scored: 10, direct_sun: 6, shaded: 3, sun_nv: 1 }));
    expect(t?.key).toBe("direct_sun");
    expect(t?.share).toBeCloseTo(0.6);
  });

  it("reports shade when shade dominates", () => {
    const t = lightTintFor(attrs({ scored: 10, direct_sun: 2, shaded: 8 }));
    expect(t?.key).toBe("shaded");
    expect(t?.label).toBe("in shade");
  });

  it("says so honestly when the light mostly could not be read", () => {
    const t = lightTintFor(attrs({ scored: 10, direct_sun: 1, sun_nv: 9 }));
    expect(t?.key).toBe("sun_nv");
    expect(t?.share).toBeCloseTo(0.9);
  });

  it("prefers a real reading to 'could not tell' on a tie", () => {
    expect(lightTintFor(attrs({ scored: 10, direct_sun: 5, sun_nv: 5 }))?.key).toBe("direct_sun");
    expect(lightTintFor(attrs({ scored: 10, shaded: 5, sun_nv: 5 }))?.key).toBe("shaded");
  });

  it("returns nothing on an unscored area, so the aura keeps its old meaning", () => {
    expect(lightTintFor(attrs({ scored: 0, direct_sun: 3 }))).toBeNull();
    expect(lightTintFor(undefined)).toBeNull();
  });

  it("returns nothing when the sun head wrote no class at all", () => {
    expect(lightTintFor(attrs({ scored: 5 }))).toBeNull();
  });
});

describe("ringLegend — one key per map", () => {
  it("names every model class once any visible area is scored", () => {
    const rings = [
      ringFor(attrs({ scored: 4, feeding: 4 }), undefined),
      ringFor(undefined, posture({ standing: 2 })),
    ];
    const legend = ringLegend(rings);
    expect(legend.source).toBe("model");
    // 'lying' and 'standing' are in the key even though no area shows them now
    expect(legend.entries.map((e) => e.key)).toEqual(["feeding", "lying", "standing", "unsure"]);
  });

  it("names the proxy's classes when nothing is scored", () => {
    const legend = ringLegend([ringFor(undefined, posture({ standing: 1 }))]);
    expect(legend.source).toBe("posture");
    expect(legend.entries.map((e) => e.key)).toEqual(["standing", "lying", "unknown"]);
  });

  it("has no key at all when there is nothing to draw", () => {
    expect(ringLegend([])).toEqual({ source: "none", entries: [] });
    expect(ringLegend([ringFor(undefined, undefined)]).entries).toEqual([]);
  });
});
