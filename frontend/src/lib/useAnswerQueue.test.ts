import { describe, expect, it } from "vitest";
import { coalesce, parseWrite, storeKeyFor, type LabelWrite } from "./useAnswerQueue";

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

/* Minimal writes for the coalescing rules: only kind, key and (for an outline)
   the verdict matter to them. */
const req = (key: string, extra: Record<string, unknown> = {}) =>
  ({ instance_key: key, anchor, session_id: "s", ...extra }) as never;
const answer = (key: string): LabelWrite => ({ kind: "answer", req: req(key, { answers: {} }) });
const flag = (key: string): LabelWrite => ({ kind: "flag", req: req(key, { reason: "other" }) });
const mask = (key: string, kind: string): LabelWrite => ({ kind: "mask", req: req(key, { kind }) });
const points = (key: string): LabelWrite => ({ kind: "points", req: req(key, { kind: "points" }) });
const kinds = (q: LabelWrite[]) => q.map((w) => `${w.kind}:${w.req.instance_key}${w.kind === "mask" ? ":" + w.req.kind : ""}`);

describe("coalesce", () => {
  it("a flag replaces an unsent answer for the same cow, and the other way round", () => {
    // One annotations row: posted together they raced, and the earlier choice
    // landed last.
    expect(kinds(coalesce([answer("A")], flag("A")))).toEqual(["flag:A"]);
    expect(kinds(coalesce([flag("A")], answer("A")))).toEqual(["answer:A"]);
  });

  it("keeps writes of different families for one cow — all of them must land", () => {
    expect(kinds(coalesce([mask("A", "ok"), points("A")], answer("A")))).toEqual([
      "mask:A:ok",
      "points:A",
      "answer:A",
    ]);
  });

  it("never lets an ok replace a waiting correction or removal", () => {
    expect(kinds(coalesce([mask("A", "polygon")], mask("A", "ok")))).toEqual(["mask:A:polygon"]);
    expect(kinds(coalesce([mask("A", "false_positive")], mask("A", "ok")))).toEqual([
      "mask:A:false_positive",
    ]);
  });

  it("lets a correction replace a waiting ok, as the server would", () => {
    expect(kinds(coalesce([mask("A", "ok")], mask("A", "polygon")))).toEqual(["mask:A:polygon"]);
  });

  it("never touches another cow's writes", () => {
    expect(kinds(coalesce([answer("B")], flag("A")))).toEqual(["answer:B", "flag:A"]);
  });
});

describe("storeKeyFor", () => {
  it("keeps one unsent-writes mirror per account", () => {
    expect(storeKeyFor("alice")).not.toEqual(storeKeyFor("bob"));
    expect(storeKeyFor("alice")).toContain("alice");
  });
});

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
