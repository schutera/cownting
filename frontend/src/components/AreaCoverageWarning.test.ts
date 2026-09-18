import { describe, expect, it } from "vitest";
import { coverageOf } from "./AreaCoverageWarning";
import type { DatasetRow } from "../lib/types";

/* MISSING POLYGONS LOOK EXACTLY LIKE A QUIET DAY.

   Count areas are drawn per day, because the cameras move. Upload a day, forget
   the polygons, and every number on the dashboard is still right — there simply
   are no area counts, the map is an empty orthophoto, and a breakdown by area
   is blank. Nothing is red. Two days shipped like that and went unnoticed for
   three weeks (2025-09-03 and 2025-09-04, 5,281 cows).

   So the decision of WHEN to shout is the thing worth pinning:

     - no polygons at all is always wrong, and says so loudly;
     - polygons that miss most of the herd is a softer, real signal — usually a
       camera moved after the areas were drawn — and must not use the same voice,
       or the loud one stops meaning anything;
     - cows legitimately wander outside the pen, so a day with decent coverage
       says nothing;
     - a day with no cows at all cannot have bad coverage, and must not be
       accused of it;
     - an older server sends no coverage fields, and silence is the only honest
       output then — guessing "no areas" would flag every day on the page. */

const row = (over: Partial<DatasetRow>): DatasetRow => ({
  dataset_id: "2025-09-03",
  day: "2025-09-03",
  label: null,
  status: "localized",
  n_frames: 100,
  n_detections: 1000,
  n_cameras: 3,
  ...over,
});

describe("coverageOf", () => {
  it("shouts when a day has no polygons at all", () => {
    expect(coverageOf(row({ n_areas: 0, n_unassigned: 1000 })).kind).toBe("none");
  });

  it("shouts even if, by some accident, everything is assigned", () => {
    // n_areas is read from the day's own file; it is the authority on whether
    // anyone drew anything. Stale assignments must not hide a deleted file.
    expect(coverageOf(row({ n_areas: 0, n_unassigned: 0 })).kind).toBe("none");
  });

  it("uses the quieter voice when polygons exist but miss most of the herd", () => {
    const c = coverageOf(row({ n_areas: 3, n_detections: 1000, n_unassigned: 900 }));
    expect(c.kind).toBe("thin");
    if (c.kind === "thin") {
      expect(c.pct).toBeCloseTo(0.1);
      expect(c.inside).toBe(100);
      expect(c.total).toBe(1000);
    }
  });

  it("stays silent when coverage is merely imperfect — cows do leave the pen", () => {
    expect(coverageOf(row({ n_areas: 3, n_detections: 1000, n_unassigned: 500 })).kind).toBe("ok");
    expect(coverageOf(row({ n_areas: 3, n_detections: 1000, n_unassigned: 740 })).kind).toBe("ok");
  });

  it("does not accuse a day that has no cows to cover", () => {
    expect(coverageOf(row({ n_areas: 2, n_detections: 0, n_unassigned: 0 })).kind).toBe("ok");
  });

  it("says nothing when the server did not report coverage", () => {
    expect(coverageOf(row({})).kind).toBe("ok");
    expect(coverageOf(null).kind).toBe("ok");
    expect(coverageOf(undefined).kind).toBe("ok");
  });

  it("treats a missing unassigned count as fully covered rather than as a fault", () => {
    // n_areas present but n_unassigned absent: polygons exist and we know
    // nothing against them, so do not invent an alarm.
    expect(coverageOf(row({ n_areas: 4 })).kind).toBe("ok");
  });
});
