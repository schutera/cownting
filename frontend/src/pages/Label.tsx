import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { canManageData, useAuth } from "../lib/auth";
import type {
  InstanceAnchor,
  LabelFlagReq,
  LabelGroup,
  LabelInputMode,
  LabelItem,
  LabelMaskFixReq,
  LabelSkipReason,
  LabelStats,
  LabelSubmitReq,
  MaskFixKind,
  Taxonomy,
} from "../lib/types";
import { getLabelProgress, getLabelQueue, getTaxonomy, postLabelEvent } from "../lib/api";
import {
  isTypingTarget,
  LABEL_ACTIONS,
  OPTION_KEYS,
  resolveLabelKey,
  visibleClasses,
  visibleGroups,
} from "../lib/labelKeys";
import { emitDecisionEvent, useAnswerQueue } from "../lib/useAnswerQueue";
import type { AnswerQueueSync, LabelWrite } from "../lib/useAnswerQueue";
import { ClassIcon } from "../components/ClassIcon";
import { InstanceCrop } from "../components/InstanceCrop";
import { MaskEditor } from "../components/MaskEditor";
import { MaskPanel } from "../components/MaskPanel";
import { PANEL_H, QuestionPanel } from "../components/QuestionPanel";
import { TILE_GAP, TILE_H, TILE_W } from "../components/OptionTile";
import { LabelProgress } from "../components/LabelProgress";
import { approxIou, rectSeed, type Point } from "../lib/polygon";
import type { CropLevel } from "../lib/types";

/* The labeling screen (docs/roadmap/M3_labeling_ux.md; data model in
 * docs/roadmap/M3_labeling.md §5).
 *
 * ONE ITEM = ONE SCREEN = ONE FIXATION REGION + ONE SACCADE PER QUESTION (§2.1).
 * The previous layout stacked both questions as full-width rows under the crop,
 * which put the nearest answer 293px and the furthest 737px below the ringed
 * animal and pushed the second question and the Save button off a 1366x768
 * viewport entirely. Everything below is arithmetic in service of that one rule:
 * the crop is sized by SUBTRACTION from the viewport (§2.3), the question panel
 * is a fixed-height rectangle that Q2 replaces Q1 inside (§2.7), and nothing in
 * the per-item loop is ever reachable only by scrolling.
 *
 * THREE THINGS THIS FILE EXISTS TO NOT DO AGAIN:
 *
 *  1. ARROW OWNERSHIP (§3.8). The old option rows were `<input type="radio">` in
 *     a `role="radiogroup"`, which moves the checked radio on Left/Right and
 *     fires onChange — so one mouse click on an option armed ArrowLeft to WRITE
 *     A LABEL instead of moving the tape. The tiles are buttons now, and this
 *     page's key listener is registered in the CAPTURE phase and preventDefaults
 *     the arrows and Space before anything else can see them.
 *
 *  2. THE KEYBOARD IS NEVER BLOCKED (§3.5). `applyAnswer` has no `busy` guard and
 *     awaits nothing. Answers go into memory and into the optimistic queue; the
 *     sync state is displayed rather than waited on. A dropped keypress is worse
 *     than a slow save, because the annotator presses again and the second press
 *     lands on the next cow.
 *
 *  3. ANSWERS ARE KEYED BY INSTANCE KEY (§3.4, §6.2). There is deliberately no
 *     single "current answers" object: a shared object is exactly how an answer
 *     gets recorded against a cow the annotator was not looking at, which is the
 *     worst bug this feature can have. Every write path reads and writes
 *     `answersByKey[item.instance_key]` for an item captured in the same closure
 *     as the keystroke that caused it.
 *
 * WHAT IS INHERITED FROM THE OLD SCREEN, because it was right: the prefetch
 * buffer and its `exclude`-based refill, the served-event plumbing
 * (`serve_event_id` + `client_elapsed_ms` echoed on every write), the five
 * DISTINCT terminal states of §5.6 (a caught-up queue, an empty pool, a crop the
 * server would not serve, a failed write and an unusable taxonomy have different
 * causes and different next actions, so they are never collapsed into one), the
 * crop prefetch, and the visibility-aware ACTIVE clock — which is now also what
 * the per-decision timings of §6.1 are measured on, so a tab-away cannot present
 * as a five-minute decision.
 *
 * WHAT IS GONE, per §7 and the six fixed decisions: Save/Enter (selection IS
 * submission), Skip/S and the skip dialog (skipping is abolished; an instance
 * that cannot be answered is FLAGGED, with a reason and a written explanation),
 * Undo/U (ArrowLeft is the undo), the `?` sheet and the definitions toggle/I
 * (definitions open one at a time from a tile's info dot into the side panel's
 * reserved slot), every preference and every localStorage read.
 */

// ---------------------------------------------------------------- §2.3 budget
// Below the 73px sticky header a 1366x768 browser gives about 620px. The fixed
// chrome is 302px (28 strip + 16 main pad + 16 card pad + 8 gap + 160 panel +
// 10 gap + 32 footer + 16 card pad + 16 main pad), and 414 is that plus ~112px
// of browser chrome. These are a layout CONTRACT with QuestionPanel's PANEL_H
// and OptionTile's tile box — every pixel added here comes off the crop on a
// small screen — which is why they are named constants and not utility classes.
const CROP_MIN = 300;
const CROP_MAX = 440;
// 414 + the Classify|Outline toggle row (TOGGLE_H + CROP_GAP = 32). Every pixel
// of chrome added above the crop comes off the crop on a small screen, so a new
// control has to be paid for HERE — leaving this at 414 is how the question
// panel would have been pushed off a 1366x768 viewport again.
const CROP_CHROME = 446;
const TOGGLE_H = 24;
const STRIP_H = 28;
const FOOTER_H = 32;
const CARD_PAD = 16;
const CROP_GAP = 8;
const PANEL_GAP = 10;
// Space-hold (§2.4, revised) no longer scales the crop: it overlays the WHOLE
// frame, which is a different image at a different aspect ratio, so there is no
// scale factor to compute. The crop column keeps its geometry untouched.

// Refetch when this few items remain AHEAD of the cursor — early enough that the
// next batch lands before the annotator drains what is left.
const REFILL_AT = 3;
// Crops decoded ahead of the current one. The server's ETag + max-age makes the
// later real <img> load a cache hit, so this is one request per item, not two.
const PREFETCH_AHEAD = 3;
// Keys we just wrote, still excluded from refetches: our own submit may not be
// visible to the queue scan yet, and being served an item we answered seconds
// ago reads as a bug even when it is only a race.
const RECENT_CAP = 100;

// How many answered items stay on the tape behind the cursor, WITH their
// answers, so ArrowLeft re-shows and re-edits them with no round trip
// (Prodigy ships history_length: 10). This used to live in RecentStrip, but it
// was never really about the strip: it is the retention depth that decides how
// far back the undo reaches, and it outlived the thumbnails.
const MAX_RECENT = 10;
// A pool query per answered item would be one GET every ~3s of an eight-hour
// shift. The panel's numbers are feedback, not state, so they can lag.
const STATS_MIN_INTERVAL_MS = 5_000;
// How long a transient footer line stays up. Long enough to read at a glance,
// short enough that it is gone before the next item is answered.
const HINT_MS = 2_400;
// §3.4: answering the last question on a REVISITED item saves and stays; the
// chip is the only thing that says the correction landed.
const SAVED_CHIP_MS = 800;

const HAIRLINE = "var(--lbl-line, rgba(255, 255, 255, 0.09))";
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const CARD = "var(--lbl-card, #1E2124)";
const TILE = "var(--lbl-tile, #262A2E)";
const ALARM = "var(--lbl-alarm, #C8CDD2)";
// Chrome drawn ON TOP OF the crop. Deliberately NOT the --lbl-* tokens: those
// follow the page, and the page is now paper, but the photograph underneath is
// whatever the camera saw — usually dark. Themed ink here is how the caption
// went black-on-black the moment the route stopped being dark.
const ON_IMAGE_INK = "#F2F0EC";
const ON_IMAGE_ACCENT = "#F0B460";
const ON_IMAGE_SCRIM = "rgba(12,14,16,0.72)";
const ON_IMAGE_LINE = "rgba(255,255,255,0.28)";

/* The frozen flag reasons (types.ts LabelSkipReason), as a tile row bound to the
   same answer letters the questions use — the row is modal, so there is no
   collision, and reusing the letters keeps one set of keys to learn.
   `multiple_cows` in particular is a direct detector-quality signal, so
   the reason is asked AT THE PIXELS — three hundred items later it cannot be
   reconstructed from a thumbnail. The glyphs come from the same ClassIcon
   vocabulary the answers use; there is no flag-specific icon set to keep in
   sync, and `question` is already the screen's "I don't know" landmark. */
const FLAG_REASONS: { reason: LabelSkipReason; label: string; icon: string }[] = [
  { reason: "bad_crop", label: "Bad crop", icon: "eye-off" },
  { reason: "no_cow", label: "No cow", icon: "question" },
  { reason: "multiple_cows", label: "Multiple cows", icon: "dot" },
  { reason: "occluded", label: "Occluded", icon: "shade" },
  { reason: "low_resolution", label: "Resolution too low", icon: "pixels" },
  { reason: "other", label: "Other", icon: "question" },
];

/* The page's own keyframes. index.css belongs to the whole app and this feature
   is not allowed to extend it, so the three animations that are genuinely this
   screen's live here — the same call QuestionPanel makes. */
const PAGE_CSS = `
@keyframes lbl-fade-in { from { opacity: 0; } to { opacity: 1; } }
@keyframes lbl-sync-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.3; } }
.lbl-crop-in { animation: lbl-fade-in 100ms ease-out both; }
.lbl-sync-retry { animation: lbl-sync-pulse 1.2s ease-in-out infinite; }
@media (prefers-reduced-motion: reduce) {
  .lbl-crop-in { animation-duration: 1ms; }
  .lbl-sync-retry { animation: none; }
}
`;

/** The tape: every item served this session, in presentation order, plus where
    the annotator is standing on it. ONE state object rather than three, because
    `cursor` and `frontier` are indices INTO `items` — trimming the tail of
    history has to move all three or an ArrowLeft lands on the wrong cow. */
interface Tape {
  items: LabelItem[];
  /** The item on screen. Equals `items.length` only while the buffer refills. */
  cursor: number;
  /** The first item not yet answered — the head of the tape. `cursor < frontier`
      IS the review phase; there is no separate boolean to fall out of step. */
  frontier: number;
}

/* The flag row is now a single step, so the draft is just "is it open". The
   `reason` and `explanation` fields are gone with the two-step flow: picking a
   reason commits immediately, so there is no intermediate state to hold and
   nothing that can be half-filled when the annotator navigates away. */
interface FlagDraft {
  open: boolean;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v));
}

/** What ArrowLeft means right now. Extracted from the component because it is the
    rule, not the plumbing: the ordering of its three cases is the whole
    behaviour, and it is the part worth pinning in a test. */
export function backTarget(s: {
  step: number;
  cursor: number;
  inGeometry: boolean;
  outlineOpen: boolean;
}): "question" | "item" | "none" {
  // The geometry step precedes every question, so there is no earlier question
  // to step back to; and the outline editor owns its own keys, so ← never
  // reaches here from inside it.
  if (!s.inGeometry && !s.outlineOpen && s.step > 0) return "question";
  if (s.cursor > 0) return "item";
  return "none";
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
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

/* label_events.session_id is documented as uuid4 hex (32 chars, no dashes).
   Built from getRandomValues, NOT crypto.randomUUID: randomUUID is
   [SecureContext]-only and this dashboard is reachable over plain LAN http,
   where it would throw on mount. */
function newSessionId(): string {
  const buf = new Uint8Array(16);
  crypto.getRandomValues(buf);
  buf[6] = (buf[6] & 0x0f) | 0x40; // version 4
  buf[8] = (buf[8] & 0x3f) | 0x80; // RFC 4122 variant
  return Array.from(buf, (b) => b.toString(16).padStart(2, "0")).join("");
}

export default function Label() {
  const { user } = useAuth();
  const canManage = canManageData(user);

  // One labeling session per page mount; brackets the session_start/end events.
  const [sessionId] = useState(newSessionId);

  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [taxonomyError, setTaxonomyError] = useState<string | null>(null);
  const [staleNotice, setStaleNotice] = useState<string | null>(null);
  const [stats, setStats] = useState<LabelStats | null>(null);

  const [tape, setTapeState] = useState<Tape>({ items: [], cursor: 0, frontier: 0 });
  // Mirrored so the async refill and the key handler never act on a snapshot
  // that is one render old — being served a duplicate of the cow on screen, or
  // advancing past one, both present as data bugs rather than as races.
  const tapeRef = useRef<Tape>(tape);
  const setTape = useCallback((next: Tape) => {
    tapeRef.current = next;
    setTapeState(next);
  }, []);

  // group_key -> class_key, PER INSTANCE. Never one shared object (see header).
  const [answersByKey, setAnswersByKey] = useState<Record<string, Record<string, string>>>({});
  const [flaggedByKey, setFlaggedByKey] = useState<Record<string, LabelSkipReason>>({});

  const [step, setStep] = useState(0);
  const [flag, setFlag] = useState<FlagDraft>({ open: false });
  const [openDefinitionKey, setOpenDefinitionKey] = useState<string | null>(null);
  const [hint, setHint] = useState<string | null>(null);
  const [shakeNonce, setShakeNonce] = useState(0);
  const [savedChip, setSavedChip] = useState(false);
  // TWO independent hold-states, deliberately not one mode with a flag.
  // `clean` (hold H) strips the ring and the scrim at UNCHANGED size, so
  // occlusion is judged on unobstructed pixels. `inspect` (hold Space) swaps in
  // the whole uncropped frame and enlarges it, so the animal is judged in its
  // scene. They answer different questions and compose: holding both is the
  // clean full frame, which is a legitimate thing to want and costs nothing to
  // allow.
  const [clean, setClean] = useState(false);
  const [inspect, setInspect] = useState(false);
  const [failedKey, setFailedKey] = useState<string | null>(null);

  // ------------------------------------------------------- outline mode (M4a)
  // The Classify|Outline toggle is PER-ITEM TOOLING, not a destination, so it is
  // in-page state rather than a route (M4a §5.1) — and it is reset on every
  // advance below, for the same reason the two holds are: a mode that survived
  // onto the next cow would have the annotator editing an outline they never
  // chose to open. `draft` is null exactly when the mode is Classify.
  const [draft, setDraft] = useState<Point[] | null>(null);
  const [maskSaving, setMaskSaving] = useState(false);
  // Where the annotator is on the zoom ladder, or null to use the item's own
  // default. Held here rather than in MaskEditor so leaving the editor and
  // coming back to the same animal keeps the view they had chosen — and reset
  // per item below, because a zoom chosen for one cow means nothing on the next.
  const [zoom, setZoom] = useState<number | null>(null);
  // Which instances have PASSED the geometry step. Keyed by instance_key for the
  // same reason the answers are (see the header's point 3): a shared boolean is
  // how a confirmation given for one cow ends up gating a different one. It also
  // makes going back along the tape behave — a revisited item keeps its verdict
  // and does not ask again.
  const [geomByKey, setGeomByKey] = useState<Record<string, boolean>>({});

  const [fetching, setFetching] = useState(false);
  const [fetchError, setFetchError] = useState<string | null>(null);
  // The last refetch produced nothing new — stop polling until a write changes
  // the pool or the annotator asks to check again.
  const [queueDrained, setQueueDrained] = useState(false);
  const fetchingRef = useRef(false);
  const recentWrittenRef = useRef<string[]>([]);

  // Side-panel feedback (§2.5). All of it is per-session and owned here: the
  // corpus-wide LabelStats cannot say how TODAY is going.
  //
  // The per-class answer MIX that used to live here is gone with the side
  // panel's mix block. It was never only a display: §6.1 argued the running
  // share belonged in the panel rather than on the tile faces precisely because
  // a live percentage at the moment of choice nudges the choice. Removing the
  // readout removes the nudge outright, which is strictly safer for the
  // statistic; drift is still recoverable after the fact from the stored
  // answers, which is where the escape-rate monitor (§6.4) reads it from.
  const [recentMs, setRecentMs] = useState<number[]>([]);
  const [flagCount, setFlagCount] = useState(0);

  const [viewportH, setViewportH] = useState(() => window.innerHeight);

  // ------------------------------------------------------------- active clock
  // Only VISIBLE time counts: banked on hide, resumed on show. One running total
  // for the mount, with two marks taken off it — the item's and the active
  // question's — so `presented -> answered` and `answered -> answered` are both
  // measured on the same tab-away-proof clock (§6.1, §8.2).
  const accumRef = useRef(0);
  const sinceRef = useRef<number | null>(null);
  const activeNow = useCallback((): number => {
    const since = sinceRef.current;
    return accumRef.current + (since === null ? 0 : performance.now() - since);
  }, []);
  const itemMarkRef = useRef(0);
  const groupMarkRef = useRef(0);

  useEffect(() => {
    sinceRef.current = document.visibilityState === "visible" ? performance.now() : null;
    const onVis = () => {
      if (document.visibilityState === "hidden") {
        if (sinceRef.current !== null) {
          accumRef.current += performance.now() - sinceRef.current;
          sinceRef.current = null;
        }
      } else if (sinceRef.current === null) {
        sinceRef.current = performance.now();
      }
    };
    document.addEventListener("visibilitychange", onVis);
    return () => document.removeEventListener("visibilitychange", onVis);
  }, []);

  // ------------------------------------------------------------ derived state

  const groups = useMemo(() => taxonomy?.groups ?? [], [taxonomy]);
  // The questions, in the order they are answered. A group whose classes are all
  // archived is dropped entirely rather than shown as an unanswerable step —
  // it would be a rectangle with no way out.
  const steps = useMemo(
    () => visibleGroups(groups).filter((g) => visibleClasses(g).length > 0),
    [groups],
  );
  const taxonomyUsable = steps.length > 0;
  // The tile row reserves the WIDEST question's width so the frame does not
  // resize at the handoff (§2.7).
  const reserveOptions = useMemo(
    () => steps.reduce((n, g) => Math.max(n, visibleClasses(g).length), 1),
    [steps],
  );

  const current: LabelItem | null = tape.cursor < tape.items.length ? tape.items[tape.cursor] : null;
  const currentKey = current?.instance_key ?? null;
  const reviewing = current !== null && tape.cursor < tape.frontier;
  const cropFailed = currentKey !== null && failedKey === currentKey;
  const flaggedReason = currentKey === null ? undefined : flaggedByKey[currentKey];

  const EMPTY_ANSWERS: Record<string, string> = useMemo(() => ({}), []);
  const answers = currentKey === null ? EMPTY_ANSWERS : (answersByKey[currentKey] ?? EMPTY_ANSWERS);

  const activeGroup: LabelGroup | null = steps.length === 0 ? null : steps[Math.min(step, steps.length - 1)];
  const activeOptions = useMemo(
    () => (activeGroup === null ? [] : visibleClasses(activeGroup)),
    [activeGroup],
  );


  const cropPx = clamp(CROP_MIN, viewportH - CROP_CHROME, CROP_MAX);

  // ------------------------------------------------------------ outline seeds
  // What the editor opens with, and what Revert returns to. Both read the ITEM,
  // never the draft, so they are stable across an edit.
  // FULL-FRAME px — the space the editor works in, so that zooming changes the
  // visible crop and nothing else. `mask` (crop-local) is still what the
  // read-only preview on the classify view draws.
  const servedMask = useMemo<Point[] | null>(() => {
    const m = current?.mask_frame;
    // A server that predates M4a sends nothing; one that has no stored mask for
    // this instance sends null. Both mean "seed from the ring" (§4.1), and a
    // polygon too short to be a shape is treated the same way rather than
    // opening an editor on a line.
    if (m === undefined || m === null || m.length < 3) return null;
    return m.map(([x, y]): Point => [x, y]);
  }, [current]);
  /* The SAME outline, CROP-LOCAL, for the read-only overlay on the crop.
     InstanceCrop draws into a `0 0 crop_w crop_h` viewBox, so it cannot be fed
     `servedMask`: that is full-frame px now, and a point at (1168, 534) inside a
     59x59 viewBox is simply off the canvas — the overlay vanishes rather than
     misdraws, which is why it looked like "no overlay" instead of "wrong
     overlay". The server sends both projections precisely so neither side has to
     convert. */
  const servedMaskCrop = useMemo<Point[] | null>(() => {
    const m = current?.mask;
    if (m === undefined || m === null || m.length < 3) return null;
    return m.map(([x, y]): Point => [x, y]);
  }, [current]);

  const seed = useMemo<Point[] | null>(() => {
    if (current === null) return null;
    // The fallback rectangle is the BBOX, in the same full-frame space — not the
    // crop-local ring, which would put the seed in a different coordinate system
    // from everything else the editor touches.
    return servedMask ?? rectSeed(current.bbox);
  }, [current, servedMask]);
  const outlineOpen = draft !== null;
  // The editor is offered even with no stored mask (decision 2: sculpting from
  // the ring beats a control that is dead on every pre-remask day) — but not on
  // a crop whose pixels never arrived, where there is nothing to trace against.
  const outlineAvailable = current !== null && !cropFailed;
  const maskDirty = useMemo(() => {
    if (draft === null || seed === null) return false;
    if (draft.length !== seed.length) return true;
    return draft.some((p, i) => p[0] !== seed[i][0] || p[1] !== seed[i][1]);
  }, [draft, seed]);
  // Only against a REAL model polygon: comparing a sculpted outline to the
  // rectangle it was seeded from would print a number that means nothing.
  const maskIou = useMemo(
    () => (draft === null || servedMask === null ? null : approxIou(draft, servedMask)),
    [draft, servedMask],
  );

  /* THE GEOMETRY STEP (M4a §5.1, revised). Checking the outline is no longer an
     optional mode: it is step 1 of the item, and the questions are not reachable
     until it is passed. The reason is a data one rather than a UI one — an answer
     recorded against a box that is on the wrong animal, or on no animal, is not a
     weak label but a WRONG one, and nothing downstream can tell the two apart.
     Asking first is also the only order that is cheap: the annotator is already
     looking at the pixels, and finding out afterwards means re-deciding answers
     that were given about something else.

     It costs one keystroke on a correct detection (Enter), which is the price of
     the measurement it produces: a confirmation is stored as an 'ok' verdict, so
     the corpus gains a per-model false-positive rate instead of only corrections.
     A crop that could not be shown is exempt — there is nothing to verify, and F
     is the way out of those. */
  // This session's verdict first, then the one the server already holds. The
  // fallback is what makes the step survive a reload: without it the gate
  // re-asks about a cow this annotator already judged, and answering that second
  // asking used to overwrite their own correction.
  const geomDone =
    currentKey === null ? false : (geomByKey[currentKey] ?? current?.geom_done ?? false);

  // The zoom ladder, with a single-rung fallback so a server that predates it
  // still yields a working editor at the crop it did send.
  const cropLevels = useMemo<CropLevel[]>(() => {
    if (current === null) return [];
    if (current.crop_levels && current.crop_levels.length > 0) return current.crop_levels;
    return [{
      pad: 0,
      url: current.crop_url,
      // Without a served source box the crop's own pixel grid is the only space
      // available; the ring is in exactly that space.
      src: [0, 0, current.crop_w, current.crop_h],
      out: current.crop_w,
    }];
  }, [current]);
  const zoomIndex = zoom ?? current?.crop_level ?? 0;
  const inGeometry = current !== null && !geomDone && !cropFailed;

  useEffect(() => {
    const onResize = () => setViewportH(window.innerHeight);
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

// The Label route used to repaint the body near-black and cancel `main`'s
// padding to go full-bleed. Both are gone: the route now uses the app's own
// paper surface and its normal page rhythm, so there is nothing to override
// and nothing to restore on the way out.

  // ------------------------------------------------------------ nudges & shake

  const hintTimer = useRef<number | null>(null);
  const showHint = useCallback((text: string) => {
    setHint(text);
    if (hintTimer.current !== null) window.clearTimeout(hintTimer.current);
    hintTimer.current = window.setTimeout(() => setHint(null), HINT_MS);
  }, []);
  // A dead key must be SEEN (§3.3): CVAT #8400 shipped a build where keys 0-8
  // worked and 9 silently did nothing, and a silent no-op on this screen is
  // indistinguishable from a slow save — the annotator presses again.
  const shake = useCallback(() => setShakeNonce((n) => n + 1), []);

  const chipTimer = useRef<number | null>(null);
  const showSavedChip = useCallback(() => {
    setSavedChip(true);
    if (chipTimer.current !== null) window.clearTimeout(chipTimer.current);
    chipTimer.current = window.setTimeout(() => setSavedChip(false), SAVED_CHIP_MS);
  }, []);

  useEffect(
    () => () => {
      if (hintTimer.current !== null) window.clearTimeout(hintTimer.current);
      if (chipTimer.current !== null) window.clearTimeout(chipTimer.current);
    },
    [],
  );

  // ------------------------------------------------------------- data fetching

  const lastStatsRef = useRef(0);
  const refreshStats = useCallback((force = false) => {
    const t = Date.now();
    if (!force && t - lastStatsRef.current < STATS_MIN_INTERVAL_MS) return;
    lastStatsRef.current = t;
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
      const have = tapeRef.current.items.map((i) => i.instance_key);
      // Tape keys first: api.ts slices `exclude` to the server's 200 cap, and
      // losing a recently-written key to the cap is survivable while losing a
      // tape key would serve us a duplicate of what is already on screen.
      // SPREAD, not the server's `fresh` default: draw from the WHOLE corpus —
      // every day and every camera interleaved — rather than draining the newest
      // day first.
      //
      // It is a sampling decision, not a preference. Labelling a day at a time
      // means consecutive items share weather, herd position and sun angle, so a
      // session's worth of answers is far more correlated than its count
      // suggests, and any model trained on the first N labels sees one afternoon
      // rather than a season. The ordering is still the same stable
      // per-annotator md5 permutation, so two annotators walk the pool in
      // different orders and meet in the middle instead of racing down one list.
      //
      // What this trades away: a freshly uploaded day no longer appears at the
      // head of the queue — its cows are scattered through the permutation like
      // everything else. Pass `order: "fresh"` here to get that back.
      const q = await getLabelQueue({
        exclude: [...have, ...recentWrittenRef.current],
        order: "spread",
      });
      const known = new Set([...have, ...recentWrittenRef.current]);
      const fresh = q.items.filter((i) => !known.has(i.instance_key));
      if (fresh.length === 0) {
        setQueueDrained(true);
      } else {
        const t = tapeRef.current;
        setTape({ ...t, items: [...t.items, ...fresh] });
      }
      setFetchError(null);
    } catch (e) {
      // Only fatal when there is nothing left to work on — with items still on
      // the tape the annotator keeps labeling and the next write retriggers.
      if (tapeRef.current.items.length === 0) setFetchError(errMsg(e));
    } finally {
      fetchingRef.current = false;
      setFetching(false);
    }
  }, [setTape]);

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

  // Keep the tape topped up AHEAD of the cursor. Waits for the taxonomy: an item
  // without its questions is unanswerable, and the queue GET writes served
  // events we would waste (§4.2 — every serve is an abandonment candidate until
  // it is answered).
  const ahead = tape.items.length - tape.cursor - 1;
  useEffect(() => {
    if (taxonomy === null || queueDrained) return;
    if (ahead > REFILL_AT) return;
    void refill();
  }, [taxonomy, ahead, queueDrained, refill]);

  // Decode upcoming crops off-screen so advancing swaps pixels instantly.
  const prefetchedRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    for (const it of tape.items.slice(tape.cursor + 1, tape.cursor + 1 + PREFETCH_AHEAD)) {
      if (prefetchedRef.current.has(it.crop_url)) continue;
      prefetchedRef.current.add(it.crop_url);
      const img = new Image();
      img.src = it.crop_url;
    }
  }, [tape]);

  // -------------------------------------------------------------- write path

  const onStale = useCallback(() => {
    // A 409 is emphatically not a network failure: a poweruser edited the
    // questions mid-session. The write is dropped by the queue, so the instance
    // was never answered as far as the server is concerned and the queue will
    // serve it again — but the annotator has to be TOLD, or a silently vanished
    // answer is exactly the kind of loss this screen was rebuilt to prevent.
    getTaxonomy()
      .then((t) => {
        setTaxonomy(t);
        setStaleNotice(
          "The questions changed while you were labeling — answers that hadn't reached the server were not saved, and those cows will come round again.",
        );
      })
      .catch((e: unknown) => setStaleNotice(errMsg(e)));
  }, []);

  const onWritten = useCallback(
    (write: LabelWrite, result?: unknown) => {
      // The pool moved under us, so any "drained" verdict is stale by definition.
      setQueueDrained(false);
      refreshStats();
      // An outline landed: fold the STORED polygon back into the item on the
      // tape, in both spaces, so hold-Space draws the correction the annotator
      // just made instead of the state before it. Both come from the server —
      // the client never converts between crop and frame coordinates. Without
      // this the editor and the peek disagree until the next queue fetch.
      if (write.kind !== "mask" || result === null || typeof result !== "object") return;
      const r = result as { mask?: unknown; mask_frame?: unknown };
      const key = write.req.instance_key;
      const asPoly = (v: unknown): [number, number][] | null =>
        Array.isArray(v) &&
        v.length >= 3 &&
        v.every((p) => Array.isArray(p) && p.length === 2 && p.every((n) => typeof n === "number"))
          ? v.map((p) => [p[0], p[1]] as [number, number])
          : null;
      const mask = asPoly(r.mask);
      const maskFrame = asPoly(r.mask_frame);
      if (mask === null && maskFrame === null) return; // a removal carries no geometry
      // The re-framed crop, when the server sent one: after a correction the tile
      // is re-cut around the shape the annotator drew, so the questions are asked
      // over a centred animal instead of one sitting off to one side of a crop
      // cut around the box they just rejected. `bbox` is deliberately NOT patched
      // — it is identity, and every later submit still echoes the anchor the
      // queue signed.
      const r2 = result as {
        crop_url?: unknown; crop_w?: unknown; crop_h?: unknown; ring?: unknown;
        crop_levels?: unknown; crop_level?: unknown;
      };
      const framed =
        typeof r2.crop_url === "string" &&
        typeof r2.crop_w === "number" &&
        typeof r2.crop_h === "number" &&
        Array.isArray(r2.ring) &&
        r2.ring.length === 4
          ? {
              crop_url: r2.crop_url,
              crop_w: r2.crop_w,
              crop_h: r2.crop_h,
              ring: r2.ring as [number, number, number, number],
              ...(Array.isArray(r2.crop_levels) && typeof r2.crop_level === "number"
                ? {
                    crop_levels: r2.crop_levels as CropLevel[],
                    crop_level: r2.crop_level,
                  }
                : {}),
            }
          : {};
      const t = tapeRef.current;
      setTape({
        ...t,
        items: t.items.map((it) =>
          it.instance_key === key
            ? { ...it, mask, mask_frame: maskFrame, mask_seed: "edit", ...framed }
            : it,
        ),
      });
    },
    [refreshStats, setTape],
  );

  const queue = useAnswerQueue({ onStale, onWritten });

  const markWritten = useCallback((key: string) => {
    const next = recentWrittenRef.current.filter((k) => k !== key);
    next.push(key);
    recentWrittenRef.current = next.slice(-RECENT_CAP);
  }, []);

  // ------------------------------------------------------------- the sequence

  const firstUnansweredIndex = useCallback(
    (ans: Record<string, string>): number | null => {
      for (let i = 0; i < steps.length; i += 1) {
        if (ans[steps[i].group_key] === undefined) return i;
      }
      return null;
    },
    [steps],
  );

  // Completeness is about REQUIRED groups only — an optional question left blank
  // must not wedge the annotator on an item with no way forward but a flag.
  const isComplete = useCallback(
    (ans: Record<string, string>): boolean =>
      steps.every((g) => !g.required || ans[g.group_key] !== undefined),
    [steps],
  );

  /** Move the cursor. Never leaves bounds, always re-reads the item from Q1
      (§3.7: going back means re-reading from the top), and always leaves the
      flag row — a half-typed flag on the cow you just left is not a state. */
  const goTo = useCallback(
    (index: number) => {
      const t = tapeRef.current;
      if (t.items.length === 0) return;
      const clamped = clamp(index, 0, t.items.length - 1);
      setTape({ ...t, cursor: clamped });
      setStep(0);
      setFlag({ open: false });
      setSavedChip(false);
      setHint(null);
    },
    [setTape],
  );

  /** Commit the item on screen and move to the head of the tape. The write goes
      to the optimistic queue and the UI does not wait for it (§3.4's advance is
      130-230ms with no network on the path). */
  const advance = useCallback(() => {
    const t = tapeRef.current;
    const cursor = Math.min(t.cursor + 1, t.items.length);
    const frontier = Math.max(t.frontier, cursor);
    // Prodigy's history_length: the last ten answered items stay in memory with
    // their answers so ArrowLeft costs no round trip. Everything older is
    // dropped from the tape — and its answers with it, or the map would grow for
    // the whole shift holding cows nobody can reach.
    const drop = Math.max(0, cursor - MAX_RECENT);
    if (drop > 0) {
      const dropped = tapeRef.current.items.slice(0, drop);
      setAnswersByKey((prev) => {
        const next = { ...prev };
        for (const it of dropped) delete next[it.instance_key];
        return next;
      });
      // The geometry verdicts go with them, for the same reason: a map that grew
      // for a whole shift would hold every cow the annotator can no longer reach.
      setGeomByKey((prev) => {
        const next = { ...prev };
        for (const it of dropped) delete next[it.instance_key];
        return next;
      });
      setTape({ items: t.items.slice(drop), cursor: cursor - drop, frontier: frontier - drop });
    } else {
      setTape({ items: t.items, cursor, frontier });
    }
    setStep(0);
    setFlag({ open: false });
  }, [setTape]);

  const commitAnswers = useCallback(
    (item: LabelItem, ans: Record<string, string>, mode: LabelInputMode) => {
      if (taxonomy === null) return;
      const req: LabelSubmitReq = {
        instance_key: item.instance_key,
        anchor: anchorOf(item),
        answers: ans,
        taxonomy_revision: taxonomy.revision,
        serve_event_id: item.serve_event_id,
        session_id: sessionId,
        client_elapsed_ms: Math.max(0, Math.round(activeNow() - itemMarkRef.current)),
        input_mode: mode,
      };
      queue.enqueue({ kind: "answer", req });
      markWritten(item.instance_key);
    },
    [activeNow, markWritten, queue, sessionId, taxonomy],
  );

  /** One answer, from a digit or from a tile click. Synchronous, unguarded, and
      the only place an advance can be triggered from — never an effect watching
      `answersByKey`, which is what made the old screen re-commit an item the
      moment you touched any option on a revisit (§1.6, §3.4). */
  const applyAnswer = useCallback(
    (groupKey: string, classKey: string, mode: LabelInputMode) => {
      if (current === null || currentKey === null) return;
      if (cropFailed) {
        // Not a silent refusal: there are no pixels to judge, and F is the way out.
        shake();
        showHint("this image can't be shown — press F to flag it");
        return;
      }
      const item = current;
      const key = currentKey;
      const previous = answers[groupKey];
      const now = activeNow();
      const detail = {
        group_key: groupKey,
        ms_since_group_shown: Math.max(0, Math.round(now - groupMarkRef.current)),
        ms_since_item_shown: Math.max(0, Math.round(now - itemMarkRef.current)),
        replaced_class_key: previous ?? null,
        input_mode: mode,
        phase: reviewing ? "review" : "fresh",
      };

      // §3.3: pressing the digit of the class already chosen CLEARS it — the
      // correction path that never leaves the item. Two platforms shipped this
      // independently (SuperAnnotate, Supervisely). No handoff, no advance.
      if (previous === classKey) {
        setAnswersByKey((prev) => {
          const forItem = { ...(prev[key] ?? {}) };
          delete forItem[groupKey];
          return { ...prev, [key]: forItem };
        });
        // class_key null, replaced_class_key set: a clear is still a within-item
        // correction and A8 has to be able to count it.
        emitDecisionEvent({
          session_id: sessionId,
          kind: "answered",
          instance_key: key,
          class_key: null,
          detail,
        });
        return;
      }

      const next = { ...answers, [groupKey]: classKey };
      setAnswersByKey((prev) => ({ ...prev, [key]: next }));
      emitDecisionEvent({
        session_id: sessionId,
        kind: "answered",
        instance_key: key,
        class_key: classKey,
        detail,
      });

      if (reviewing) {
        // THE REVISIT TRAP (§1.6, §6.2). This branch must never advance. The old
        // code fired the submit as soon as every group had an answer, which on a
        // revisited item is true the instant you touch ANY option — so correcting
        // Q1 skipped past Q2 and moved on. Here the correction is saved in place
        // and the annotator leaves with ArrowRight, deliberately.
        if (previous !== undefined) {
          postLabelEvent({
            session_id: sessionId,
            kind: "relabel",
            instance_key: key,
            class_key: classKey,
          }).catch(() => {});
        }
        if (isComplete(next)) {
          commitAnswers(item, next, mode);
          showSavedChip();
        }
        // Hand off by POSITION, not by "first unanswered": on a revisited item
        // every group is already answered, and "first unanswered" is exactly the
        // null that used to mean "advance".
        if (step + 1 < steps.length) setStep(step + 1);
        return;
      }

      const nextStep = firstUnansweredIndex(next);
      if (nextStep !== null) {
        setStep(nextStep); // the handoff — same rectangle, letters rebound (§3.4)
        return;
      }
      commitAnswers(item, next, mode);
      setRecentMs((prev) => [Math.max(0, Math.round(now - itemMarkRef.current)), ...prev].slice(0, 200));
      advance();
    },
    [
      activeNow,
      advance,
      answers,
      commitAnswers,
      cropFailed,
      current,
      currentKey,
      firstUnansweredIndex,
      isComplete,
      reviewing,
      sessionId,
      shake,
      showHint,
      showSavedChip,
      step,
      steps.length,
    ],
  );

  // The reason IS the flag. The written explanation it used to demand is gone:
  // the six codes carry the signal the prose stood in for, and once instance
  // masks land a defective crop is inspectable rather than describable. So this
  // takes the reason directly instead of reading it back off the draft — the
  // pick and the submit are now one gesture, and routing a two-keystroke flag
  // through a state round trip is how you drop the second keystroke.
  const submitFlag = useCallback((picked: LabelSkipReason) => {
    if (current === null || currentKey === null) return;
    const reason = picked;
    const req: LabelFlagReq = {
      instance_key: current.instance_key,
      anchor: anchorOf(current),
      reason,
      serve_event_id: current.serve_event_id,
      session_id: sessionId,
      client_elapsed_ms: Math.max(0, Math.round(activeNow() - itemMarkRef.current)),
    };
    queue.enqueue({ kind: "flag", req });
    markWritten(current.instance_key);
    setFlaggedByKey((prev) => ({ ...prev, [currentKey]: reason }));
    setFlagCount((n) => n + 1);
    if (reviewing) {
      // Flagging something you had already answered leaves review rather than
      // advancing the frontier past an item that is still ahead of you.
      goTo(tapeRef.current.frontier);
    } else {
      advance();
    }
  }, [activeNow, advance, current, currentKey, goTo, markWritten, queue, reviewing, sessionId]);

  // ------------------------------------------------------ outline mode (M4a)

  const openOutline = useCallback(() => {
    if (current === null || seed === null) return;
    if (cropFailed) {
      shake();
      showHint("this image can't be shown — press F to flag it");
      return;
    }
    setDraft(seed);
    setFlag({ open: false });
    setOpenDefinitionKey(null);
    setHint(null);
  }, [cropFailed, current, seed, shake, showHint]);

  const closeOutline = useCallback(() => {
    setDraft(null);
    setMaskSaving(false);
  }, []);

  /** Both outline writes: a corrected polygon, or the false-positive verdict.
      They share everything except `kind` and what happens afterwards, so they
      share a function — two copies would drift on the anchor or the timing. */
  const commitMask = useCallback(
    (kind: MaskFixKind) => {
      if (current === null || currentKey === null) return;
      if (kind === "polygon" && (draft === null || draft.length < 3)) return;
      const req: LabelMaskFixReq = {
        instance_key: current.instance_key,
        anchor: anchorOf(current),
        kind,
        // FULL-FRAME px, which is the space the editor already works in — so
        // there is no conversion on this path at all, and no basis for the
        // stored outline to disagree with the pixels it was drawn on, whatever
        // zoom level it was drawn at.
        space: "frame",
        polygon: kind === "polygon" && draft !== null ? draft.map(([x, y]): [number, number] => [x, y]) : null,
        mask_rev: current.mask_rev ?? null,
        // What this correction was actually drawn over, straight from the item —
        // never re-derived here, or the stored provenance would be the client's
        // opinion rather than what the server served.
        seeded_from: servedMask === null ? "bbox" : (current.mask_seed ?? "model"),
        serve_event_id: current.serve_event_id,
        session_id: sessionId,
        client_elapsed_ms: Math.max(0, Math.round(activeNow() - itemMarkRef.current)),
      };
      setMaskSaving(true);
      queue.enqueue({ kind: "mask", req });
      // Deliberately NOT markWritten(): an outline fix is not an answer, and
      // excluding the key from refetches would hide an instance whose questions
      // are still unanswered. A removal is different — see below.
      postLabelEvent({
        session_id: sessionId,
        kind: "info_opened", // nearest stored kind until the server learns 'mask_fixed'
        instance_key: currentKey,
      }).catch(() => {});

      // Any verdict passes the geometry step: the annotator has now looked at
      // the outline and said something about it, which is exactly what the step
      // is for. Set before the branch so a removal cannot leave the flag behind
      // on an item the tape may still hold.
      setGeomByKey((prev) => ({ ...prev, [currentKey]: true }));

      if (kind === "false_positive") {
        // There is no cow to ask questions about, so this one is done. The key
        // IS excluded here: the server will retire it for everyone, and being
        // served it again before that lands reads as a bug.
        markWritten(current.instance_key);
        closeOutline();
        if (reviewing) goTo(tapeRef.current.frontier);
        else advance();
        return;
      }
      // 'ok' and 'polygon' both hand on to the questions on the SAME animal: the
      // geometry step is satisfied, and the cow still has to be answered.
      closeOutline();
      setStep(0);
      // Only a CORRECTION gets the chip. Confirming is the routine path taken by
      // most items, and a "saved ✓" flashing on every cow is noise that teaches
      // the annotator to ignore the one place the screen reports a write.
      if (kind === "polygon") showSavedChip();
    },
    [
      activeNow,
      advance,
      closeOutline,
      current,
      currentKey,
      draft,
      goTo,
      markWritten,
      queue,
      reviewing,
      servedMask,
      sessionId,
      showSavedChip,
    ],
  );

  /** "The outline is right" — the fast path through the geometry step, and the
      one the great majority of items take. It writes an 'ok' verdict (the
      measurement, §5.1) and hands straight to question 1. */
  const confirmGeometry = useCallback(() => {
    if (current === null || currentKey === null || maskSaving) return;
    commitMask("ok");
    setStep(0);
  }, [commitMask, current, currentKey, maskSaving]);

  const goPrev = useCallback(() => {
    const t = tapeRef.current;
    switch (backTarget({ step, cursor: t.cursor, inGeometry, outlineOpen })) {
      case "question":
        // WITHIN the item first. A mis-click on question 1 is corrected by
        // stepping back to it, not by abandoning the animal — and until this
        // existed there was no way back at all: answering handed off to question
        // 2, and ← moved the tape, so on the first item of a session it only
        // shook and said the tape went no further. The tile still shows what was
        // chosen, so pressing a different letter replaces it and pressing the
        // same one clears it.
        setStep(step - 1);
        setHint(null);
        return;
      case "item":
        goTo(t.cursor - 1);
        return;
      case "none":
        shake();
        showHint("this is as far back as the tape goes");
    }
  }, [goTo, inGeometry, outlineOpen, shake, showHint, step]);

  const goNext = useCallback(() => {
    const t = tapeRef.current;
    // §3.7: on a revisited item ArrowRight LEAVES review and returns to the head
    // of the tape, rather than walking forward one answered item at a time.
    if (t.cursor < t.frontier) {
      goTo(t.frontier);
      return;
    }
    if (current === null) return;
    if (!isComplete(answers)) {
      // There is no skipping. The only ways off an item are answering it and
      // flagging it, and a silent no-op here would read as a broken key.
      shake();
      showHint("answer both questions, or press F to flag");
      return;
    }
    commitAnswers(current, answers, "key");
    setRecentMs((prev) =>
      [Math.max(0, Math.round(activeNow() - itemMarkRef.current)), ...prev].slice(0, 200),
    );
    advance();
  }, [activeNow, advance, answers, commitAnswers, current, goTo, isComplete, shake, showHint]);

  const openFlag = useCallback(() => {
    if (current === null) return;
    setFlag({ open: true });
    setHint(null);
  }, [current]);

  const closeFlag = useCallback(() => {
    setFlag({ open: false });
  }, []);

  const onOpenDefinition = useCallback(
    (classKey: string) => {
      setOpenDefinitionKey(classKey);
      // A deliberate single open is the "this definition is ambiguous" signal
      // (SQL_INFO_ICON_PRESSURE).
      postLabelEvent({
        session_id: sessionId,
        kind: "info_opened",
        instance_key: tapeRef.current.items[tapeRef.current.cursor]?.instance_key ?? null,
        class_key: classKey,
      }).catch(() => {});
    },
    [sessionId],
  );

  // ------------------------------------------------------------- telemetry §6.1

  // `presented` is the real per-item clock. The batch `served` row is written
  // once per fetch (labeling.py:412-424), so item k of a batch of eight carries
  // items 1..k-1 as well as its own — keep it as the abandonment denominator,
  // but never compute effort from it.
  useEffect(() => {
    if (currentKey === null) return;
    const mark = activeNow();
    itemMarkRef.current = mark;
    groupMarkRef.current = mark;
    // Both holds reset on a new item (§3.2): a hold that survived the advance
    // would apply to a cow the annotator has not looked at yet. The outline
    // mode resets for the same reason and is stronger still — a draft carried
    // onto the next cow would be a polygon drawn on one animal and saved
    // against another.
    setInspect(false);
    setClean(false);
    setDraft(null);
    setMaskSaving(false);
    setZoom(null);
    // A popover must not survive the advance: the same class_key exists in the
    // next item, so it would silently reappear anchored to a cow nobody opened
    // it for.
    setOpenDefinitionKey(null);
    const t = tapeRef.current;
    emitDecisionEvent({
      session_id: sessionId,
      kind: "presented",
      instance_key: currentKey,
      detail: { phase: t.cursor < t.frontier ? "review" : "fresh" },
    });
  }, [activeNow, currentKey, sessionId]);

  // The per-QUESTION clock. A2 (median Q2 time within 1.25x of Q1) is the
  // sharpest acceptance test in §8.3 and it needs this mark to exist.
  useEffect(() => {
    groupMarkRef.current = activeNow();
  }, [activeNow, currentKey, step]);

  // ---------------------------------------------------------------- keyboard

  // ONE window listener in the CAPTURE phase, delegating to a ref that is
  // refreshed after every render so the handler always sees fresh state without
  // re-binding on each keystroke. Capture phase is not a detail: at bubble phase
  // a focused native control has already acted on ArrowLeft, and only
  // preventDefault stops it (§3.8).
  const keydownRef = useRef<(e: KeyboardEvent) => void>(() => {});
  const keyupRef = useRef<(e: KeyboardEvent) => void>(() => {});

  const onKeyDown = (e: KeyboardEvent) => {
    // Escape first — it must also fire from inside the flag explanation
    // textarea, which the typing guard below deliberately swallows keys for.
    if (e.key === "Escape") {
      if (outlineOpen) {
        // Leaves without saving — the draft is discarded on purpose. Nothing
        // here is worth an "are you sure": reopening re-seeds from the model
        // polygon, which is one keystroke away.
        e.preventDefault();
        closeOutline();
        return;
      }
      if (flag.open) {
        e.preventDefault();
        closeFlag();
        return;
      }
      if (openDefinitionKey !== null) {
        e.preventDefault();
        setOpenDefinitionKey(null);
        return;
      }
      if (hint !== null) setHint(null);
      return;
    }
    if (isTypingTarget(e.target)) return;

    // §3.8, the half that cannot be skipped: these three are the page's, and
    // preventDefault here (before the default action, and before any element
    // handler, because this listener captures) is what stops the browser
    // scrolling on Space and any focused widget moving a selection on an arrow.
    // ONLY for an unmodified press: Alt+ArrowLeft is the browser's Back and
    // Ctrl/Meta chords belong to the OS, and stealing those would be a second
    // ownership bug in the opposite direction. labelKeys' own isPlainPress
    // applies the same rule to the bindings themselves.
    const plain = !e.ctrlKey && !e.metaKey && !e.altKey;
    if (plain && (e.key === "ArrowLeft" || e.key === "ArrowRight" || e.key === " ")) {
      e.preventDefault();
    }

    if (e.key === " ") {
      if (!plain) return;
      // A HOLD, never a toggle: a mode you can be stranded in is worse than no
      // mode. `repeat` is ignored so the key's auto-repeat does not re-fire it.
      if (!e.repeat) setInspect(true);
      return;
    }

    // H is the same shape of affordance as Space and is handled in the same
    // place, BEFORE resolveLabelKey, for the same reason: this is a keydown/keyup
    // pair the page owns, not a one-shot action the table can dispatch. It is
    // listed in LABEL_ACTIONS purely so every legend advertises it.
    if (e.key === "h" || e.key === "H") {
      if (!plain) return;
      e.preventDefault();
      if (!e.repeat) setClean(true);
      return;
    }

    // THE GEOMETRY STEP OWNS THE KEYBOARD until it is passed. Enter accepts the
    // outline, E opens the editor, X removes a false detection; the answer
    // letters are deliberately inert, because the whole point of the step is
    // that an answer cannot be given about geometry nobody has checked. A
    // pressed letter shakes and says why rather than doing nothing — a silent
    // no-op on this screen is indistinguishable from a slow save.
    // F (flag) and the two holds keep working: looking harder, and giving up on
    // an unjudgeable crop, are both legitimate at this point.
    if (inGeometry && !flag.open && !outlineOpen) {
      if (!plain || e.repeat) {
        // fall through to the action table for F/Escape only
      } else if (e.key === "Enter") {
        e.preventDefault();
        confirmGeometry();
        return;
      } else if (e.key === "e" || e.key === "E") {
        e.preventDefault();
        openOutline();
        return;
      } else if (e.key === "x" || e.key === "X") {
        e.preventDefault();
        commitMask("false_positive");
        return;
      } else if (e.key === "ArrowRight") {
        shake();
        showHint("check the outline first — Enter accepts it, E fixes it");
        return;
      } else if (resolveLabelKey(e, activeOptions.length)?.kind === "option") {
        shake();
        showHint("check the outline first — Enter accepts it, E fixes it");
        return;
      }
    }

    // OUTLINE MODE OWNS THE KEYBOARD (M4a §5.1). One hotkey layer at a time is
    // this page's standing rule; everything below this block — the answer
    // letters, F, and the arrows — is inert while an outline is open. The
    // arrows in particular: leaving the item by tape navigation mid-edit would
    // discard the draft silently, which is precisely the class of loss the
    // screen was rebuilt to prevent. Space and H are already handled above and
    // deliberately keep working, because looking harder is always allowed.
    if (outlineOpen) {
      if (!plain || e.repeat) return;
      if (e.key === "Enter") {
        e.preventDefault();
        if (maskDirty && !maskSaving) commitMask("polygon");
        else {
          shake();
          showHint("nothing changed yet — drag a point first, or press Esc");
        }
        return;
      }
      if (e.key === "r" || e.key === "R") {
        e.preventDefault();
        if (seed !== null) setDraft(seed);
        return;
      }
      if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
        shake();
        showHint("finish the outline first — Enter saves, Esc goes back");
        return;
      }
      return;
    }

    // How many digits are live right now. In the flag row it is the five
    // reasons (the flag row is one step now, so there is no state in which it
    // is open and listening to nothing); otherwise it is the ACTIVE question's
    // options, and only that question's — nothing else listens, which is the
    // whole point of the sequential flow.
    const optionCount = flag.open ? FLAG_REASONS.length : activeOptions.length;
    const hitKey = resolveLabelKey(e, optionCount);

    if (hitKey === null) {
      if (/^[a-z]$/i.test(e.key)) {
        shake();
        showHint(
          `that key isn't bound here — ${
            optionCount > 0
              ? `use ${OPTION_KEYS.slice(0, optionCount).map((k) => k.letter).join(" ")}`
              : "nothing is listening"
          }`,
        );
      }
      return;
    }

    if (hitKey.kind === "action") {
      switch (hitKey.action) {
        case "prev":
          if (!e.repeat) goPrev();
          return;
        case "next":
          if (!e.repeat) goNext();
          return;
        case "flag":
          e.preventDefault();
          if (!e.repeat && !flag.open) openFlag();
          return;
        case "close":
          return; // Escape is handled above, before the typing guard
        case "clean":
        case "inspect":
          // Unreachable: the Space keydown/keyup pair is handled further up, so
          // this table is never consulted for it. The case exists because the
          // action is in LABEL_ACTIONS (so every legend advertises it), and
          // without it the switch is non-exhaustive and the narrowing below —
          // which is what makes `hitKey.index` type-safe — silently stops
          // working. It is a compile-time guard, not dead code.
          return;
      }
    }

    if (e.repeat) return;
    if (flag.open) {
      // Two keystrokes total: F, then the reason. There is no third step any
      // more, so the pick commits rather than arming a Submit button.
      submitFlag(FLAG_REASONS[hitKey.index].reason);
      return;
    }
    const cls = activeOptions[hitKey.index];
    if (cls !== undefined && activeGroup !== null) {
      applyAnswer(activeGroup.group_key, cls.class_key, "key");
    }
  };

  const onKeyUp = (e: KeyboardEvent) => {
    if (e.key === " ") setInspect(false);
    if (e.key === "h" || e.key === "H") setClean(false);
  };

  useEffect(() => {
    keydownRef.current = onKeyDown;
    keyupRef.current = onKeyUp;
  });

  useEffect(() => {
    const down = (e: KeyboardEvent) => keydownRef.current(e);
    const up = (e: KeyboardEvent) => keyupRef.current(e);
    // Alt-tabbing away with a hold key down would otherwise leave the crop
    // enlarged, or the scrim lifted, for good. BOTH holds are released: with two
    // of them a stuck one is twice as likely, and no key the annotator can press
    // would clear it.
    const onBlur = () => {
      setInspect(false);
      setClean(false);
    };
    window.addEventListener("keydown", down, { capture: true });
    window.addEventListener("keyup", up, { capture: true });
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", down, { capture: true });
      window.removeEventListener("keyup", up, { capture: true });
      window.removeEventListener("blur", onBlur);
    };
  }, []);

  // ------------------------------------------------------------------ render

  const provenance =
    current === null ? "" : `${current.camera_id} · ${current.day ?? "unknown day"}`;

  let main: ReactNode;
  if (taxonomyError !== null) {
    main = (
      <TerminalCard title="Couldn't load the questions">
        <p className="text-[13px]" style={{ color: INK_DIM }}>
          {taxonomyError}
        </p>
        <DarkButton
          className="mt-5"
          onClick={() => {
            setTaxonomyError(null);
            getTaxonomy()
              .then(setTaxonomy)
              .catch((e: unknown) => setTaxonomyError(errMsg(e)));
          }}
        >
          Try again
        </DarkButton>
      </TerminalCard>
    );
  } else if (taxonomy === null) {
    main = <Shimmer />;
  } else if (!taxonomyUsable) {
    // Terminal state 5: the taxonomy has nothing to ask.
    main = (
      <TerminalCard title="There are no questions yet">
        <p className="text-[13px] max-w-md mx-auto" style={{ color: INK_DIM }}>
          The label taxonomy has no active classes, so there is nothing to answer about an animal
          yet.
        </p>
        {canManage ? (
          <Link
            to="/label/classes"
            className="inline-block mt-4 text-[13px] underline underline-offset-2"
            style={{ color: INK }}
          >
            Set up the questions →
          </Link>
        ) : (
          <p className="text-[12px] mt-4" style={{ color: INK_DIM }}>
            A poweruser has to set up the questions before labeling can start.
          </p>
        )}
      </TerminalCard>
    );
  } else if (stats !== null && stats.pool_total === 0) {
    // Terminal state 2: no footage has been processed at all.
    main = (
      <TerminalCard title="No footage to label yet">
        <p className="text-[13px] max-w-md mx-auto" style={{ color: INK_DIM }}>
          Nothing has been processed yet, so there are no detections to judge. New footage joins the
          labeling queue by itself as soon as a day finishes processing.
        </p>
        <Link
          to="/data"
          className="inline-block mt-4 text-[13px] underline underline-offset-2"
          style={{ color: INK }}
        >
          Upload a day of footage →
        </Link>
      </TerminalCard>
    );
  } else if (current !== null && activeGroup !== null) {
    main = (
      <div
        className="w-full max-w-[760px] rounded-xl border box-border"
        style={{ background: CARD, borderColor: HAIRLINE, padding: CARD_PAD }}
      >
        {/* The Classify|Outline toggle (M4a §5.1) sits directly above the crop —
            the one place the eye already is when the outline looks wrong. It is
            drawn at a fixed height inside the existing CROP_GAP budget so the
            crop below it does not lose a pixel. */}
        <div className="flex justify-center" style={{ marginBottom: CROP_GAP }}>
          <ModeToggle
            stage={outlineOpen || inGeometry ? "outline" : "classify"}
            // Classify cannot be selected until the geometry step is passed:
            // the sequence is the feature, and a control that let you click past
            // step 1 would make the gate a suggestion. Once passed, going BACK
            // to the outline stays available — the tape's whole ethos is that a
            // decision can be revisited, and a second look costs nothing.
            classifyDisabled={inGeometry}
            outlineDisabled={!outlineAvailable}
            onClassify={closeOutline}
            onOutline={openOutline}
          />
        </div>

        {/* The crop is the brightest region on screen (§2.2) and its size is
            subtraction, not taste (§2.3): clamp(300, 100vh - 414, 440). Space
            enlarges it by TRANSFORM so nothing below it moves. */}
        <div className="flex justify-center">
          <div
            key={current.crop_url}
            className="lbl-crop-in relative"
            style={{
              width: cropPx,
              height: cropPx,
              zIndex: inspect ? 5 : undefined,
            }}
          >
            {outlineOpen && draft !== null ? (
              // Same box, same stretch contract — only the overlay changes, so
              // the photograph does not move under the cursor at the handoff.
              <MaskEditor
                item={current}
                polygon={draft}
                onChange={setDraft}
                levels={cropLevels}
                levelIndex={zoomIndex}
                onLevelChange={setZoom}
                hidden={clean || inspect}
                onRefusedDelete={() => {
                  shake();
                  showHint("an outline needs at least three points");
                }}
                className="w-full h-full"
              />
            ) : (
              <InstanceCrop
                item={current}
                // The outline is drawn during the geometry step (it is the thing
                // being judged) and while the flag row is open (M4a §5.4 — the
                // annotator is deciding whether the detection is wrong, and that
                // is a judgement about the shape the model drew). It is NOT
                // drawn while answering: the question is about the animal, and a
                // polygon competing with the ring on every item is clutter.
                maskPreview={flag.open || inGeometry ? servedMaskCrop : null}
                hideRing={clean || inspect}
                onError={(k) => {
                  // A late error from an item already advanced past must not blank
                  // the item now on screen — that is why the key rides along.
                  if (k === tapeRef.current.items[tapeRef.current.cursor]?.instance_key) {
                    setFailedKey(k);
                  }
                }}
                className="w-full h-full"
              />
            )}
            {flaggedReason !== undefined ? <FlaggedOverlay size={cropPx} /> : null}
            {/* The zoom affordance sits ON the crop because that is the one place
                the annotator is already looking; as trailing prose in the footer
                legend it went unread. It is a hold, so it also has to say so —
                a label reading "Space" alone invites a press-and-release that
                appears to do nothing. While held it swaps to a release cue, which
                is what tells a first-time user the enlargement is theirs to end
                rather than a state they are stuck in. */}
            {!cropFailed ? (
              <div
                className="absolute left-2 bottom-2 flex items-center gap-1.5 rounded-md px-2 py-1 font-mono text-[11px] pointer-events-none select-none"
                style={{
                  background: ON_IMAGE_SCRIM,
                  color: inspect ? ON_IMAGE_ACCENT : ON_IMAGE_INK,
                  border: `1px solid ${inspect ? ON_IMAGE_ACCENT : ON_IMAGE_LINE}`,
                  transition: "color 120ms ease-out, border-color 120ms ease-out",
                }}
              >
                <ZoomGlyph />
                {inspect
                  ? "release Space"
                  : clean
                    ? "release H"
                    : "hold Space full frame · H clean · F flag"}
              </div>
            ) : null}
          </div>
        </div>

        {/* HOLD SPACE = the WHOLE frame (§2.4, revised). The square crop answers
            "what is this animal doing"; sun exposure is judged from the SURROUNDINGS,
            and a padded square is not the surroundings. The frame is a fixed overlay
            rather than a bigger crop because the two have different aspect ratios --
            letterboxing a 16:9 frame into the square box would waste most of the
            height the affordance exists to buy. Nothing under it reflows: the crop
            column keeps its geometry and simply sits beneath.
        
            The image is banner-masked SERVER-side (/api/img/label-frame), exactly
            like the crop. That is not incidental: a full frame shows MORE of the
            burned-in Brinno clock than any crop of it, and the clock time predicts
            the sun answer outright. */}
        {inspect && !cropFailed ? (
          <div
            className="fixed inset-0 z-50 flex items-center justify-center pointer-events-none"
            style={{ background: "rgba(12,14,16,0.92)" }}
          >
            {/* The peek exists to answer "where is this animal in the scene",
                so it carries the SAME marks the crop does: the box, and the
                outline when one exists. Holding H strips them, which is the
                composition the two holds were designed for — the clean full
                frame is a legitimate thing to want, and sun exposure is judged
                on unobstructed pixels. */}
            <FramePeek item={current} hideMarks={clean} />
            <div
              className="absolute left-4 bottom-4 flex items-center gap-1.5 rounded-md px-2 py-1 font-mono text-[11px]"
              style={{
                background: ON_IMAGE_SCRIM,
                color: ON_IMAGE_ACCENT,
                border: `1px solid ${ON_IMAGE_ACCENT}`,
              }}
            >
              <ZoomGlyph />
              {clean ? "release H for the outline" : "release Space · H hides the marks"}
            </div>
          </div>
        ) : null}
        
        {/* Decoded while the annotator is still answering, so the hold is instant
            rather than a grey flash on a full-resolution JPEG. Same trick the crop
            prefetch already uses; the ETag makes the overlay's own load a cache hit. */}
        {!cropFailed ? (
          <img src={current.frame_url} alt="" aria-hidden="true" className="hidden" />
        ) : null}
        
        {cropFailed ? (
          // Terminal state 3: the image is missing or the server refused it
          // (e.g. a mostly-banner crop, §4.5). F records that and moves on.
          <p role="alert" className="mt-2 text-[12px] text-center" style={{ color: ALARM }}>
            This image can't be shown — press F to flag it and move on.
          </p>
        ) : null}

        <div style={{ marginTop: CROP_GAP }}>
          {outlineOpen ? (
            <MaskPanel
              nodeCount={draft?.length ?? 0}
              iou={maskIou}
              dirty={maskDirty}
              seededFromBbox={servedMask === null}
              saving={maskSaving}
              onSave={() => commitMask("polygon")}
              onRevert={() => seed !== null && setDraft(seed)}
              onRemove={() => commitMask("false_positive")}
              onCancel={closeOutline}
            />
          ) : flag.open ? (
            <FlagRow
              onPick={submitFlag}
              onCancel={closeFlag}
              // The two repair actions (M4a §5.4). They lead the row because a
              // detection that is merely mis-outlined is repairable, and a flag
              // is the outcome for what cannot be repaired — offering the
              // structured verdicts first is what keeps flags for the cases that
              // genuinely cannot be judged.
              onFixOutline={openOutline}
              onRemoveInstance={() => commitMask("false_positive")}
              canRepair={outlineAvailable}
              // Whether there is actually an amber shape on the crop to point at.
              // A legend for something that is not drawn is worse than no legend:
              // the annotator hunts the image for it.
              hasMask={servedMaskCrop !== null}
            />
          ) : inGeometry ? (
            <GeometryPanel
              hasMask={servedMaskCrop !== null}
              saving={maskSaving}
              stepCount={steps.length + 1}
              onConfirm={confirmGeometry}
              onFix={openOutline}
              onRemove={() => commitMask("false_positive")}
            />
          ) : (
            <QuestionPanel
              group={activeGroup}
              // The questions are steps 2..n+1 of the item now: geometry is
              // step 1, and a counter that still read [1/2] would be telling the
              // annotator they are somewhere they are not. The offset moves the
              // counter without moving the per-question colours.
              stepIndex={step}
              stepOffset={1}
              stepCount={steps.length + 1}
              reserveOptions={reserveOptions}
              selectedClassKey={answers[activeGroup.group_key] ?? null}
              reviewing={reviewing}
              openDefinitionKey={openDefinitionKey}
              onSelect={(g, c) => applyAnswer(g, c, "mouse")}
              onOpenDefinition={onOpenDefinition}
              onCloseDefinition={() => setOpenDefinitionKey(null)}
              shakeNonce={shakeNonce}
            />
          )}
        </div>

        {/* Footer, 32px, fixed: the bindings are PERMANENTLY PRINTED here, which
            is CVAT's shipping alternative to a `?` overlay and what satisfies
            decision 5. Transient lines replace the legend in place rather than
            adding a row, so the tile row above never moves. */}
        <div
          className="flex items-center gap-3"
          style={{ marginTop: PANEL_GAP, height: FOOTER_H, color: INK_DIM }}
        >
          <div className="min-w-0 grow text-[11px] leading-snug truncate" role="status">
            {hint !== null ? (
              <span style={{ color: ALARM }}>{hint}</span>
            ) : savedChip ? (
              <span style={{ color: INK }}>saved ✓</span>
            ) : flaggedReason !== undefined ? (
              <span style={{ color: ALARM }}>⚑ FLAGGED — {flaggedReason.replace(/_/g, " ")}</span>
            ) : reviewing ? (
              <span>✎ reviewing — → to leave · answers already saved</span>
            ) : (
              <KeyLegend />
            )}
          </div>
          <div className="flex items-center gap-2 shrink-0">
            <SyncDot sync={queue.sync} />
            <span className="font-mono text-[11px] tabular-nums">{provenance}</span>
          </div>
        </div>
      </div>
    );
  } else if (fetchError !== null) {
    main = (
      <TerminalCard title="Couldn't fetch the queue">
        <p className="text-[13px]" style={{ color: INK_DIM }}>
          {fetchError}
        </p>
        <DarkButton
          className="mt-5"
          onClick={() => {
            setFetchError(null);
            setQueueDrained(false);
            void refill();
          }}
        >
          Try again
        </DarkButton>
      </TerminalCard>
    );
  } else if (fetching || !queueDrained) {
    main = <Shimmer />;
  } else {
    // Terminal state 1: genuinely caught up.
    main = (
      <TerminalCard title="You're caught up">
        <p className="text-[13px] max-w-md mx-auto" style={{ color: INK_DIM }}>
          Every instance that still needs your judgement has it. New footage joins the queue by
          itself, and instances other annotators haven't finished stay available to them.
        </p>
        <DarkButton
          className="mt-5"
          onClick={() => {
            setQueueDrained(false);
            void refill();
          }}
        >
          Check again
        </DarkButton>
      </TerminalCard>
    );
  }

  return (
    <div
      data-surface="label"
      style={{
        color: INK,
      }}
    >
      <style>{PAGE_CSS}</style>

      {/* The page header block collapses to one 28px strip (§2.2). No hero, no
          paragraph of instructions: they cost the crop 150px of height on the
          machine where the crop is already smallest. */}
      <div
        className="mx-auto w-full max-w-[1104px] flex items-center gap-3"
        style={{ height: STRIP_H }}
      >
        <span className="font-mono text-[11px] truncate" style={{ color: INK_DIM }}>
          {current === null ? "label" : captionFor(current)}
        </span>
        {/* Hidden, not disabled, for plain users — the same rule as every other
            poweruser affordance (§5.1). */}
        {canManage ? (
          <Link
            to="/label/classes"
            className="ml-auto shrink-0 font-mono text-[11px] uppercase tracking-[0.14em] hover:opacity-100 opacity-70"
            style={{ color: INK_DIM }}
          >
            Manage classes →
          </Link>
        ) : null}
      </div>

      {staleNotice !== null ? (
        <div className="mx-auto w-full max-w-[1104px] mt-2">
          <p
            role="alert"
            className="text-[12px] leading-snug rounded-lg border px-3 py-2"
            style={{ color: INK, borderColor: ALARM, background: CARD }}
          >
            {staleNotice}{" "}
            <button
              type="button"
              onClick={() => setStaleNotice(null)}
              className="underline underline-offset-2 cursor-pointer"
            >
              dismiss
            </button>
          </p>
        </div>
      ) : null}

      {/* 760 + 24 + 320 = 1104. Below 1100px the side panel drops beneath the
          fold and the main column keeps its geometry unchanged — nothing in the
          panel is needed to answer an item (§2.4). */}
      <div className="mx-auto w-full max-w-[1104px] mt-2 flex flex-wrap gap-6 justify-center items-start">
        <div className="min-w-0 grow basis-[760px] max-w-[760px] flex justify-center">{main}</div>
        <div className="w-[320px] shrink-0 grow-0">
          <LabelProgress
            groups={groups}
            stream={{
              // The SERVER's count, not the tape's. `tape.frontier` is how many
              // items this MOUNT has answered, so it is 0 after every reload —
              // which showed an annotator who had genuinely labeled work a flat
              // 0, next to a store that still held it. The tape is the right
              // thing for navigation and the wrong thing for a scoreboard.
              done: stats?.my_labeled ?? 0,
              // ...plus what the queue can still serve ME. A corpus-coverage bar
              // was removed on purpose (§7) — it is a number the annotator
              // cannot move.
              target: (stats?.my_labeled ?? 0) + (stats?.remaining ?? 0),
              recentMs,
              flags: stats?.my_skipped ?? flagCount,
              // Always zero: decision 6 makes the written explanation mandatory
              // at flag time, so there is no deferred-notes queue to owe.
              pendingNotes: 0,
            }}
          />
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ pieces */

/** STEP 1 OF THE ITEM: check the outline before answering anything about it.
 *
 * Not a dialog and not a mode — the same rectangle the questions use, at the
 * same PANEL_H, so passing this step is the same gesture as answering one: look
 * at the crop, press one key. Enter is the overwhelmingly common answer and is
 * therefore the one that needs no aiming; E and X are the exceptions and are
 * spelled out on their tiles.
 *
 * The three outcomes are the three stored verdicts (labels_db.MASK_EDIT_KINDS),
 * so this step is M4's pass-A triage collected inside the classification flow:
 * accepting is a measurement, not a no-op.
 */
function GeometryPanel({
  hasMask,
  saving,
  stepCount,
  onConfirm,
  onFix,
  onRemove,
}: {
  hasMask: boolean;
  saving: boolean;
  stepCount: number;
  onConfirm: () => void;
  onFix: () => void;
  onRemove: () => void;
}) {
  /* The question is PRESENCE first, geometry second — "is there a cow in this
     box", not "is this the right cow". There is no other cow to be right about:
     the detector proposed a box, and step 1 asks whether it found an animal and
     drew it correctly. The three answers are the three stored verdicts
     (labels_db.MASK_EDIT_KINDS): ok, imprecise-so-corrected, not_a_cow. */
  const CHOICES = [
    {
      key: "Enter",
      label: "Yes",
      hint: hasMask ? "outline is right" : "box is right",
      onClick: onConfirm,
      primary: true,
    },
    {
      key: "E",
      label: "Yes, but…",
      hint: hasMask ? "fix the outline" : "fix the box",
      onClick: onFix,
      primary: false,
    },
    { key: "X", label: "No cow", hint: "remove it", onClick: onRemove, primary: false },
  ];
  return (
    <div
      className="box-border w-full flex flex-col"
      style={{
        background: TILE,
        borderLeft: `3px solid ${ON_IMAGE_ACCENT}`,
        borderRadius: 10,
        padding: 10,
        height: PANEL_H,
        color: INK,
      }}
    >
      <div className="flex items-center gap-2" style={{ height: 24 }}>
        <span className="text-[13px] font-semibold uppercase tracking-[0.08em] leading-none">
          Is there a cow in the box?
        </span>
        <span className="ml-auto font-mono text-[11px] tabular-nums" style={{ color: INK_DIM }}>
          {`[1/${stepCount}]`}
        </span>
      </div>
      <div
        className="grid"
        style={{
          marginTop: 8,
          gridAutoFlow: "column",
          gridAutoColumns: `${TILE_W}px`,
          gap: TILE_GAP,
          justifyContent: "start",
          height: TILE_H,
        }}
      >
        {CHOICES.map((c) => (
          <button
            key={c.key}
            type="button"
            onClick={c.onClick}
            disabled={saving}
            className="flex flex-col items-center rounded-lg border box-border cursor-pointer
                       disabled:opacity-40 disabled:cursor-default
                       focus-visible:outline-2 focus-visible:outline-offset-2"
            style={{
              width: TILE_W,
              height: TILE_H,
              padding: 7,
              background: CARD,
              borderColor: c.primary ? ON_IMAGE_ACCENT : HAIRLINE,
              color: INK,
              outlineColor: INK,
            }}
          >
            <span className="flex w-full items-center" style={{ height: 20 }}>
              <span
                className="grid place-items-center rounded-md px-1 font-mono text-[11px] font-bold leading-none"
                style={{
                  minWidth: 20,
                  height: 20,
                  color: c.primary ? "#14161A" : INK,
                  background: c.primary ? ON_IMAGE_ACCENT : HAIRLINE,
                }}
              >
                {c.key}
              </span>
            </span>
            <span className="grid w-full flex-1 place-items-center text-[12px] leading-[14px] text-center px-1">
              {c.label}
            </span>
            <span className="w-full text-center text-[11px] leading-[13px]" style={{ color: INK_DIM }}>
              {c.hint}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

/** Hold-Space's whole frame, with the instance marked on it.
 *
 * WHY THE WRAPPER SHRINK-WRAPS THE IMAGE. The frame is sized by `max-w`/`max-h`
 * against the viewport, so its rendered rectangle is not known until it loads
 * and is a different shape on every screen. An SVG absolutely positioned over
 * the flex CONTAINER would therefore be letterboxed relative to the photograph
 * and every mark would be offset. The inline-block wrapper is exactly the
 * image's rendered box, so an `inset-0` SVG inside it covers the pixels and
 * nothing else.
 *
 * WHY THE viewBox IS `frame_w x frame_h` AND NOT THE IMAGE'S NATURAL SIZE.
 * /api/img/label-frame serves the frame DOWNSCALED (labeling.render_frame caps
 * it at FRAME_MAX_WIDTH), while `bbox` and `mask_frame` are in the frame's
 * ORIGINAL pixels. The served image is a uniform scale of the original, so the
 * original dimensions as the viewBox — stretched over the rendered box with
 * `preserveAspectRatio="none"` — map original px onto displayed px correctly
 * whatever that scale turned out to be. Using naturalWidth here would silently
 * misplace every mark by the downscale factor, which is worse than drawing
 * nothing: it would point confidently at the wrong animal.
 */
function FramePeek({ item, hideMarks }: { item: LabelItem; hideMarks: boolean }) {
  const fw = item.frame_w ?? 0;
  const fh = item.frame_h ?? 0;
  // No dimensions (an older server, or the JPEG has gone) -> the photo alone.
  // Guessing would be worse than not drawing.
  const canMark = fw > 0 && fh > 0;
  // Same rule as the crop's ring (InstanceCrop.ringFor): once the annotator has
  // corrected the outline, the box drawn is the extent of THEIR shape, not the
  // detector's. Display only — the stored bbox is key material and never moves.
  const poly0 = item.mask_seed === "edit" ? item.mask_frame : null;
  const [x1, y1, x2, y2] =
    poly0 && poly0.length >= 3
      ? [
          Math.min(...poly0.map((p) => p[0])),
          Math.min(...poly0.map((p) => p[1])),
          Math.max(...poly0.map((p) => p[0])),
          Math.max(...poly0.map((p) => p[1])),
        ]
      : item.bbox;
  const bw = Math.max(x2 - x1, 1);
  const bh = Math.max(y2 - y1, 1);
  const poly = item.mask_frame ?? null;
  const d =
    poly !== null && poly.length >= 3
      ? `M${poly.map(([x, y]) => `${x.toFixed(1)} ${y.toFixed(1)}`).join("L")}Z`
      : null;
  // Even-odd hole at the box: on a 4K frame of a full pen the ring alone is a
  // thin rectangle among many animals, and the eye does not find it. The scrim
  // is light — this view is also how sun exposure is judged, so the surroundings
  // must stay readable — and H removes it outright.
  const scrim = `M0 0H${fw}V${fh}H0Z M${x1} ${y1}H${x1 + bw}V${y1 + bh}H${x1}Z`;

  // A distant cow is a ~25px box in a 1920px frame: the ring is *drawn*, but the
  // eye still has to sweep the whole pen to find it, which is precisely the cost
  // this peek exists to remove. Below this fraction of the frame, four leader
  // lines run in from the edges and stop short of the animal — the eye lands on
  // the intersection without anything being painted over the pixels being judged.
  // A big, obvious box gets no leaders: they would be clutter around something
  // already impossible to miss.
  const LOCATOR_MAX_FRAC = 0.08;
  const small = canMark && Math.max(bw / fw, bh / fh) < LOCATOR_MAX_FRAC;
  const cx = x1 + bw / 2;
  const cy = y1 + bh / 2;
  const gap = Math.max(bw, bh) * 1.4;
  const leaders = small
    ? [
        `M${cx} 0V${Math.max(0, cy - gap)}`,
        `M${cx} ${Math.min(fh, cy + gap)}V${fh}`,
        `M0 ${cy}H${Math.max(0, cx - gap)}`,
        `M${Math.min(fw, cx + gap)} ${cy}H${fw}`,
      ].join(" ")
    : null;

  return (
    <div className="relative inline-block leading-none">
      <img
        src={item.frame_url}
        alt=""
        className="block max-w-[94vw] max-h-[88vh]"
        style={{ boxShadow: "0 0 0 1px rgba(255,255,255,0.22)" }}
      />
      {canMark ? (
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none transition-opacity duration-100"
          viewBox={`0 0 ${fw} ${fh}`}
          preserveAspectRatio="none"
          style={{ opacity: hideMarks ? 0 : 1 }}
          aria-hidden="true"
        >
          <path d={scrim} fill="#000" fillOpacity={0.35} fillRule="evenodd" />
          {leaders !== null ? (
            <>
              <path
                d={leaders}
                stroke="#000"
                strokeOpacity={0.5}
                strokeWidth={3}
                vectorEffect="non-scaling-stroke"
                fill="none"
              />
              <path
                d={leaders}
                stroke="#fff"
                strokeOpacity={0.75}
                strokeWidth={1}
                vectorEffect="non-scaling-stroke"
                fill="none"
              />
            </>
          ) : null}
          {/* Dark under light, the ring's own rule: one colour disappears
              against either a sunlit flank or panel shade. */}
          <rect
            x={x1}
            y={y1}
            width={bw}
            height={bh}
            fill="none"
            stroke="#000"
            strokeOpacity={0.55}
            strokeWidth={4}
            vectorEffect="non-scaling-stroke"
          />
          <rect
            x={x1}
            y={y1}
            width={bw}
            height={bh}
            fill="none"
            stroke="#fff"
            strokeWidth={2}
            vectorEffect="non-scaling-stroke"
          />
          {d !== null ? (
            <>
              <path
                d={d}
                fill="rgba(240,180,96,0.20)"
                stroke="#000"
                strokeOpacity={0.55}
                strokeWidth={3.5}
                vectorEffect="non-scaling-stroke"
              />
              <path
                d={d}
                fill="none"
                stroke={ON_IMAGE_ACCENT}
                strokeWidth={2}
                vectorEffect="non-scaling-stroke"
              />
            </>
          ) : null}
        </svg>
      ) : null}
    </div>
  );
}

/** A compact action inside the flag row's header line. Sized to the 24px header
    so adding these cost the panel no height at all (see FlagRow's comment). */
function RepairChip({ children, onClick }: { children: ReactNode; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="inline-flex items-center h-[22px] px-2 rounded-md border box-border text-[11px]
                 whitespace-nowrap cursor-pointer transition-opacity duration-150 hover:opacity-80
                 focus-visible:outline focus-visible:outline-2"
      style={{
        background: "var(--lbl-card, #1E2124)",
        borderColor: ON_IMAGE_ACCENT,
        color: ON_IMAGE_ACCENT,
        outlineColor: INK,
      }}
    >
      {children}
    </button>
  );
}

/** The Classify|Outline toggle (M4a §5.1).
 *
 * A segmented control of two buttons rather than a checkbox or a switch: both
 * states are named, which is what makes it obvious that Outline is a place you
 * can come back from — the single most important thing to communicate about a
 * mode on a screen whose whole discipline is "no mode you can be stranded in".
 *
 * Disabled rather than hidden when there is nothing to trace (§5.5): a control
 * that appears only sometimes reads as flaky, and the title says why.
 */
function ModeToggle({
  stage,
  classifyDisabled,
  outlineDisabled,
  onClassify,
  onOutline,
}: {
  stage: "outline" | "classify";
  classifyDisabled: boolean;
  outlineDisabled: boolean;
  onClassify: () => void;
  onOutline: () => void;
}) {
  return (
    <div
      className="inline-flex items-center rounded-full border overflow-hidden"
      style={{ height: TOGGLE_H, borderColor: HAIRLINE, background: CARD }}
      role="group"
      aria-label="annotation step"
    >
      <ModeButton
        active={stage === "outline"}
        disabled={outlineDisabled}
        onClick={onOutline}
        title={outlineDisabled ? "there are no pixels to check on this one" : "check the outline"}
      >
        1 · Outline
      </ModeButton>
      <ModeButton
        active={stage === "classify"}
        disabled={classifyDisabled}
        onClick={onClassify}
        title={classifyDisabled ? "check the outline first" : "answer the questions"}
      >
        2 · Classify
      </ModeButton>
    </div>
  );
}

function ModeButton({
  children,
  active,
  disabled,
  onClick,
  title,
}: {
  children: ReactNode;
  active: boolean;
  disabled: boolean;
  onClick: () => void;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-pressed={active}
      className="h-full px-3 font-mono text-[11px] uppercase tracking-[0.12em] cursor-pointer
                 transition-opacity duration-150 disabled:opacity-40 disabled:cursor-default
                 focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2"
      style={{
        background: active ? ON_IMAGE_ACCENT : "transparent",
        color: active ? "#14161A" : INK_DIM,
        outlineColor: INK,
      }}
    >
      {children}
    </button>
  );
}

/** One big card per terminal state — same surface, different next action. The
    house `Card` is painted in the light theme and is deliberately not reused
    here; restyling it would follow onto every other page (§2.2). */
function TerminalCard({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div
      className="w-full max-w-[760px] rounded-xl border box-border p-8 text-center"
      style={{ background: CARD, borderColor: HAIRLINE }}
    >
      <h2 className="font-display text-2xl leading-tight" style={{ color: INK }}>
        {title}
      </h2>
      <div className="mt-3">{children}</div>
    </div>
  );
}

function DarkButton({
  children,
  onClick,
  disabled,
  className,
}: {
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={
        "inline-flex items-center h-9 px-3.5 rounded-lg border box-border text-[13px] cursor-pointer " +
        "transition-opacity duration-150 hover:opacity-80 disabled:opacity-40 disabled:cursor-default " +
        "focus-visible:outline focus-visible:outline-2 " +
        (className ?? "")
      }
      style={{ background: TILE, borderColor: HAIRLINE, color: INK, outlineColor: INK }}
    >
      {children}
    </button>
  );
}

function Shimmer() {
  return (
    <div
      className="animate-shimmer w-full max-w-[760px] rounded-xl border"
      style={{ height: 520, background: CARD, borderColor: HAIRLINE }}
    />
  );
}

/** The permanent legend (§2.4). Every entry — including the Space hold — comes
    from LABEL_ACTIONS and prints its own `short`, so it cannot drift from what
    the handler actually does. The previous version mapped action -> word with a
    ternary that fell through to "next", so any binding added to the table would
    have been advertised under the wrong name. */
function KeyLegend() {
  // "close" has never been listed: Escape is discoverable from the thing it
  // closes. "inspect", "clean" and "flag" are not listed EITHER, because the
  // crop already carries them — the chip sits on the photograph, which is where
  // the eye is when any of the three is worth reaching for, and printing them
  // again two rows down was the same sentence twice on one card. What is left
  // here is exactly the tape: back and next.
  const HELD_ON_CROP = new Set(["inspect", "clean", "flag"]);
  const shown = LABEL_ACTIONS.filter(
    (a) => a.action !== "close" && !HELD_ON_CROP.has(a.action),
  );
  return (
    <span>
      {shown.map((a, i) => (
        <span key={a.action}>
          {i > 0 ? " · " : ""}
          <span
            className="inline-block rounded px-1 font-mono"
            style={{ border: `1px solid ${HAIRLINE}`, color: INK }}
          >
            {a.label}
          </span>{" "}
          {a.short}
        </span>
      ))}
    </span>
  );
}

/** §5.5: filled dot = everything flushed, hollow dot + count = n pending,
    hollow dot + a 1.2s pulse = retrying. Shape and motion, never colour, and
    never a button — it is a read-out, and it never blocks anything. */
function SyncDot({ sync }: { sync: AnswerQueueSync }) {
  const flushed = sync.pending === 0;
  const title = flushed
    ? "all answers saved"
    : sync.retrying
      ? `${sync.pending} waiting — retrying`
      : `${sync.pending} saving`;
  return (
    <span className="flex items-center gap-1.5 font-mono text-[11px] tabular-nums" title={title}>
      <span
        className={sync.retrying ? "lbl-sync-retry" : undefined}
        style={{
          width: 8,
          height: 8,
          borderRadius: 999,
          border: `1.5px solid ${INK_DIM}`,
          background: flushed ? INK_DIM : "transparent",
        }}
        aria-hidden="true"
      />
      <span>{flushed ? "synced" : sync.pending}</span>
      {sync.dropped > 0 ? <span style={{ color: ALARM }}>· {sync.dropped} unsaved</span> : null}
    </span>
  );
}

/** §5.4: a flagged item carries three channels and NO hue — a flag must never
    read as a sun class. The dash is drawn as SVG because CSS outlines cannot
    express a 6/4 dash, and both layers are painted over the crop so neither
    changes its box. */
/** Day + camera, and the dataset id ONLY when it adds something.
 *
 * A dataset defaults to its capture day as its id, so the obvious
 * `dataset · camera · day` rendered "2025-07-03 · camera_05 · 2025-07-03" for
 * every ordinary day — the same string twice, which reads as a bug and costs the
 * one strip of context the annotator has. The id survives here only when it was
 * deliberately overridden (a same-day re-shoot), which is exactly when knowing
 * which package you are labelling matters. Still no clock time, ever: it would
 * hand over the sun-exposure answer that the crop's masked banner hides. */
function captionFor(item: LabelItem): string {
  const day = item.day ?? "unknown day";
  const parts = [day, item.camera_id];
  if (item.dataset_id !== null && item.dataset_id !== item.day) parts.push(item.dataset_id);
  return parts.join(" · ");
}

/** Four corners pulling apart — "this gets bigger". Drawn rather than typed:
    a magnifier emoji renders differently per platform and reads as "search". */
function ZoomGlyph() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M9 3H3v6M15 3h6v6M9 21H3v-6M15 21h6v-6" />
    </svg>
  );
}

function FlaggedOverlay({ size }: { size: number }) {
  return (
    <>
      {/* Sized in real pixels rather than percentages: a 6/4 dash is a
          measurement, and a viewBox that scales would turn it into whatever the
          crop's height happens to make it. */}
      <svg
        className="absolute inset-0 pointer-events-none"
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
        aria-hidden="true"
      >
        <rect
          x={1.5}
          y={1.5}
          width={Math.max(0, size - 3)}
          height={Math.max(0, size - 3)}
          fill="none"
          stroke={ALARM}
          strokeWidth="3"
          strokeDasharray="6 4"
          rx="10"
        />
      </svg>
      <div
        className="absolute left-0 right-0 top-0 pointer-events-none"
        style={{
          height: 20,
          backgroundImage: `repeating-linear-gradient(45deg, ${ALARM} 0 2px, transparent 2px 4px)`,
          opacity: 0.75,
        }}
        aria-hidden="true"
      />
    </>
  );
}

/* FLAG, §3.9. The reasons replace the TILE ROW IN PLACE — same rectangle, same
   badges, same rhythm — rather than opening a modal over the crop: a
   558-participant AMT study found sequences of distinct task types measurably
   hurt classification engagement, and a dialog over the animal is exactly such a
   switch. The reason is asked at the pixels, where `multiple_cows` versus
   `occluded` is still visible.

   ONE STEP. The mandatory written explanation is gone, so picking a reason IS
   flagging: F then a letter, two keystrokes, no Submit button and no textarea.
   What that costs is a crop broken in a way the six codes do not cover — it now
   arrives as `other` with nothing attached. What bought it is that the codes are
   countable where prose was not, and that an inspectable mask will answer
   "what was wrong with it" better than a sentence typed at 3s/item ever did. */
function FlagRow({
  onPick,
  onCancel,
  onFixOutline,
  onRemoveInstance,
  canRepair,
  hasMask,
}: {
  onPick: (reason: LabelSkipReason) => void;
  onCancel: () => void;
  onFixOutline: () => void;
  onRemoveInstance: () => void;
  canRepair: boolean;
  hasMask: boolean;
}) {
  return (
    <div
      className="box-border w-full"
      style={{
        // The neutral alarm field (§5.4): no hue at all, so a flag can never be
        // mistaken for a sun answer.
        background: TILE,
        borderLeft: `3px solid ${ALARM}`,
        borderRadius: 10,
        padding: 10,
        height: PANEL_H,
        color: INK,
      }}
    >
      {/* The header row carries the two repair actions (M4a §5.4). They live
          HERE rather than in a strip of their own because PANEL_H is exactly
          consumed by the header and the tile row — an extra 32px band would
          overflow the rectangle whose height the crop above is sized against.
          They are ACTIONS, not two more reasons: they write structured verdicts
          about the segmentation rather than prose about the crop, and the mask
          is drawn on the crop behind this row so both are decidable without
          leaving. */}
      <div className="flex items-center gap-2" style={{ height: 24 }}>
        <span className="text-[13px] font-semibold uppercase tracking-[0.08em] leading-none">
          ⚑ Flag — why?
        </span>
        {canRepair ? (
          <span className="flex items-center gap-1.5 ml-2">
            <RepairChip onClick={onFixOutline}>✎ Fix outline</RepairChip>
            <RepairChip onClick={onRemoveInstance}>✕ Not a cow</RepairChip>
          </span>
        ) : null}
        <span className="ml-auto flex items-center gap-2 text-[11px] shrink-0"
              style={{ color: INK_DIM }}>
          {hasMask ? <span>amber = what the model found</span> : null}
          {OPTION_KEYS.slice(0, FLAG_REASONS.length).map((k) => k.letter).join(" ")}
          {/* A SIBLING of the reason tiles, never a child of one: a button inside
              a button is invalid HTML, and here it would also mean that clicking
              "cancel" flagged the instance. */}
          <button
            type="button"
            onClick={onCancel}
            className="underline underline-offset-2 cursor-pointer"
            style={{ color: INK_DIM }}
          >
            Esc cancels
          </button>
        </span>
      </div>
      <div
        className="grid"
        style={{
          marginTop: 8,
          gridAutoFlow: "column",
          gridAutoColumns: `${TILE_W}px`,
          gap: TILE_GAP,
          justifyContent: "start",
          height: TILE_H,
        }}
      >
        {FLAG_REASONS.map((r, i) => (
          <button
            key={r.reason}
            type="button"
            onClick={() => onPick(r.reason)}
            className="flex flex-col items-center rounded-lg border box-border cursor-pointer
                       focus-visible:outline-2 focus-visible:outline-offset-2"
            style={{
              width: TILE_W,
              height: TILE_H,
              padding: 7,
              background: "var(--lbl-card, #1E2124)",
              borderColor: HAIRLINE,
              color: INK,
              outlineColor: INK,
            }}
          >
            <span className="flex w-full items-center" style={{ height: 20 }}>
              <span
                className="grid place-items-center rounded-md font-mono text-[12px] font-bold leading-none"
                style={{ width: 20, height: 20, color: ALARM, background: HAIRLINE }}
              >
                {/* The SAME letter table the answers use — the row is modal, so
                    reusing them costs nothing and keeps one set of keys to learn. */}
                {OPTION_KEYS[i]?.letter ?? ""}
              </span>
            </span>
            <span className="grid w-full flex-1 place-items-center">
              <ClassIcon name={r.icon} />
            </span>
            <span
              className="w-full text-center text-[12px] leading-[14px] break-words"
              style={{ height: 28 }}
            >
              {r.label}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}
