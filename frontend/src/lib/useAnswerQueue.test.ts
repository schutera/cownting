import { describe, expect, it } from "vitest";
import { parseWrite } from "./useAnswerQueue";

/* The sessionStorage replay for the body-points write. The mirror is untrusted
   input and is only read after a crash or a reload, so a record the route would
   refuse must be dropped here — the queue retries a refused write forever. */

const anchor = {
  dataset_id: "2026-07-03",
  camera_id: "camera_01",
  frame_file: "00000001.jpg",
  bbox: [10, 20, 110, 220],
  ordinal: 0,
  ts: null,
  frame_sig: null,
};

const POINTS = [[100, 60], [60, 30], [15, 40], [70, 215], null];

function rec(req: Record<string, unknown>) {
  return { kind: "points", req: { instance_key: "k1", anchor, session_id: "s", ...req } };
}

describe("parseWrite: points", () => {
  it("rebuilds a points write field by field", () => {
    const w = parseWrite(rec({ kind: "points", points: POINTS, serve_event_id: 7 }));
    expect(w).not.toBeNull();
    expect(w?.kind).toBe("points");
    if (w?.kind !== "points") return;
    expect(w.req.points).toEqual(POINTS);
    expect(w.req.serve_event_id).toBe(7);
  });

  it("rebuilds a too_small write with no points", () => {
    const w = parseWrite(rec({ kind: "too_small", points: null }));
    expect(w?.kind).toBe("points");
    if (w?.kind !== "points") return;
    expect(w.req.kind).toBe("too_small");
    expect(w.req.points).toBeNull();
  });

  it("drops every record the route would refuse", () => {
    expect(parseWrite(rec({ kind: "points", points: POINTS.slice(0, 4) }))).toBeNull();
    expect(parseWrite(rec({ kind: "points", points: [null, null, null, null, null] }))).toBeNull();
    expect(parseWrite(rec({ kind: "points", points: [[1], ...POINTS.slice(1)] }))).toBeNull();
    expect(parseWrite(rec({ kind: "points", points: null }))).toBeNull();
    expect(parseWrite(rec({ kind: "too_small", points: POINTS }))).toBeNull();
    expect(parseWrite(rec({ kind: "nonsense", points: POINTS }))).toBeNull();
  });
});
