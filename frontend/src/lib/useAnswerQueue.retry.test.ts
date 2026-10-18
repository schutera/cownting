// @vitest-environment jsdom
import { createElement } from "react";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { InstanceAnchor } from "./types";
import { storeKeyFor, useAnswerQueue, type AnswerQueue, type LabelWrite } from "./useAnswerQueue";

/* The write queue's timers and its sessionStorage mirror, run in a real React
   mount. The pure rules (coalesce, parseWrite) have their own tests; these are
   the parts that only exist while a page is mounted: the retry chain, what a
   dead page still does, and the replay of a mirror an older build wrote. */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const anchor: InstanceAnchor = {
  dataset_id: "ds",
  camera_id: "cam1",
  frame_file: "f0.jpg",
  bbox: [10, 20, 110, 220],
  ordinal: 0,
  ts: null,
  frame_sig: null,
};

function answer(key: string): LabelWrite {
  return {
    kind: "answer",
    req: {
      instance_key: key,
      anchor,
      answers: { sun: "sun.sun" },
      taxonomy_revision: 1,
      serve_event_id: null,
      queue_reason: null,
      session_id: "s",
      client_elapsed_ms: null,
      input_mode: "key",
    },
  };
}

let posts: string[] = [];
let failPosts = false;
let beacons = 0;

beforeEach(() => {
  posts = [];
  failPosts = false;
  beacons = 0;
  sessionStorage.clear();
  vi.stubGlobal("fetch", async (_url: string, init?: { body?: string }) => {
    const body = init?.body ? JSON.parse(init.body) : {};
    posts.push(body.instance_key);
    const status = failPosts ? 500 : 200;
    return {
      ok: !failPosts,
      status,
      statusText: failPosts ? "Server Error" : "OK",
      json: async () => (failPosts ? { detail: "down" } : { ok: true, annotation_id: 1, version: 1 }),
      text: async () => (failPosts ? '{"detail":"down"}' : '{"ok":true}'),
      headers: new Map(),
    };
  });
  Object.defineProperty(navigator, "sendBeacon", {
    configurable: true,
    value: () => {
      beacons += 1;
      return true;
    },
  });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/** Mounts the hook for `owner` and hands back its live queue. */
async function mount(owner: string): Promise<{ q: () => AnswerQueue; root: Root }> {
  let current: AnswerQueue | null = null;
  function Probe() {
    current = useAnswerQueue({ owner });
    return null;
  }
  const host = document.createElement("div");
  document.body.appendChild(host);
  const root = createRoot(host);
  await act(async () => {
    root.render(createElement(Probe));
  });
  await settle();
  return { q: () => current!, root };
}

/** Lets the fetch stub's promises and the flusher's awaits run. It fires no
    retry by itself: under fake timers it runs only what is due NOW, which is
    jsdom's own zero-delay timer for the storage event of each mirror write. */
async function settle(): Promise<void> {
  await act(async () => {
    for (let i = 0; i < 25; i += 1) await Promise.resolve();
    if (vi.isFakeTimers()) vi.advanceTimersByTime(0);
  });
}

describe("useAnswerQueue retries", () => {
  it("keeps one retry chain while writes arrive during a backoff", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    failPosts = true;
    const { q, root } = await mount("alice");
    for (const key of ["A", "B", "C"]) {
      act(() => q().enqueue(answer(key)));
      await settle();
      // Each enqueue starts a flush, and each flush fails. One timer, always:
      // the old code armed a new one over the old one every time.
      expect(vi.getTimerCount()).toBe(1);
    }
    expect(posts).toEqual(["A", "A", "B", "A", "B", "C"]);
    expect(q().sync.retrying).toBe(true);
    expect(q().sync.pending).toBe(3);

    // The link comes back. One chain means one retry: three posts, not one per
    // orphaned timer.
    failPosts = false;
    posts = [];
    await act(async () => {
      await vi.advanceTimersByTimeAsync(15_000);
    });
    await settle();
    expect(posts).toEqual(["A", "B", "C"]);
    expect(q().sync.pending).toBe(0);
    expect(vi.getTimerCount()).toBe(0);
    await act(async () => root.unmount());
  });

  it("posts nothing after the page unmounts; the beacon carries what is pending", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    failPosts = true;
    const { q, root } = await mount("alice");
    act(() => q().enqueue(answer("A")));
    await settle();
    act(() => q().enqueue(answer("B")));
    await settle();
    const before = posts.length;

    await act(async () => root.unmount());
    expect(beacons).toBe(2);
    failPosts = false;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10 * 60_000);
    });
    await settle();
    expect(posts.length).toBe(before);
    // The mirror stays for the next mount, which replays it.
    expect(JSON.parse(sessionStorage.getItem(storeKeyFor("alice"))!)).toHaveLength(2);
  });
});

describe("useAnswerQueue mirror", () => {
  it("replays an older build's mirror once, under the account now signed in", async () => {
    // The bare key is what the build before this one wrote, for every account.
    sessionStorage.setItem("cownting.label.pending", JSON.stringify([answer("OLD")]));
    // Another labeler's writes on the same tab are theirs alone.
    sessionStorage.setItem(storeKeyFor("bob"), JSON.stringify([answer("BOB")]));

    const { q, root } = await mount("alice");
    expect(posts).toEqual(["OLD"]);
    expect(q().sync.pending).toBe(0);
    expect(sessionStorage.getItem("cownting.label.pending")).toBeNull();
    expect(sessionStorage.getItem(storeKeyFor("alice"))).toBeNull();
    expect(JSON.parse(sessionStorage.getItem(storeKeyFor("bob"))!)).toHaveLength(1);
    await act(async () => root.unmount());

    // Taken once: a second mount has nothing of the old key left to post.
    posts = [];
    const again = await mount("alice");
    expect(posts).toEqual([]);
    await act(async () => again.root.unmount());
  });

  it("keeps a failed write in this account's mirror for the next mount", async () => {
    failPosts = true;
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    const first = await mount("alice");
    act(() => first.q().enqueue(answer("A")));
    await settle();
    await act(async () => first.root.unmount());

    vi.useRealTimers();
    failPosts = false;
    posts = [];
    const second = await mount("alice");
    expect(posts).toEqual(["A"]);
    expect(second.q().sync.pending).toBe(0);
    await act(async () => second.root.unmount());
  });
});
