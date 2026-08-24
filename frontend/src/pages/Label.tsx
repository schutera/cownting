import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { canManageData, useAuth } from "../lib/auth";
import type {
  InstanceAnchor,
  LabelGroup,
  LabelInputMode,
  LabelItem,
  LabelSkipReason,
  LabelSkipReq,
  LabelStats,
  LabelSubmitReq,
  Taxonomy,
} from "../lib/types";
import {
  getLabelProgress,
  getLabelQueue,
  getTaxonomy,
  postLabelEvent,
  skipLabel,
  submitLabel,
  TaxonomyStaleError,
  undoLabel,
} from "../lib/api";
import type { LabelKeyMap } from "../lib/labelKeys";
import {
  actionForEvent,
  buildKeyMap,
  groupKeyHint,
  isTypingTarget,
  LABEL_ACTIONS,
  optionForEvent,
} from "../lib/labelKeys";
import { InstanceCrop } from "../components/InstanceCrop";
import { LabelGroupList } from "../components/LabelGroup";
import { LabelProgress } from "../components/LabelProgress";
import { Button, Card, Kbd, SectionLabel } from "../components/ui";

/* The labeling screen (docs/roadmap/M3_labeling.md §5). This page owns every
 * keystroke, the prefetch buffer, submit/skip/flag/undo, the served-event
 * plumbing (`serve_event_id` + `client_elapsed_ms` echoed on every write), and
 * the five DISTINCT terminal states of §5.6 — "nothing to label" has different
 * causes with different next actions, so they are never collapsed into one.
 *
 * The queue is self-consuming and idempotent (no leases, §4.2), so the buffer
 * here is purely a latency device: a batch is held client-side, upcoming crops
 * are decoded ahead via `new Image()`, and a refetch with `exclude` always
 * advances. Undo pushes the item back to the HEAD of this buffer with its
 * previous selections re-applied — U means "fix the one option I fumbled",
 * not "answer both questions again".
 */

const DEFS_KEY = "cownting.label.defs";
const AUTOSUBMIT_KEY = "cownting.label.autosubmit";
/* Refetch when the buffer shrinks to this many items — early enough that the
   next batch lands before the annotator drains what is left. */
const REFILL_AT = 3;
/* Crops decoded ahead of the current one. The server's ETag + max-age makes the
   later real <img> load a cache hit, so this is one request per item, not two. */
const PREFETCH_AHEAD = 3;
/* Keys we just wrote, still excluded from refetches: our own submit may not be
   visible to the queue scan yet, and being served an item we answered seconds
   ago reads as a bug even when it is only a race. */
const RECENT_CAP = 100;
/* Undo depth. The server can supersede any of our rows by key, so this is only
   how far back the client can re-present an item with its selections intact. */
const UNDO_CAP = 50;

/* Wording for the frozen skip reasons (types.ts LabelSkipReason). The digit is
   the in-overlay hotkey; the blurb is why the reason exists. */
const SKIP_REASONS: { reason: LabelSkipReason; label: string; blurb: string }[] = [
  { reason: "bad_crop", label: "Bad crop", blurb: "the ring doesn't frame a judgeable animal" },
  { reason: "no_cow", label: "No cow", blurb: "there is no animal in the ring at all" },
  { reason: "multiple_cows", label: "Multiple cows", blurb: "two or more animals share the ring" },
  { reason: "occluded", label: "Occluded", blurb: "a panel or another cow hides too much of it" },
  { reason: "other", label: "Other", blurb: "something else is wrong with this one" },
];

/* Mirrors AnnotationCfg.max_note_chars (config §3.6): the server truncates
   anyway; matching it here just keeps the annotator from typing past the cap. */
const MAX_NOTE_CHARS = 500;

type OverlayKind = "help" | "skip" | "flag";

/* A failed submit or skip, held so the `online` event (and a manual "Try again")
   can re-send the SAME request. The item never advances until it lands. */
type PendingWrite =
  | { kind: "submit"; req: LabelSubmitReq; item: LabelItem }
  | { kind: "skip"; req: LabelSkipReq; item: LabelItem };

interface UndoEntry {
  item: LabelItem;
  answers: Record<string, string | string[]>;
}

/* The anchor echoed back verbatim from the queue item — the server re-hashes it
   (labeling.verify_anchor) and 400s a mismatch. `ts` is null because the item
   deliberately never carries the clock time (§5.3) and ts is not hashed (§2.2). */
function anchorOf(item: LabelItem): InstanceAnchor {
  return {
    dataset_id: item.dataset_id,
    camera_id: item.camera_id,
    frame_file: item.frame_file,
    bbox: item.bbox,
    ordinal: item.ordinal,
    ts: null,
    frame_sig: item.frame_sig,
  };
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/* label_events.session_id is documented as uuid4 hex (32 chars, no dashes).
   Built from getRandomValues, NOT crypto.randomUUID: randomUUID is
   [SecureContext]-only and this dashboard is reachable over plain LAN http,
   where it would throw on mount. getRandomValues is what Admin.tsx's credential
   proposer already leans on, so it is a known-good primitive here. */
function newSessionId(): string {
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  buf[6] = (buf[6] & 0x0f) | 0x40; // version 4
  buf[8] = (buf[8] & 0x3f) | 0x80; // RFC 4122 variant
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

function loadDefs(): Set<string> {
  try {
    const raw = localStorage.getItem(DEFS_KEY);
    if (raw === null) return new Set();
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return new Set();
    return new Set(parsed.filter((v): v is string => typeof v === "string"));
  } catch {
    return new Set();
  }
}

/* Drop unanswered groups from the submit body: an empty string or empty list is
   "no answer", not an answer, and the server 400s a submit with zero choices. */
function pruneAnswers(
  answers: Record<string, string | string[]>,
): Record<string, string | string[]> {
  const out: Record<string, string | string[]> = {};
  for (const [k, v] of Object.entries(answers)) {
    if (Array.isArray(v)) {
      if (v.length > 0) out[k] = v;
    } else if (v !== "") {
      out[k] = v;
    }
  }
  return out;
}

export default function Label() {
  const { user } = useAuth();
  const canManage = canManageData(user);

  // One labeling session per page mount; brackets the session_start/end events.
  const [sessionId] = useState(newSessionId);

  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null);
  const [stats, setStats] = useState<LabelStats | null>(null);

  // The prefetch buffer. `bufferRef` mirrors the state so the async paths and
  // the (stable, ref-delegated) hotkey handler never act on a stale snapshot.
  const [buffer, setBuffer] = useState<LabelItem[]>([]);
  const bufferRef = useRef<LabelItem[]>([]);
  const setBufferState = useCallback((next: LabelItem[]) => {
    bufferRef.current = next;
    setBuffer(next);
  }, []);
  const [fetching, setFetching] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  // The last refetch produced nothing new — stop polling until a write changes
  // the pool or the annotator asks to check again.
  const [queueDrained, setQueueDrained] = useState(false);
  const fetchingRef = useRef(false);
  const recentRef = useRef<string[]>([]);

  const [answers, setAnswers] = useState<Record<string, string | string[]>>({});
  const [busy, setBusy] = useState(false);
  const [nudge, setNudge] = useState<string | null>(null);
  const [staleNotice, setStaleNotice] = useState<string | null>(null);
  const [writeError, setWriteError] = useState<string | null>(null);
  const [pending, setPending] = useState<PendingWrite | null>(null);
  const [overlay, setOverlay] = useState<OverlayKind | null>(null);
  const [hideRing, setHideRing] = useState(false);
  const [failedKey, setFailedKey] = useState<string | null>(null);

  const [openDefs, setOpenDefs] = useState<ReadonlySet<string>>(loadDefs);
  const [autoSubmit, setAutoSubmitState] = useState<boolean>(() => {
    try {
      return localStorage.getItem(AUTOSUBMIT_KEY) === "1";
    } catch {
      return false;
    }
  });

  const undoStackRef = useRef<UndoEntry[]>([]);
  // The modality that ANSWERED (not the one that saved): set by every answer
  // action, reported as input_mode, reset per item.
  const lastModeRef = useRef<LabelInputMode | null>(null);
  const prefetchedRef = useRef<Set<string>>(new Set());

  // Per-item ACTIVE clock for client_elapsed_ms — the tab-away detector. Only
  // visible time counts: banked on hide, resumed on show, reset per item.
  const clockAccumRef = useRef(0);
  const clockVisibleSinceRef = useRef<number | null>(null);
  const activeElapsedMs = useCallback((): number => {
    const since = clockVisibleSinceRef.current;
    const live = since === null ? 0 : performance.now() - since;
    return Math.max(0, Math.round(clockAccumRef.current + live));
  }, []);

  const groups = useMemo(() => taxonomy?.groups ?? [], [taxonomy]);
  const keys = useMemo(() => buildKeyMap(groups), [groups]);
  const groupByKey = useMemo(() => new Map(groups.map((g) => [g.group_key, g])), [groups]);
  // "Taxonomy empty" means no active group has an answerable (active) class.
  const taxonomyUsable = keys.groups.some((g) => g.options.length > 0);

  const current: LabelItem | null = buffer.length > 0 ? buffer[0] : null;
  const currentKey = current?.instance_key ?? null;
  const cropFailed = currentKey !== null && failedKey === currentKey;

  // ------------------------------------------------------------- preferences

  const updateDefs = useCallback((next: Set<string>) => {
    setOpenDefs(next);
    try {
      localStorage.setItem(DEFS_KEY, JSON.stringify([...next]));
    } catch {
      /* private mode etc. — the toggle still works for this session */
    }
  }, []);

  const setAutoSubmit = useCallback((on: boolean) => {
    setAutoSubmitState(on);
    try {
      localStorage.setItem(AUTOSUBMIT_KEY, on ? "1" : "0");
    } catch {
      /* ditto */
    }
  }, []);

  function allDefKeys(): Set<string> {
    const all = new Set<string>();
    for (const g of groups) {
      if (!g.active) continue;
      if (g.description !== null && g.description !== "") all.add(g.group_key);
      for (const c of g.classes) if (c.active) all.add(c.class_key);
    }
    return all;
  }

  function setAllDefs(on: boolean) {
    updateDefs(on ? allDefKeys() : new Set());
  }

  function toggleAllDefs() {
    setAllDefs(openDefs.size === 0);
  }

  function onToggleDef(key: string) {
    const next = new Set(openDefs);
    if (next.has(key)) {
      next.delete(key);
    } else {
      next.add(key);
      // A deliberate single open is the "this definition is ambiguous" signal
      // (SQL_INFO_ICON_PRESSURE); the mass `I` toggle deliberately sends nothing.
      postLabelEvent({
        session_id: sessionId,
        kind: "info_opened",
        instance_key: bufferRef.current[0]?.instance_key ?? null,
        class_key: key,
      }).catch(() => {});
    }
    updateDefs(next);
  }

  // ------------------------------------------------------------ data fetching

  const refreshStats = useCallback(() => {
    getLabelProgress()
      .then(setStats)
      .catch(() => {
        /* the panel keeps its last numbers; stats are never worth an error state */
      });
  }, []);

  const refill = useCallback(async () => {
    if (fetchingRef.current) return;
    fetchingRef.current = true;
    setFetching(true);
    try {
      const have = bufferRef.current.map((i) => i.instance_key);
      // Buffer keys first: api.ts slices exclude to the server's 200 cap, and
      // losing a recent key to the cap is survivable, losing a buffered one
      // would serve us a duplicate of what is already on screen.
      const q = await getLabelQueue({ exclude: [...have, ...recentRef.current] });
      const known = new Set([...have, ...recentRef.current]);
      const fresh = q.items.filter((i) => !known.has(i.instance_key));
      if (fresh.length === 0) {
        setQueueDrained(true);
      } else {
        setBufferState([...bufferRef.current, ...fresh]);
      }
      setFetchError(null);
    } catch (e) {
      // Only fatal when there is nothing left to work on — with items still
      // buffered the annotator keeps labeling and the next write retriggers.
      if (bufferRef.current.length === 0) setFetchError(errMsg(e));
    } finally {
      fetchingRef.current = false;
      setFetching(false);
    }
  }, [setBufferState]);

  // Boot: taxonomy, stats, session bracket. StrictMode double-mounts this; the
  // extra start/end pair is harmless fire-and-forget telemetry and the GETs are
  // idempotent by design (§4.2).
  useEffect(() => {
    let cancelled = false;
    getTaxonomy()
      .then((t) => {
        if (!cancelled) {
          setTaxonomy(t);
          setTaxonomyError(null);
        }
      })
      .catch((e: unknown) => {
        if (!cancelled) setTaxonomyError(errMsg(e));
      });
    getLabelProgress()
      .then((s) => {
        if (!cancelled) setStats(s);
      })
      .catch(() => {});
    postLabelEvent({ session_id: sessionId, kind: "session_start" }).catch(() => {});
    return () => {
      cancelled = true;
      postLabelEvent({ session_id: sessionId, kind: "session_end" }).catch(() => {});
    };
  }, [sessionId]);

  // Keep the buffer topped up. Waits for the taxonomy: an item without its
  // questions is unanswerable, and the queue GET writes served events we would
  // waste (§4.2 — every serve is an abandonment candidate until answered).
  useEffect(() => {
    if (taxonomy === null || queueDrained) return;
    if (buffer.length > REFILL_AT) return;
    void refill();
  }, [taxonomy, buffer.length, queueDrained, refill]);

  // Decode upcoming crops off-screen so advancing swaps pixels instantly.
  useEffect(() => {
    for (const it of buffer.slice(1, 1 + PREFETCH_AHEAD)) {
      if (prefetchedRef.current.has(it.crop_url)) continue;
      prefetchedRef.current.add(it.crop_url);
      const img = new Image();
      img.src = it.crop_url;
    }
  }, [buffer]);

  // Reset the active clock whenever a different item takes the screen.
  useEffect(() => {
    clockAccumRef.current = 0;
    clockVisibleSinceRef.current =
      document.visibilityState === "visible" ? performance.now() : null;
  }, [currentKey]);

  useEffect(() => {
    const onVis = () => {
      if (document.visibilityState === "hidden") {
        if (clockVisibleSinceRef.current !== null) {
          clockAccumRef.current += performance.now() - clockVisibleSinceRef.current;
          clockVisibleSinceRef.current = null;
        }
      } else if (clockVisibleSinceRef.current === null) {
        clockVisibleSinceRef.current = performance.now();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  // ------------------------------------------------------------ write actions

  function firstUnansweredIn(a: Record<string, string | string[]>): LabelGroup | null {
    for (const gk of keys.groups) {
      if (gk.options.length === 0) continue; // no active class — unanswerable, never blocks
      const g = groupByKey.get(gk.group_key);
      if (g === undefined || !g.required) continue;
      const v: string | string[] | undefined = a[gk.group_key];
      if (v === undefined || v === "" || (Array.isArray(v) && v.length === 0)) return g;
    }
    return null;
  }

  function afterWrite(item: LabelItem, answersUsed: Record<string, string | string[]>) {
    setPending(null);
    setWriteError(null);
    setStaleNotice(null);
    setNudge(null);
    undoStackRef.current = [
      ...undoStackRef.current.slice(-(UNDO_CAP - 1)),
      { item, answers: answersUsed },
    ];
    const recent = recentRef.current.filter((k) => k !== item.instance_key);
    recent.push(item.instance_key);
    recentRef.current = recent.slice(-RECENT_CAP);
    setBufferState(bufferRef.current.filter((i) => i.instance_key !== item.instance_key));
    setAnswers({});
    lastModeRef.current = null;
    // The pool changed under us, so a "drained" verdict is stale by definition.
    setQueueDrained(false);
    refreshStats();
  }

  async function handleStale() {
    setPending(null);
    try {
      const fresh = await getTaxonomy();
      setTaxonomy(fresh);
      // Keep whichever selections still resolve to an ACTIVE class in the new
      // taxonomy: an archived pick would be invisible in the option list, and a
      // selection you cannot see is worse than one you re-make.
      const active = new Set<string>();
      for (const g of fresh.groups) {
        if (!g.active) continue;
        for (const c of g.classes) if (c.active) active.add(c.class_key);
      }
      setAnswers((prev) => {
        const kept: Record<string, string | string[]> = {};
        for (const [gk, v] of Object.entries(prev)) {
          if (Array.isArray(v)) {
            const still = v.filter((ck) => active.has(ck));
            if (still.length > 0) kept[gk] = still;
          } else if (active.has(v)) {
            kept[gk] = v;
          }
        }
        return kept;
      });
      setStaleNotice(
        "The questions just changed — your still-valid answers were kept. Re-check them and save again.",
      );
    } catch (e) {
      setWriteError(errMsg(e));
    }
  }

  async function doSubmit(answersToSend: Record<string, string | string[]>, gesture: LabelInputMode) {
    const item: LabelItem | undefined = bufferRef.current[0];
    if (item === undefined || busy || taxonomy === null) return;
    const req: LabelSubmitReq = {
      instance_key: item.instance_key,
      anchor: anchorOf(item),
      answers: answersToSend,
      taxonomy_revision: taxonomy.revision,
      serve_event_id: item.serve_event_id,
      session_id: sessionId,
      client_elapsed_ms: activeElapsedMs(),
      input_mode: lastModeRef.current ?? gesture,
    };
    setBusy(true);
    try {
      await submitLabel(req);
      afterWrite(item, answersToSend);
    } catch (e) {
      if (e instanceof TaxonomyStaleError) {
        // Emphatically NOT a network failure: refetch, keep what resolves,
        // re-present. Never into the retry path (api.ts says the same).
        await handleStale();
      } else {
        setPending({ kind: "submit", req, item });
        setWriteError(errMsg(e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function doSkip(reason: LabelSkipReason, note: string | null) {
    const item: LabelItem | undefined = bufferRef.current[0];
    if (item === undefined || busy) return;
    setOverlay(null);
    const req: LabelSkipReq = {
      instance_key: item.instance_key,
      anchor: anchorOf(item),
      reason,
      serve_event_id: item.serve_event_id,
      session_id: sessionId,
      client_elapsed_ms: activeElapsedMs(),
      note,
    };
    setBusy(true);
    try {
      await skipLabel(req);
      afterWrite(item, {});
    } catch (e) {
      if (e instanceof TaxonomyStaleError) {
        await handleStale();
      } else {
        setPending({ kind: "skip", req, item });
        setWriteError(errMsg(e));
      }
    } finally {
      setBusy(false);
    }
  }

  async function retryPending() {
    const pw = pending;
    if (pw === null || busy) return;
    setBusy(true);
    try {
      if (pw.kind === "submit") {
        await submitLabel(pw.req);
        afterWrite(pw.item, pw.req.answers);
      } else {
        await skipLabel(pw.req);
        afterWrite(pw.item, {});
      }
    } catch (e) {
      if (e instanceof TaxonomyStaleError) {
        await handleStale();
      } else {
        setWriteError(errMsg(e)); // pending stays armed for the next attempt
      }
    } finally {
      setBusy(false);
    }
  }

  async function doUndo() {
    if (busy) return;
    const entry: UndoEntry | undefined = undoStackRef.current[undoStackRef.current.length - 1];
    if (entry === undefined) {
      setNudge("Nothing to undo yet — U rewinds your own last save.");
      return;
    }
    setBusy(true);
    try {
      await undoLabel(entry.item.instance_key);
      undoStackRef.current = undoStackRef.current.slice(0, -1);
      // Head of the buffer + the previous selections re-applied: "fix the one
      // option I fumbled", not "start over" (§5.5).
      const rest = bufferRef.current.filter((i) => i.instance_key !== entry.item.instance_key);
      setBufferState([entry.item, ...rest]);
      setAnswers(entry.answers);
      lastModeRef.current = null;
      setPending(null);
      setWriteError(null);
      setNudge(null);
      setQueueDrained(false);
      refreshStats();
    } catch (e) {
      setWriteError(errMsg(e));
    } finally {
      setBusy(false);
    }
  }

  function applyAnswer(groupKey: string, classKey: string, mode: LabelInputMode) {
    if (currentKey === null || busy || cropFailed) return;
    const group = groupByKey.get(groupKey);
    if (group === undefined) return;
    lastModeRef.current = mode;
    setNudge(null);
    let next: Record<string, string | string[]>;
    if (group.multi_select) {
      const prev = answers[groupKey];
      const arr = Array.isArray(prev) ? [...prev] : [];
      const at = arr.indexOf(classKey);
      if (at >= 0) arr.splice(at, 1);
      else arr.push(classKey);
      next = { ...answers, [groupKey]: arr };
    } else {
      next = { ...answers, [groupKey]: classKey };
    }
    setAnswers(next);
    // Auto-submit fires ONLY here, on a real answer action. Firing from an
    // effect that watches `answers` would save the selections U just restored,
    // making undo save itself straight back.
    if (autoSubmit && firstUnansweredIn(next) === null) {
      const pruned = pruneAnswers(next);
      if (Object.keys(pruned).length > 0) void doSubmit(pruned, mode);
    }
  }

  function trySave(gesture: LabelInputMode) {
    if (currentKey === null || busy) return;
    if (cropFailed) {
      setNudge("This image can't be judged — press F to flag it, or S to skip.");
      return;
    }
    const missing = firstUnansweredIn(answers);
    if (missing !== null) {
      // Never a silent no-op: a swallowed Enter is pressed twice, assumed to
      // have worked, and the annotator moves on (§5.5).
      const hint = groupKeyHint(keys, missing.group_key);
      setNudge(
        `“${missing.name}” is still unanswered — ${hint !== "" ? `press ${hint}` : "pick an option with the mouse"}.`,
      );
      return;
    }
    const pruned = pruneAnswers(answers);
    if (Object.keys(pruned).length === 0) {
      setNudge("Pick at least one answer before saving.");
      return;
    }
    void doSubmit(pruned, gesture);
  }

  // -------------------------------------------------------- global hotkeys

  // One window-level listener delegating to a ref that is rebuilt every render,
  // so the handler always sees fresh state without re-binding on each keystroke.
  const keydownRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keydownRef.current = (e: KeyboardEvent) => {
    // Escape first — it must also fire from inside the flag dialog's textarea,
    // which the typing guard below would otherwise swallow.
    if (e.key === "Escape") {
      if (overlay !== null) {
        e.preventDefault();
        setOverlay(null);
      } else if (nudge !== null) {
        setNudge(null);
      }
      return;
    }
    if (isTypingTarget(e.target)) return;

    if (overlay === "help") {
      if (actionForEvent(e) === "help") {
        e.preventDefault();
        setOverlay(null);
      }
      return;
    }
    if (overlay === "skip") {
      const at = "12345".indexOf(e.key);
      if (at >= 0 && at < SKIP_REASONS.length) {
        e.preventDefault();
        void doSkip(SKIP_REASONS[at].reason, null);
      }
      return;
    }
    if (overlay === "flag") return; // mouse + Esc; its own inputs take the keys

    const action = actionForEvent(e);
    if (action !== null) {
      switch (action) {
        case "save":
          e.preventDefault();
          if (!e.repeat) trySave("key");
          return;
        case "skip":
          e.preventDefault();
          if (currentKey !== null && !busy) setOverlay("skip");
          return;
        case "flag":
          e.preventDefault();
          if (currentKey === null || busy) return;
          if (cropFailed) {
            // The one-key flag of terminal state 3: nothing to judge, so no
            // dialog — record the refusal and move on.
            void doSkip("bad_crop", "flagged: the crop failed to load in the browser");
          } else {
            setOverlay("flag");
          }
          return;
        case "undo":
          e.preventDefault();
          if (!e.repeat) void doUndo();
          return;
        case "definitions":
          e.preventDefault();
          toggleAllDefs();
          return;
        case "hideRing":
          if (!e.repeat) setHideRing(true);
          return;
        case "clear":
          e.preventDefault(); // Backspace must never navigate
          if (currentKey !== null && !busy) {
            setAnswers({});
            setNudge(null);
          }
          return;
        case "help":
          e.preventDefault();
          setOverlay("help");
          return;
        case "close":
          return; // Escape is handled above, before the typing guard
      }
    }
    const opt = optionForEvent(keys, e);
    if (opt !== null && !e.repeat) {
      e.preventDefault();
      applyAnswer(opt.group_key, opt.class_key, "key");
    }
  };

  const keyupRef = useRef<(e: KeyboardEvent) => void>(() => {});
  keyupRef.current = (e: KeyboardEvent) => {
    if (e.key.toLowerCase() === "h") setHideRing(false);
  };

  useEffect(() => {
    const down = (e: KeyboardEvent) => keydownRef.current(e);
    const up = (e: KeyboardEvent) => keyupRef.current(e);
    // Alt-tabbing away with H held would otherwise leave the ring hidden forever.
    const onBlur = () => setHideRing(false);
    window.addEventListener("keydown", down);
    window.addEventListener("keyup", up);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", down);
      window.removeEventListener("keyup", up);
      window.removeEventListener("blur", onBlur);
    };
  }, []);

  // Terminal state 4: a submit that failed while offline retries on the
  // `online` event. A persistent 4xx is NOT in a loop — this fires once per
  // reconnect, and the only other retry is the explicit button.
  const onlineRef = useRef<() => void>(() => {});
  onlineRef.current = () => {
    if (pending !== null && !busy) void retryPending();
  };
  useEffect(() => {
    const onOnline = () => onlineRef.current();
    window.addEventListener("online", onOnline);
    return () => window.removeEventListener("online", onOnline);
  }, []);

  // ------------------------------------------------------------------ render

  const defsShown = openDefs.size > 0;

  let main: ReactNode;
  if (taxonomyError !== null) {
    main = (
      <TerminalCard title="Couldn't load the questions">
        <p className="text-sm text-gray-mid">{taxonomyError}</p>
        <div className="mt-5">
          <Button
            variant="ghost"
            onClick={() => {
              setTaxonomyError(null);
              getTaxonomy()
                .then(setTaxonomy)
                .catch((e: unknown) => setTaxonomyError(errMsg(e)));
            }}
          >
            Try again
          </Button>
        </div>
      </TerminalCard>
    );
  } else if (taxonomy === null) {
    main = <div className="animate-shimmer h-96 bg-surface border border-border rounded-2xl" />;
  } else if (!taxonomyUsable) {
    // Terminal state 5: the taxonomy has nothing to ask.
    main = (
      <TerminalCard title="There are no questions yet">
        <p className="text-sm text-gray-mid max-w-md mx-auto">
          The label taxonomy has no active classes, so there is nothing to answer about an animal
          yet.
        </p>
        {canManage ? (
          <Link
            to="/label/classes"
            className="inline-block mt-4 text-sm text-accent hover:text-accent-deep transition-colors"
          >
            Set up the questions →
          </Link>
        ) : (
          <p className="text-[13px] text-gray-tertiary mt-4">
            A poweruser has to set up the questions before labeling can start.
          </p>
        )}
      </TerminalCard>
    );
  } else if (stats !== null && stats.pool_total === 0) {
    // Terminal state 2: no footage has been processed at all.
    main = (
      <TerminalCard title="No footage to label yet">
        <p className="text-sm text-gray-mid max-w-md mx-auto">
          Nothing has been processed yet, so there are no detections to judge. New footage joins the
          labeling queue by itself as soon as a day finishes processing.
        </p>
        <Link
          to="/data"
          className="inline-block mt-4 text-sm text-accent hover:text-accent-deep transition-colors"
        >
          Upload a day of footage →
        </Link>
      </TerminalCard>
    );
  } else if (current !== null) {
    main = (
      <Card className="p-5 sm:p-6">
        {/* Day + camera, NEVER the clock time — the banner is masked server-side
            for the same reason: time of day hands the annotator the sun answer. */}
        <div className="flex items-baseline justify-between gap-3 flex-wrap">
          <span className="font-mono text-[11px] uppercase tracking-[0.16em] text-gray-tertiary">
            {current.day ?? "unknown day"} · {current.camera_id}
          </span>
          <span className="font-mono text-[11px] text-gray-tertiary tabular-nums">
            {current.n_annotators} of {current.target} answers so far
            {current.overlap ? " · overlap set" : ""}
          </span>
        </div>

        <InstanceCrop
          item={current}
          hideRing={hideRing}
          onError={(key) => {
            // A late error from an item already advanced past must not blank
            // the item now on screen — that is why the key rides along.
            if (key === bufferRef.current[0]?.instance_key) setFailedKey(key);
          }}
          className="mt-3 w-full max-w-[440px] mx-auto"
        />

        {cropFailed ? (
          // Terminal state 3: the image is missing or the server refused it
          // (e.g. a mostly-banner crop, §4.5). One key records that and moves on.
          <p role="alert" className="mt-3 text-[13px] text-danger text-center">
            This image can't be shown — press <Kbd>F</Kbd> to flag it and move on.
          </p>
        ) : null}

        <div className="mt-5">
          <LabelGroupList
            groups={groups}
            keys={keys}
            answers={answers}
            onAnswer={(g, c) => applyAnswer(g, c, "mouse")}
            openDefs={openDefs}
            onToggleDef={onToggleDef}
            disabled={busy || cropFailed}
          />
        </div>

        {staleNotice !== null ? (
          <p role="alert" className="mt-4 text-[13px] text-warn">
            {staleNotice}
          </p>
        ) : null}
        {nudge !== null ? (
          <p role="alert" className="mt-4 text-[13px] text-warn">
            {nudge}
          </p>
        ) : null}
        {writeError !== null ? (
          <div role="alert" className="mt-4 border border-danger/40 rounded-xl px-3.5 py-3">
            <p className="text-[13px] text-danger">Couldn't save — {writeError}</p>
            <p className="text-[12px] text-gray-tertiary mt-1">
              Your answer is still here
              {pending !== null ? " and will be retried as soon as the connection returns" : ""}.
            </p>
            {pending !== null ? (
              <div className="mt-2.5">
                <Button variant="ghost" onClick={() => void retryPending()} disabled={busy}>
                  Try again
                </Button>
              </div>
            ) : null}
          </div>
        ) : null}

        <div className="mt-5 pt-4 border-t border-border flex items-center gap-x-4 gap-y-2 flex-wrap">
          <span className="flex items-center gap-1.5">
            <Button onClick={() => trySave("mouse")} disabled={busy}>
              {busy ? "Saving…" : "Save"}
            </Button>
            <Kbd>Enter</Kbd>
          </span>
          <span className="flex items-center gap-1.5">
            <Button variant="ghost" onClick={() => setOverlay("skip")} disabled={busy}>
              Skip…
            </Button>
            <Kbd>S</Kbd>
          </span>
          <span className="flex items-center gap-1.5">
            <Button
              variant="ghost"
              onClick={() => {
                if (cropFailed) void doSkip("bad_crop", "flagged: the crop failed to load in the browser");
                else setOverlay("flag");
              }}
              disabled={busy}
            >
              Flag…
            </Button>
            <Kbd>F</Kbd>
          </span>
          <span className="flex items-center gap-1.5">
            <Button variant="ghost" onClick={() => void doUndo()} disabled={busy}>
              Undo
            </Button>
            <Kbd>U</Kbd>
          </span>
          <span className="ml-auto text-[11px] text-gray-tertiary">
            <Kbd>?</Kbd> shows every key
          </span>
        </div>
      </Card>
    );
  } else if (fetchError !== null) {
    main = (
      <TerminalCard title="Couldn't fetch the queue">
        <p className="text-sm text-gray-mid">{fetchError}</p>
        <div className="mt-5">
          <Button
            variant="ghost"
            onClick={() => {
              setFetchError(null);
              setQueueDrained(false);
              void refill();
            }}
          >
            Try again
          </Button>
        </div>
      </TerminalCard>
    );
  } else if (fetching || !queueDrained) {
    main = <div className="animate-shimmer h-96 bg-surface border border-border rounded-2xl" />;
  } else {
    // Terminal state 1: genuinely caught up.
    main = (
      <TerminalCard title="You're caught up">
        <p className="text-sm text-gray-mid max-w-md mx-auto">
          Every instance that still needs your judgement has it. New footage joins the queue by
          itself, and instances other annotators haven't finished stay available to them.
        </p>
        <div className="mt-5">
          <Button
            variant="ghost"
            onClick={() => {
              setQueueDrained(false);
              void refill();
            }}
          >
            Check again
          </Button>
        </div>
      </TerminalCard>
    );
  }

  return (
    <div className="flex flex-col gap-8 animate-fade-slide-in">
      <header className="flex items-end justify-between gap-4 flex-wrap">
        <div>
          <SectionLabel>LABEL</SectionLabel>
          <h1 className="font-display text-3xl sm:text-4xl font-light text-near-black leading-tight mt-1">
            Label cows
          </h1>
          <p className="text-gray-mid text-sm mt-2 max-w-xl">
            One ringed animal at a time — answer the questions under it and save. The keyboard is
            the fast path; press <Kbd>?</Kbd> for every binding.
          </p>
        </div>
        {/* Hidden, not disabled, for plain users — same rule as every other
            poweruser affordance (§5.1). */}
        {canManage ? (
          <Link
            to="/label/classes"
            className="font-mono text-[11px] uppercase tracking-[0.16em] text-gray-tertiary hover:text-accent transition-colors"
          >
            Manage classes →
          </Link>
        ) : null}
      </header>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_360px] items-start">
        <div className="min-w-0">{main}</div>
        <LabelProgress
          stats={stats}
          groups={groups}
          keys={keys}
          autoSubmit={autoSubmit}
          onAutoSubmitChange={setAutoSubmit}
          defsShown={defsShown}
          onDefsShownChange={setAllDefs}
          className="lg:sticky lg:top-[calc(var(--app-header-h)+24px)]"
        />
      </div>

      {overlay === "help" ? (
        <HelpSheet groups={groups} keys={keys} onClose={() => setOverlay(null)} />
      ) : null}
      {overlay === "skip" ? (
        <SkipDialog
          busy={busy}
          onPick={(reason) => void doSkip(reason, null)}
          onClose={() => setOverlay(null)}
        />
      ) : null}
      {overlay === "flag" ? (
        <FlagDialog
          busy={busy}
          onFlag={(reason, note) => void doSkip(reason, note === "" ? null : note)}
          onClose={() => setOverlay(null)}
        />
      ) : null}
    </div>
  );
}

/* One big friendly card per terminal state — same surface, different next action. */
function TerminalCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <Card className="p-8 sm:p-10 text-center">
      <h2 className="font-display text-2xl text-near-black leading-tight">{title}</h2>
      <div className="mt-3">{children}</div>
    </Card>
  );
}

/* Modal shell, the DeleteModal idiom: full-screen click-away layer, the card
   stops propagation. No scrim, matching the house dialogs. */
function Overlay({
  children,
  onClose,
  labelledBy,
}: {
  children: ReactNode;
  onClose: () => void;
  labelledBy: string;
}) {
  return (
    <div className="fixed inset-0 z-[100] grid place-items-center px-4" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={labelledBy}
        className="w-full max-w-md bg-surface border border-border rounded-2xl shadow-xl p-6 animate-fade-slide-in"
        onClick={(e) => e.stopPropagation()}
      >
        {children}
      </div>
    </div>
  );
}

/* The `?` sheet: the whole key map, fed from the same lib/labelKeys data as the
   option badges and the legend, so it cannot drift from the real bindings. */
function HelpSheet({
  groups,
  keys,
  onClose,
}: {
  groups: LabelGroup[];
  keys: LabelKeyMap;
  onClose: () => void;
}) {
  const groupOf = new Map(groups.map((g) => [g.group_key, g]));
  return (
    <Overlay onClose={onClose} labelledBy="label-help-title">
      <h3 id="label-help-title" className="font-display text-xl text-near-black leading-tight">
        All keys
      </h3>
      <div className="mt-4 flex flex-col gap-4 max-h-[60vh] overflow-y-auto pr-1">
        {keys.groups.map((gk) => {
          const group = groupOf.get(gk.group_key);
          if (group === undefined || gk.options.length === 0) return null;
          const nameOf = new Map(group.classes.map((c) => [c.class_key, c.name]));
          return (
            <div key={gk.group_key}>
              <SectionLabel>{group.name}</SectionLabel>
              <div className="mt-1.5 flex flex-col gap-1">
                {gk.options.map((o) => (
                  <div key={o.class_key} className="flex items-center gap-2.5">
                    {o.key !== null ? (
                      <Kbd>{o.label}</Kbd>
                    ) : (
                      <span className="inline-grid place-items-center min-w-6 h-6 text-[11px] text-gray-tertiary">
                        ·
                      </span>
                    )}
                    <span className="text-[13px] text-text">
                      {nameOf.get(o.class_key) ?? o.class_key}
                    </span>
                    {o.key === null ? (
                      <span className="text-[11px] text-gray-tertiary">mouse only</span>
                    ) : null}
                  </div>
                ))}
              </div>
            </div>
          );
        })}
        <div>
          <SectionLabel>Actions</SectionLabel>
          <div className="mt-1.5 flex flex-col gap-1">
            {LABEL_ACTIONS.map((a) => (
              <div key={a.action} className="flex items-center gap-2.5">
                <Kbd>{a.label}</Kbd>
                <span className="text-[13px] text-text">{a.hint}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
      <p className="text-[11px] text-gray-tertiary mt-4">
        <Kbd>Esc</Kbd> or <Kbd>?</Kbd> closes this sheet.
      </p>
    </Overlay>
  );
}

/* Skip is keyboard-fast: a digit picks the reason and posts in one press. The
   wording matters — a skip is recorded, not discarded (§3.3). */
function SkipDialog({
  busy,
  onPick,
  onClose,
}: {
  busy: boolean;
  onPick: (reason: LabelSkipReason) => void;
  onClose: () => void;
}) {
  return (
    <Overlay onClose={onClose} labelledBy="label-skip-title">
      <h3 id="label-skip-title" className="font-display text-xl text-near-black leading-tight">
        Skip this one — why?
      </h3>
      <p className="text-[12px] text-gray-tertiary mt-1.5">
        A skip is recorded, not discarded: it tells us the instance was hard to judge, and after a
        few independent skips it stops being served.
      </p>
      <div className="mt-4 flex flex-col gap-2">
        {SKIP_REASONS.map((r, i) => (
          <button
            key={r.reason}
            type="button"
            disabled={busy}
            onClick={() => onPick(r.reason)}
            className="flex items-baseline gap-2.5 rounded-xl border border-border bg-surface hover:border-accent px-3 py-2 text-left transition-colors duration-150"
          >
            <Kbd>{String(i + 1)}</Kbd>
            <span className="text-[13px] text-text">{r.label}</span>
            <span className="text-[11px] text-gray-tertiary">{r.blurb}</span>
          </button>
        ))}
      </div>
      <p className="text-[11px] text-gray-tertiary mt-4">
        <Kbd>Esc</Kbd> cancels.
      </p>
    </Overlay>
  );
}

/* Flag = a skip that carries a written note (the annotations.flag_note column).
   The instance_key rides in the payload, so traceability needs no clock time. */
function FlagDialog({
  busy,
  onFlag,
  onClose,
}: {
  busy: boolean;
  onFlag: (reason: LabelSkipReason, note: string) => void;
  onClose: () => void;
}) {
  const [reason, setReason] = useState<LabelSkipReason>("other");
  const [note, setNote] = useState("");
  return (
    <Overlay onClose={onClose} labelledBy="label-flag-title">
      <h3 id="label-flag-title" className="font-display text-xl text-near-black leading-tight">
        Flag a problem
      </h3>
      <p className="text-[12px] text-gray-tertiary mt-1.5">
        Skips this instance and attaches your note, so whoever reviews the queue can find it again.
      </p>
      <div className="mt-4 flex flex-col gap-1.5">
        {SKIP_REASONS.map((r) => (
          <label key={r.reason} className="flex items-baseline gap-2.5 cursor-pointer">
            <input
              type="radio"
              name="flag-reason"
              value={r.reason}
              checked={reason === r.reason}
              onChange={() => setReason(r.reason)}
              className="accent-accent"
            />
            <span className="text-[13px] text-text">{r.label}</span>
            <span className="text-[11px] text-gray-tertiary">{r.blurb}</span>
          </label>
        ))}
      </div>
      <label className="flex flex-col gap-1.5 mt-4">
        <span className="text-[12px] text-gray-tertiary">What's wrong? (optional)</span>
        <textarea
          value={note}
          maxLength={MAX_NOTE_CHARS}
          rows={3}
          autoFocus
          onChange={(e) => setNote(e.target.value)}
          className="bg-bg border border-border rounded-xl px-3 py-2 box-border text-sm text-text focus:outline-none focus:border-accent transition-colors resize-y"
        />
      </label>
      <div className="mt-4 flex items-center justify-end gap-3">
        <Button variant="ghost" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button onClick={() => onFlag(reason, note.trim())} disabled={busy}>
          Flag &amp; skip
        </Button>
      </div>
    </Overlay>
  );
}
