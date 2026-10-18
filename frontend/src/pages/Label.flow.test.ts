// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/* The Label page, rendered whole in jsdom against a stub of the label API, and
   driven by keys and clicks as a labeler drives it. Each case is a defect an
   adversarial review found in a flow; each failed on the code before its fix.
   The pure step rules have their own tests (Label.backTarget, pointSteps);
   these are the paths that only show up when the page, the keyboard handler and
   the write queue run together. */

// A whole item is some forty renders; the slowest case takes about 4 s here,
// too near the 5 s default for a slower CI runner.
vi.setConfig({ testTimeout: 30_000 });

const g = globalThis as Record<string, unknown>;
g.IS_REACT_ACT_ENVIRONMENT = true;
class NoResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}
g.ResizeObserver = NoResizeObserver;

type Json = Record<string, any>;

/* ------------------------------------------------------------- the stub API */

const posts: { url: string; body: Json }[] = [];
const gets: string[] = [];
let queueServed = false;
let geomDone = false;
let mixed = false;
let failPosts = false;
let stats: Json = {};
/** Fields every served item takes on top of its defaults. */
let itemOver: Json = {};

function cls(group: string, key: string, order: number, escape = false) {
  return {
    class_key: `${group}.${key}`, group_key: group, name: key, description: "d", icon: "",
    sort_order: order, is_escape: escape, active: true,
  };
}

const TAXONOMY = {
  revision: 1,
  points: ["nose", "shoulders", "tail_base", "front_hoof", "hind_hoof"],
  groups: [
    {
      group_key: "sun", name: "Sun exposure", description: null, sort_order: 0, multi_select: false,
      required: true, active: true,
      classes: [cls("sun", "sun", 0), cls("sun", "shade", 1), cls("sun", "cant", 2, true)],
    },
    {
      group_key: "beh", name: "Behaviour", description: null, sort_order: 1, multi_select: false,
      required: true, active: true,
      classes: [cls("beh", "graze", 0), cls("beh", "stand", 1), cls("beh", "lie", 2)],
    },
  ],
};

function item(key: string, ordinal: number, over: Json = {}) {
  return {
    instance_key: key, dataset_id: "ds", queue_reason: "uniform", day: "2026-06-01",
    camera_id: "cam1", frame_file: `f${ordinal}.jpg`, bbox: [900, 400, 1100, 600], ordinal,
    score: 0.9, frame_sig: "sig", crop_url: `/crop/${key}?pad=0.35`, frame_url: `/frame/${key}`,
    crop_w: 280, crop_h: 280, ring: [40, 40, 240, 240], n_annotators: 0, target: 2,
    overlap: false, serve_event_id: 10 + ordinal, mask: null, mask_frame: null,
    mask_seed: "bbox", frame_w: 1920, frame_h: 1080, geom_done: geomDone,
    crop_levels: [
      { pad: 0.1, url: `/crop/${key}?pad=0.1`, src: [880, 380, 1120, 620], out: 240 },
      { pad: 0.35, url: `/crop/${key}?pad=0.35`, src: [860, 360, 1140, 640], out: 280 },
      { pad: 2.5, url: `/crop/${key}?pad=2.5`, src: [400, -100, 1600, 1100], out: 600 },
    ],
    crop_level: 1, points: null, points_done: false, point_bounds: [400, -100, 1600, 1100],
    points_only: false, ...itemOver, ...over,
  };
}

const BASE_STATS = {
  pool_total: 10, pool_labeled: 0, pool_covered: 0, remaining: 10, my_labeled: 0, my_skipped: 0,
  my_median_ms: null, my_points: 0, my_too_small: 0, points_backlog: 3, annotators: 1,
  auth_disabled: true, filters: { dataset: null, camera: null }, classes: [], class_groups: [],
};

function resp(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300, status, statusText: "OK",
    json: async () => body, text: async () => JSON.stringify(body), headers: new Map(),
  };
}

async function stubFetch(url: string, init?: { method?: string; body?: string }) {
  const u = String(url);
  if (init?.method === "POST") {
    const body = init.body ? JSON.parse(init.body) : {};
    if (failPosts && !u.startsWith("/api/label/events")) return resp({ detail: "down" }, 500);
    if (!u.startsWith("/api/label/events")) posts.push({ url: u, body });
    if (u.startsWith("/api/label/points")) {
      return resp({ ok: true, edit_id: posts.length, version: 1, points: body.points ?? null });
    }
    return resp({ ok: true, annotation_id: posts.length, edit_id: posts.length, version: 1 });
  }
  gets.push(u);
  if (u.startsWith("/api/me")) return resp({ username: "u", role: "user", auth_disabled: true });
  if (u.startsWith("/api/label/taxonomy")) return resp(TAXONOMY);
  if (u.startsWith("/api/label/progress")) return resp(stats);
  if (u.startsWith("/api/label/queue")) {
    const items = queueServed
      ? []
      : mixed
        ? [item("A", 0), item("B", 1, { points_only: true, geom_done: true }), item("C", 2)]
        : [item("A", 0), item("B", 1), item("C", 2)];
    queueServed = true;
    return resp({ items });
  }
  return resp({}, 404);
}

beforeEach(() => {
  posts.length = 0;
  gets.length = 0;
  queueServed = false;
  geomDone = false;
  mixed = false;
  failPosts = false;
  stats = { ...BASE_STATS };
  itemOver = {};
  sessionStorage.clear();
  vi.stubGlobal("fetch", stubFetch);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/* ------------------------------------------------------------- the driver */

async function boot() {
  const React = await import("react");
  const { createRoot } = await import("react-dom/client");
  const { MemoryRouter } = await import("react-router-dom");
  const { AuthProvider } = await import("../lib/auth");
  const Label = (await import("./Label")).default;
  document.body.innerHTML = "";
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  const { act } = React;
  const settle = async () => {
    for (let i = 0; i < 8; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 0));
      });
    }
  };
  await act(async () => {
    root.render(
      React.createElement(
        MemoryRouter,
        { initialEntries: ["/label"] },
        React.createElement(AuthProvider, null, React.createElement(Label)),
      ),
    );
  });
  await settle();

  const key = async (k: string, code = "") => {
    await act(async () => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: k, code, bubbles: true }));
      window.dispatchEvent(new KeyboardEvent("keyup", { key: k, code, bubbles: true }));
    });
    await settle();
  };
  /** The point editor: the one SVG with a crosshair cursor. */
  const editor = () =>
    [...document.querySelectorAll("svg")].find((s) => s.style.cursor === "crosshair");
  /** A click on the photo at full-frame (fx, fy). The default zoom level's
      viewBox is 860 360 280 280, drawn at 280x280, so one px is one px. */
  const clickPhoto = async (fx: number, fy: number) => {
    const svg = editor();
    if (!svg) throw new Error("no point editor on screen");
    svg.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 280, height: 280, right: 280, bottom: 280, x: 0, y: 0 }) as DOMRect;
    await act(async () => {
      svg.dispatchEvent(
        new MouseEvent("pointerdown", { bubbles: true, button: 0, clientX: fx - 860, clientY: fy - 360 }),
      );
    });
    await settle();
  };
  const click = async (el: Element, detail = 1) => {
    await act(async () => {
      el.dispatchEvent(new MouseEvent("click", { bubbles: true, detail }));
    });
    await settle();
  };
  const text = () => document.body.textContent ?? "";
  const unmount = () => act(async () => root.unmount());
  return { key, clickPhoto, click, text, settle, act, editor, unmount };
}

type Page = Awaited<ReturnType<typeof boot>>;

/** The writes so far, as route:cow:kind. */
const writes = () =>
  posts.map((q) => `${q.url.replace("/api/label/", "")}:${q.body.instance_key}:${q.body.kind ?? ""}`);

/** Which step the panel shows. */
const where = (t: string) =>
  t.includes("Body points")
    ? "POINTS"
    : t.includes("Is there a cow")
      ? "OUTLINE"
      : t.includes("Sun exposure")
        ? "QUESTIONS"
        : "?";

const cropSrc = () => document.querySelector("img")?.getAttribute("src") ?? "";

const button = (label: string) =>
  [...document.querySelectorAll("button")].find((b) => (b.textContent ?? "").includes(label))!;

const FIVE: [number, number][] = [[920, 450], [960, 410], [1080, 420], [950, 590], [1060, 595]];

async function placeFive(h: Page) {
  for (const [x, y] of FIVE) await h.clickPhoto(x, y);
}

/** One ordinary cow from the points step on: five points, Enter, two answers. */
async function finishCow(h: Page) {
  await placeFive(h);
  await h.key("Enter");
  await h.key("y", "KeyZ");
  await h.key("y", "KeyZ");
}

/* ------------------------------------------------------------- the cases */

describe("Label page: body points", () => {
  it("the fifth point does not save; Enter saves the whole set", async () => {
    const h = await boot();
    await h.key("Enter"); // outline is fine -> points
    await placeFive(h);
    expect(where(h.text())).toBe("POINTS");
    expect(h.text()).toContain("All five points are given");
    expect(writes()).toEqual(["mask-fix:A:ok"]);
    await h.key("x", "KeyX"); // an extra X must not answer anything
    await h.key("ArrowRight"); // nor may the arrow save
    expect(writes()).toEqual(["mask-fix:A:ok"]);
    expect(where(h.text())).toBe("POINTS");
    await h.key("Backspace"); // take one back, place it again
    await h.clickPhoto(1060, 595);
    await h.key("Enter");
    expect(writes()).toEqual(["mask-fix:A:ok", "points:A:points"]);
    expect(where(h.text())).toBe("QUESTIONS");
    expect(posts.find((p) => p.url.includes("/points"))!.body.points).toEqual(FIVE);
    await h.unmount();
  });

  it("a failed zoom level other than the default does not block the cow", async () => {
    const h = await boot();
    await h.key("Enter");
    await h.clickPhoto(950, 450);
    await h.act(async () => {
      h.editor()!.dispatchEvent(new WheelEvent("wheel", { deltaY: -100, bubbles: true, cancelable: true }));
    });
    await h.settle();
    const img = (pad: string) =>
      [...document.querySelectorAll("img")].find((i) => (i.getAttribute("src") ?? "").includes(`pad=${pad}`));
    const tight = img("0.1");
    expect(tight).toBeTruthy();
    await h.act(async () => {
      tight!.dispatchEvent(new Event("error"));
    });
    await h.settle();
    expect(h.text()).not.toContain("This image can't be shown");
    expect(h.text()).toContain("Body points");
    expect(img("0.35")).toBeTruthy(); // back on the default level
    // The DEFAULT level failing still marks the cow as one that can't be shown.
    await h.act(async () => {
      img("0.35")!.dispatchEvent(new Event("error"));
    });
    await h.settle();
    expect(h.text()).toContain("This image can't be shown");
    await h.unmount();
  });

  it("a held Enter after a click on Too small writes nothing more", async () => {
    geomDone = true;
    mixed = true;
    const h = await boot(); // A's outline is judged: the page opens on its points
    await h.act(async () => {
      button("Too small").click();
    });
    await h.settle();
    const afterClick = writes().join(" ");
    for (let i = 0; i < 5; i++) {
      await h.act(async () => {
        const ev = new KeyboardEvent("keydown", { key: "Enter", repeat: true, bubbles: true, cancelable: true });
        (document.activeElement ?? window).dispatchEvent(ev);
      });
    }
    await h.settle();
    expect(writes().join(" ")).toBe(afterClick);
    await h.unmount();
  });
});

describe("Label page: removed cows", () => {
  it("a removed cow, walked back to, is not a trap and gets no writes", async () => {
    const h = await boot();
    await h.key("x", "KeyX"); // A: no cow -> on to B
    expect(writes()).toEqual(["mask-fix:A:false_positive"]);
    await h.key("ArrowLeft"); // B's outline -> back to A
    expect(where(h.text())).not.toBe("POINTS");
    for (let i = 0; i < 4 && !h.text().includes("Is there a cow"); i++) await h.key("ArrowRight");
    expect(writes()).toEqual(["mask-fix:A:false_positive"]);
    await h.unmount();
  });

  it("Enter on the outline of a cow the labeler removed is refused", async () => {
    const h = await boot();
    await h.key("x", "KeyX"); // A removed
    await h.key("ArrowLeft"); // back on A
    await h.key("ArrowLeft"); // A's outline step
    expect(cropSrc()).toContain("/crop/A");
    await h.key("Enter");
    expect(h.text()).toContain("this cow is removed");
    expect(writes()).toEqual(["mask-fix:A:false_positive"]);
    await h.unmount();
  });

  it("answer letters on a removed backlog cow write nothing", async () => {
    mixed = true;
    const h = await boot();
    await h.key("Enter");
    await finishCow(h); // A, done properly
    await h.key("ArrowLeft"); // B, the backlog cow: back to its outline
    await h.key("x", "KeyX"); // and remove it
    const before = writes().join(" ");
    await h.key("ArrowLeft"); // back onto removed B
    expect(cropSrc()).toContain("/crop/B");
    await h.key("x", "KeyX");
    await h.key("x", "KeyX");
    await h.key("y", "KeyZ");
    expect(writes().join(" ")).toBe(before);
    expect(writes().some((w) => w.startsWith("submit:B"))).toBe(false);
    await h.key("ArrowRight"); // and -> leaves it with no write
    expect(writes().join(" ")).toBe(before);
    await h.unmount();
  });
});

describe("Label page: the outline editor", () => {
  it("holds a dragged node inside where the route accepts one", async () => {
    // Bounds tighter than the default view, so a drag can leave them while the
    // pointer is still on the photo. Saved outside them, the correction is
    // refused by the route, and the queue retries it forever.
    const bounds = [880, 380, 1120, 620];
    itemOver = { point_bounds: bounds };
    const h = await boot();
    await h.key("e", "KeyE"); // open the editor on the box rectangle
    const svg = h.editor()!;
    svg.getBoundingClientRect = () =>
      ({ left: 0, top: 0, width: 280, height: 280, right: 280, bottom: 280, x: 0, y: 0 }) as DOMRect;
    Object.assign(svg, {
      setPointerCapture: () => {},
      releasePointerCapture: () => {},
      hasPointerCapture: () => false,
    });
    // Same 1:1 view as the points: frame (fx, fy) is client (fx - 860, fy - 360).
    const pointer = async (type: string, fx: number, fy: number) => {
      await h.act(async () => {
        svg.dispatchEvent(
          new MouseEvent(type, { bubbles: true, button: 0, clientX: fx - 860, clientY: fy - 360 }),
        );
      });
    };
    await pointer("pointerdown", 900, 400); // the box's top-left node
    await pointer("pointermove", 861, 361); // on the photo, outside the bounds
    await pointer("pointerup", 861, 361);
    await h.settle();
    await h.key("Enter");
    const fix = posts.find((p) => p.url.includes("/mask-fix"))!;
    expect(fix.body.kind).toBe("polygon");
    const polygon: [number, number][] = fix.body.polygon;
    expect(polygon[0]).toEqual([880, 380]);
    for (const [x, y] of polygon) {
      expect(x).toBeGreaterThanOrEqual(bounds[0]);
      expect(y).toBeGreaterThanOrEqual(bounds[1]);
      expect(x).toBeLessThanOrEqual(bounds[2]);
      expect(y).toBeLessThanOrEqual(bounds[3]);
    }
    await h.unmount();
  });
});

describe("Label page: the backfill mix", () => {
  it("serves an ordinary cow, a points-only cow, an ordinary cow", async () => {
    mixed = true;
    const h = await boot();
    const q = gets.find((u) => u.startsWith("/api/label/queue"))!;
    expect(q).not.toContain("mode=");
    expect(q).toContain("mix=1");
    expect(h.text()).not.toContain("Points only");
    // A: ordinary — outline, points, both questions.
    expect(h.text()).toContain("[1/4]");
    await h.key("Enter");
    await finishCow(h);
    // B: backlog — straight to the points, two steps, no Classify, no flag.
    expect(cropSrc()).toContain("/crop/B");
    expect(where(h.text())).toBe("POINTS");
    expect(h.text()).toContain("[2/2]");
    expect(h.text()).not.toContain("Classify");
    expect(h.text()).toContain("cows still need body points");
    await h.key("f", "KeyF");
    expect(h.text()).toContain("no flags on a points-only cow");
    await placeFive(h);
    await h.key("Enter");
    // C: ordinary again, from the outline.
    expect(cropSrc()).toContain("/crop/C");
    expect(h.text()).toContain("[1/4]");
    const done = ["mask-fix:A:ok", "points:A:points", "submit:A:", "points:B:points"];
    expect(writes()).toEqual(done);
    // Back onto B: its saved points, and -> returns to the head with no write.
    await h.key("ArrowLeft");
    expect(cropSrc()).toContain("/crop/B");
    expect(h.editor()?.querySelectorAll("ellipse").length).toBe(5);
    await h.key("ArrowRight");
    expect(cropSrc()).toContain("/crop/C");
    expect(writes()).toEqual(done);
    await h.unmount();
  });
});

describe("Label page: keys and clicks that must not answer", () => {
  it("Enter with an answer tile focused is swallowed and writes nothing", async () => {
    const h = await boot();
    await h.key("Enter"); // outline
    await placeFive(h);
    await h.key("Enter"); // points saved -> question 1
    expect(where(h.text())).toBe("QUESTIONS");
    const tile = document.querySelector("button[aria-pressed]") as HTMLButtonElement;
    tile.focus();
    const before = writes().join(" ");
    let prevented = false;
    await h.act(async () => {
      const ev = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
      tile.dispatchEvent(ev);
      prevented = ev.defaultPrevented;
    });
    await h.settle();
    expect(prevented).toBe(true);
    expect(h.text()).toContain("Enter does nothing here");
    expect(writes().join(" ")).toBe(before);
    await h.unmount();
  });

  it("a double-click on No cow removes one cow, not two", async () => {
    const h = await boot();
    await h.click(button("No cow"), 1);
    // The second click of the pair lands on the NEXT cow's button.
    await h.click(button("No cow"), 2);
    expect(writes()).toEqual(["mask-fix:A:false_positive"]);
    expect(cropSrc()).toContain("/crop/B");
    await h.unmount();
  });
});

describe("Label page: the end of the queue and the score", () => {
  it("after You're caught up, the arrows can return to Check again", async () => {
    geomDone = true;
    const h = await boot();
    for (let i = 0; i < 3; i++) await finishCow(h);
    expect(h.text()).toContain("caught up");
    await h.key("ArrowLeft");
    expect(h.text()).toContain("reviewing");
    for (let i = 0; i < 3; i++) await h.key("ArrowRight");
    expect(h.text()).toContain("Check again");
    await h.unmount();
  });

  it("the progress target counts only what the queue can still serve this labeler", async () => {
    // Six answered; the pool still counts those six among its ten remaining,
    // but none of the ten is one this labeler can be served again.
    stats = { ...BASE_STATS, my_labeled: 6, remaining: 6, mine_remaining: 0 };
    const h = await boot();
    expect(h.text()).toContain("/ 6 labeled");
    expect(h.text()).not.toContain("/ 12 labeled");
    await h.unmount();
  });

  it("an answer then a flag for one cow, offline, leave only the flag queued", async () => {
    geomDone = true;
    failPosts = true;
    const h = await boot();
    await finishCow(h); // A answered; the post fails, so it stays queued
    await h.key("ArrowLeft"); // B's points -> B's outline
    await h.key("ArrowLeft"); // -> back on A
    expect(cropSrc()).toContain("/crop/A");
    await h.key("f", "KeyF");
    await h.key("y", "KeyZ"); // the first flag reason
    const store = Object.keys(sessionStorage).find((k) => k.startsWith("cownting.label.pending:"));
    expect(store).toBeTruthy();
    expect(Object.keys(sessionStorage)).not.toContain("cownting.label.pending");
    const queued: { kind: string; req: { instance_key: string } }[] = JSON.parse(
      sessionStorage.getItem(store!)!,
    );
    const forA = queued.filter((q) => q.req.instance_key === "A").map((q) => q.kind);
    expect(forA.filter((k) => k === "answer" || k === "flag")).toEqual(["flag"]);
    await h.unmount();
  });
});
