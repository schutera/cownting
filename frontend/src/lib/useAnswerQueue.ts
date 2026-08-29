import { useCallback, useEffect, useRef, useState } from "react";
import type {
  InstanceAnchor,
  LabelFlagReq,
  LabelInputMode,
  LabelMaskFixReq,
  LabelSkipReason,
  LabelSubmitReq,
  MaskFixKind,
  MaskSeed,
} from "./types";
import { flagLabel, submitLabel, submitMaskFix, TaxonomyStaleError } from "./api";

/* The Label screen's write path (M3 labeling UX §3.5), plus the two per-decision
 * events of §6.1.
 *
 * WHY THIS EXISTS AT ALL. The old page awaited the POST inside the keystroke
 * handler: `doSubmit` set `busy`, `applyAnswer` early-returned on `busy`, and a
 * keypress during the flush was dropped in TOTAL SILENCE. The annotator's reflex
 * is to press again — and the second press lands on the NEXT cow, which is a
 * wrong label written against an animal nobody looked at. Nothing on the answer
 * path may await the network, so answering mutates memory and enqueues a write
 * here; the keyboard is never gated, and the sync state is SURFACED (§5.5)
 * instead of being waited on.
 *
 * DURABILITY, because "optimistic" must not mean "lossy":
 *   - the queue is mirrored to sessionStorage on every mutation and replayed on
 *     mount, so a crash or a reload loses nothing. sessionStorage rather than
 *     localStorage: this is un-flushed work, not a preference (decision 4 leaves
 *     the route with no preferences at all), and it must not outlive the tab.
 *     A replayed write that actually landed before the reload appends a second
 *     version of the same answer, which is harmless — the store is append-only
 *     and versions supersede rather than overwrite.
 *   - `beforeunload` with a non-empty queue preventDefaults AND fires a
 *     sendBeacon per outstanding write; the same beacon runs on unmount, which
 *     `beforeunload` never sees (a route change inside the SPA).
 *   - retries back off 250ms / 1s / 4s / 15s and then stay at 15s INDEFINITELY.
 *     There is no give-up state, because the alternative is a silently discarded
 *     answer.
 *
 * COALESCING BY INSTANCE KEY. A second write for an instance whose first write
 * has not left the client replaces it: the annotator corrected a draft that the
 * server never saw, and posting both would mint two versions for one decision.
 * A correction to an instance already flushed is a genuine version 2 and is
 * enqueued normally — annotator variability is the product of this feature.
 *
 * A TaxonomyStaleError IS NOT A NETWORK FAILURE and never enters the retry loop
 * (api.ts says the same). The write is dropped, `dropped` is incremented and
 * `onStale` fires so the page can refetch and say so out loud. The instance was
 * never answered as far as the server is concerned, so the queue will serve it
 * again — the answer is lost, the work is not, and the annotator is told.
 */

// Matches AnnotationCfg.batch_size (§3.5). There is no batch write route, so a
// "batch" is up to eight concurrent posts — the point of the number is to cap
// how much a reconnect fires at the server at once, not to build one body.
const BATCH_MAX = 8;
const BACKOFF_MS: readonly number[] = [250, 1000, 4000, 15000];
const STORE_KEY = "cownting.label.pending";

// The two write routes, needed literally for the unload beacon: fetch() is
// cancelled when the document goes away, and sendBeacon is the only transport
// that survives it. flagLabel() sends `note` alongside `explanation` while the
// backend rename is in flight, so the beacon mirrors that. Both are now usually
// absent — the explanation is optional and this page stops collecting one — and
// that is fine on the wire: an undefined field is simply not serialised, and the
// route treats a missing note as NULL.
const SUBMIT_URL = "/api/label/submit";
const FLAG_URL = "/api/label/skip";
const MASK_URL = "/api/label/mask-fix";

export type LabelWrite =
  | { kind: "answer"; req: LabelSubmitReq }
  | { kind: "flag"; req: LabelFlagReq }
  // An outline correction or a false-positive verdict. It rides the same queue
  // as the answers rather than posting inline, so a mask fix survives a reload
  // and a flaky link exactly as an answer does — but see `enqueue`: it must NOT
  // coalesce against the answer for the same instance, because the two are
  // different judgements about the same cow and both have to land.
  | { kind: "mask"; req: LabelMaskFixReq };

/** What the footer's sync indicator draws (§5.5): shape and motion, never hue.
    `pending` 0 = filled dot, >0 = hollow dot with the count, `retrying` = the
    1.2s pulse. `dropped` is the only state that needs words, because a dropped
    write is an answer the annotator has to be told they must re-give. */
export interface AnswerQueueSync {
  pending: number;
  retrying: boolean;
  dropped: number;
  lastError: string | null;
}

export interface UseAnswerQueueOpts {
  /** The taxonomy moved under an un-flushed write. The page refetches it and
      surfaces the notice; this hook never retries such a write. */
  onStale?: () => void;
  /** A write landed. The page uses it to refresh the pool stats without polling.
      `result` is the route's parsed response, typed `unknown` because the three
      write kinds return different shapes and this hook has no business knowing
      which — the page narrows it by `write.kind`. It is what lets an outline
      correction be reflected on the item immediately rather than at the next
      queue fetch. Absent when the write landed via the unload beacon, which by
      construction has no response to read. */
  onWritten?: (write: LabelWrite, result?: unknown) => void;
}

export interface AnswerQueue {
  enqueue: (write: LabelWrite) => void;
  /** Kick the flusher now, ignoring the current backoff — the `online` event and
      an explicit "try now" both mean the reason for the wait is gone. */
  flushNow: () => void;
  sync: AnswerQueueSync;
}

/* ------------------------------------------------------------------ parsing
   The sessionStorage mirror is untrusted input: a half-written record, a stale
   shape from an older build, or a hand-edited value would otherwise be posted
   and 400 forever. Every field is checked and the object is REBUILT, which is
   also why there is no `as` anywhere here — a cast would let exactly the
   malformed record we are guarding against through the type system. */

function isRec(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function parseBox(v: unknown): [number, number, number, number] | null {
  if (!Array.isArray(v) || v.length !== 4) return null;
  const [a, b, c, d] = v;
  if (typeof a !== "number" || typeof b !== "number") return null;
  if (typeof c !== "number" || typeof d !== "number") return null;
  return [a, b, c, d];
}

function parseAnchor(v: unknown): InstanceAnchor | null {
  if (!isRec(v)) return null;
  const bbox = parseBox(v.bbox);
  if (bbox === null) return null;
  if (typeof v.camera_id !== "string" || typeof v.frame_file !== "string") return null;
  if (typeof v.ordinal !== "number") return null;
  return {
    dataset_id: typeof v.dataset_id === "string" ? v.dataset_id : null,
    camera_id: v.camera_id,
    frame_file: v.frame_file,
    bbox,
    ordinal: v.ordinal,
    ts: typeof v.ts === "string" ? v.ts : null,
    frame_sig: typeof v.frame_sig === "string" ? v.frame_sig : null,
  };
}

function parseAnswers(v: unknown): Record<string, string | string[]> | null {
  if (!isRec(v)) return null;
  const out: Record<string, string | string[]> = {};
  for (const [k, val] of Object.entries(v)) {
    if (typeof val === "string") {
      out[k] = val;
    } else if (Array.isArray(val) && val.every((x): x is string => typeof x === "string")) {
      out[k] = val;
    } else {
      return null;
    }
  }
  return out;
}

function parseMode(v: unknown): LabelInputMode | null {
  return v === "key" || v === "mouse" ? v : null;
}

// KEYED BY THE UNION, not listed as an array. `readonly LabelSkipReason[]` makes
// an INCOMPLETE list type-legal, and that is exactly what happened:
// `low_resolution` was added to the union, to the server vocabulary and to the
// flag row in one commit, and silently missed here — so a "Resolution too low"
// flag replayed out of sessionStorage was dropped on the floor. A Record over the
// union turns the next omission into a compile error instead of a lost write.
const FLAG_REASON_SET: Record<LabelSkipReason, true> = {
  bad_crop: true,
  no_cow: true,
  multiple_cows: true,
  occluded: true,
  low_resolution: true,
  other: true,
};

function parseReason(v: unknown): LabelSkipReason | null {
  return typeof v === "string" && Object.prototype.hasOwnProperty.call(FLAG_REASON_SET, v)
    ? (v as LabelSkipReason)
    : null;
}

function optNum(v: unknown): number | null {
  return typeof v === "number" ? v : null;
}

function parsePolygon(v: unknown): [number, number][] | null {
  if (!Array.isArray(v) || v.length < 3) return null;
  const out: [number, number][] = [];
  for (const pt of v) {
    if (!Array.isArray(pt) || pt.length !== 2) return null;
    const [x, y] = pt;
    if (typeof x !== "number" || typeof y !== "number") return null;
    if (!Number.isFinite(x) || !Number.isFinite(y)) return null;
    out.push([x, y]);
  }
  return out;
}

function parseMaskKind(v: unknown): MaskFixKind | null {
  return v === "polygon" || v === "false_positive" || v === "ok" ? v : null;
}

function parseSeed(v: unknown): MaskSeed | null {
  return v === "model" || v === "edit" || v === "bbox" ? v : null;
}

function parseWrite(v: unknown): LabelWrite | null {
  if (!isRec(v) || !isRec(v.req)) return null;
  const req = v.req;
  if (typeof req.instance_key !== "string" || req.instance_key === "") return null;
  const anchor = parseAnchor(req.anchor);
  if (anchor === null) return null;
  const session_id = typeof req.session_id === "string" ? req.session_id : null;
  if (v.kind === "answer") {
    const answers = parseAnswers(req.answers);
    if (answers === null || Object.keys(answers).length === 0) return null;
    if (typeof req.taxonomy_revision !== "number") return null;
    return {
      kind: "answer",
      req: {
        instance_key: req.instance_key,
        anchor,
        answers,
        taxonomy_revision: req.taxonomy_revision,
        serve_event_id: optNum(req.serve_event_id),
        session_id,
        client_elapsed_ms: optNum(req.client_elapsed_ms),
        input_mode: parseMode(req.input_mode),
      },
    };
  }
  if (v.kind === "flag") {
    const reason = parseReason(req.reason);
    // The REASON is the flag. This used to also demand a non-empty explanation,
    // matching a server-side 400 that no longer exists — so once the written
    // justification became optional, EVERY flag this page enqueues failed to
    // parse on replay and was discarded in silence: no counter, no error, and a
    // sync dot reading zero pending. That is the precise failure the offline
    // queue exists to prevent, so the rule is now "a reason, and nothing else
    // required".
    if (reason === null) return null;
    const explanation =
      typeof req.explanation === "string" && req.explanation.trim() !== ""
        ? req.explanation
        : undefined;
    return {
      kind: "flag",
      req: {
        instance_key: req.instance_key,
        anchor,
        reason,
        explanation,
        serve_event_id: optNum(req.serve_event_id),
        session_id,
        client_elapsed_ms: optNum(req.client_elapsed_ms),
      },
    };
  }
  if (v.kind === "mask") {
    const kind = parseMaskKind(req.kind);
    const seeded_from = parseSeed(req.seeded_from);
    if (kind === null || seeded_from === null) return null;
    // The polygon is required for a correction and forbidden for a removal —
    // the server enforces both, and a replayed record that disagrees would post
    // forever against a 400.
    const polygon = kind === "polygon" ? parsePolygon(req.polygon) : null;
    if (kind === "polygon" && polygon === null) return null;
    if (req.space !== undefined && req.space !== "frame" && req.space !== "crop") return null;
    return {
      kind: "mask",
      req: {
        instance_key: req.instance_key,
        anchor,
        kind,
        // WITHOUT THIS the replay is stored in the wrong coordinate system. The
        // editor sends full-frame px (`space: "frame"`); the route defaults to
        // "crop" for the pre-zoom contract. A rebuilt record that dropped the
        // field would hand 4K coordinates to the crop-local branch — rejected
        // forever on a large cow (the queue has no give-up state), or, on an
        // animal near the frame origin, quietly passed through crop_to_frame and
        // stored SHEARED. Which is exactly the class of corruption this
        // field-by-field rebuild exists to prevent.
        space: req.space === "frame" ? "frame" : "crop",
        polygon,
        mask_rev: typeof req.mask_rev === "string" ? req.mask_rev : null,
        seeded_from,
        serve_event_id: optNum(req.serve_event_id),
        session_id,
        client_elapsed_ms: optNum(req.client_elapsed_ms),
      },
    };
  }
  return null;
}

function readStore(): LabelWrite[] {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    if (raw === null) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    const out: LabelWrite[] = [];
    for (const row of parsed) {
      const w = parseWrite(row);
      if (w !== null) out.push(w);
    }
    return out;
  } catch {
    // Private mode, a quota refusal, or corrupt JSON. The queue still works for
    // this page-load; only crash recovery is lost, and that is not worth an
    // error state on a screen whose whole job is not to interrupt.
    return [];
  }
}

function writeStore(queue: readonly LabelWrite[]): void {
  try {
    if (queue.length === 0) sessionStorage.removeItem(STORE_KEY);
    else sessionStorage.setItem(STORE_KEY, JSON.stringify(queue));
  } catch {
    /* see readStore */
  }
}

function beacon(write: LabelWrite): void {
  if (typeof navigator.sendBeacon !== "function") return;
  const url =
    write.kind === "answer" ? SUBMIT_URL : write.kind === "mask" ? MASK_URL : FLAG_URL;
  const body =
    write.kind === "flag" ? { ...write.req, note: write.req.explanation } : write.req;
  try {
    navigator.sendBeacon(url, new Blob([JSON.stringify(body)], { type: "application/json" }));
  } catch {
    /* the document is going away; there is nowhere to report this */
  }
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function useAnswerQueue(opts: UseAnswerQueueOpts = {}): AnswerQueue {
  // The queue lives in a ref, not in state: the flusher and the keystroke path
  // both mutate it, and a stale snapshot here would either re-post a landed
  // write or drop a fresh one. `sync` is the render-visible mirror.
  const queueRef = useRef<LabelWrite[]>([]);
  const [sync, setSync] = useState<AnswerQueueSync>({
    pending: 0,
    retrying: false,
    dropped: 0,
    lastError: null,
  });

  const flushingRef = useRef(false);
  const attemptRef = useRef(0);
  const timerRef = useRef<number | null>(null);
  // The callbacks are re-read through refs so a page that rebuilds them every
  // render does not re-arm the unload listener or restart the flusher.
  const optsRef = useRef(opts);
  useEffect(() => {
    optsRef.current = opts;
  });

  const publish = useCallback((patch: Partial<AnswerQueueSync>) => {
    setSync((prev) => ({ ...prev, pending: queueRef.current.length, ...patch }));
  }, []);

  const flush = useCallback(
    async function flush(): Promise<void> {
      if (flushingRef.current) return;
      if (queueRef.current.length === 0) return;
      flushingRef.current = true;
      try {
        while (queueRef.current.length > 0) {
          const batch = queueRef.current.slice(0, BATCH_MAX);
          const results = await Promise.allSettled(
            batch.map((w) => {
              if (w.kind === "answer") return submitLabel(w.req);
              if (w.kind === "mask") return submitMaskFix(w.req);
              return flagLabel(w.req);
            }),
          );
          // Entries are removed by IDENTITY, never by index: the annotator can
          // enqueue a correction while this batch is in flight, which coalescing
          // may have swapped into the same slot.
          const settled = new Set<LabelWrite>();
          let staleCount = 0;
          // A plain loop rather than forEach: `failure` is read after this block,
          // and TypeScript does not track assignments made inside a callback —
          // it would narrow the variable to `null` and quietly make the retry
          // branch unreachable to the checker.
          let failure: string | null = null;
          for (let i = 0; i < results.length; i += 1) {
            const res = results[i];
            const write = batch[i];
            if (res.status === "fulfilled") {
              settled.add(write);
              optsRef.current.onWritten?.(write, res.value);
            } else if (res.reason instanceof TaxonomyStaleError) {
              settled.add(write);
              staleCount += 1;
            } else {
              failure = errMsg(res.reason);
            }
          }
          if (settled.size > 0) {
            queueRef.current = queueRef.current.filter((w) => !settled.has(w));
            writeStore(queueRef.current);
          }
          if (staleCount > 0) {
            setSync((prev) => ({ ...prev, dropped: prev.dropped + staleCount }));
            optsRef.current.onStale?.();
          }
          if (failure !== null) {
            // Something in this batch did not land. Back off and retry the whole
            // remaining queue; nothing is ever discarded for a transport error.
            const wait = BACKOFF_MS[Math.min(attemptRef.current, BACKOFF_MS.length - 1)];
            attemptRef.current += 1;
            publish({ retrying: true, lastError: failure });
            timerRef.current = window.setTimeout(() => {
              timerRef.current = null;
              void flush();
            }, wait);
            return;
          }
          attemptRef.current = 0;
          publish({ retrying: false, lastError: null });
        }
      } finally {
        flushingRef.current = false;
      }
    },
    [publish],
  );

  const flushNow = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    attemptRef.current = 0;
    void flush();
  }, [flush]);

  const enqueue = useCallback(
    (write: LabelWrite) => {
      const key = write.req.instance_key;
      // Coalesce WITHIN a kind, never across kinds. Two writes of the same kind
      // for one instance are a corrected draft the server never saw. Two writes
      // of DIFFERENT kinds are two different judgements about the same cow — an
      // outline correction and the answers to its questions both have to land,
      // and the old key-only filter would have silently thrown one away.
      const kept = queueRef.current.filter(
        (w) => w.req.instance_key !== key || w.kind !== write.kind,
      );
      queueRef.current = [...kept, write];
      writeStore(queueRef.current);
      publish({});
      void flush();
    },
    [flush, publish],
  );

  // Replay whatever the previous page-load could not deliver, once.
  const replayedRef = useRef(false);
  useEffect(() => {
    if (replayedRef.current) return;
    replayedRef.current = true;
    const stored = readStore();
    if (stored.length === 0) return;
    queueRef.current = [...stored, ...queueRef.current];
    publish({});
    void flush();
  }, [flush, publish]);

  // A reconnect is the one event that means the reason for the backoff is gone.
  useEffect(() => {
    const onOnline = () => flushNow();
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, [flushNow]);

  // Leaving with work in hand: warn, and try the beacon anyway — the warning is
  // advisory (the browser may show nothing) while the beacon actually delivers.
  useEffect(() => {
    const onUnload = (e: BeforeUnloadEvent) => {
      if (queueRef.current.length === 0) return;
      for (const w of queueRef.current) beacon(w);
      e.preventDefault();
    };
    window.addEventListener("beforeunload", onUnload);
    return () => {
      window.removeEventListener("beforeunload", onUnload);
      // Navigating away inside the SPA never fires beforeunload, and the fetches
      // in flight are abandoned with the page. The mirror survives for the next
      // mount, and the beacon covers what would otherwise wait for it.
      for (const w of queueRef.current) beacon(w);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  return { enqueue, flushNow, sync };
}

/* ------------------------------------------------- per-decision telemetry §6.1
 * `presented` and `answered` are what make the redesign measurable: today a
 * mis-key and a considered answer are byte-identical in the store, the batch
 * `served` row carries items 1..k-1 of the batch as well as its own, and
 * `client_elapsed_ms` is ONE number spanning both questions — so none of A1, A2,
 * A4, A5 or A8 in §8.3 can be computed. These two events are the fix.
 *
 * They are posted here rather than through api.ts's `postLabelEvent` because
 * `LabelEventKind` mirrors the server's stored `EVENT_KINDS`, which does not
 * carry these two yet (§8.6 lands them first, server-side), and `LabelEventReq`
 * has no `detail` field to hang the timings on. Casting a wider kind through
 * that typed helper would be a lie about what the server accepts; a separate,
 * honestly-typed poster is not.
 *
 * If the route refuses the kind (400, i.e. this build is ahead of the backend)
 * the emitter switches itself off for the rest of the page-load rather than
 * firing two rejected requests per instance forever. A transport error does NOT
 * switch it off — that is a reconnect away from working.
 */

export type DecisionEventKind = "presented" | "answered";

export interface DecisionEventDetail {
  [k: string]: string | number | boolean | null;
}

export interface DecisionEvent {
  session_id: string;
  kind: DecisionEventKind;
  instance_key: string;
  /** The class chosen, or null when the press CLEARED an answer (§3.3's
      press-again-to-clear) — which still counts as a within-item correction. */
  class_key?: string | null;
  detail?: DecisionEventDetail;
}

let decisionEventsAccepted = true;

export function emitDecisionEvent(ev: DecisionEvent): void {
  if (!decisionEventsAccepted) return;
  void fetch("/api/label/events", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(ev),
    // The last `answered` of a session is emitted microseconds before the page
    // may be torn down; keepalive is what gets it out of the door.
    keepalive: true,
  })
    .then((res) => {
      if (res.status === 400 || res.status === 422) decisionEventsAccepted = false;
    })
    .catch(() => {
      /* fire-and-forget: a dropped event must never interrupt labeling */
    });
}
