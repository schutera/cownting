import type { ReactNode } from "react";
import type { LabelGroup } from "../lib/types";
import { LABEL_ACTIONS, optionKeysFor, visibleGroups } from "../lib/labelKeys";
import { SplitBar } from "./ui";

/* The Label route's side panel (UX §2.5).
 *
 * THE PANEL IS NEVER NEEDED TO ANSWER AN ITEM. Everything required for the
 * per-item loop — crop, active question, tiles, step counter, flag affordance,
 * sync dot — lives in the 760 px main column, which is why §2.4 can drop this
 * whole column below the fold under 1100 px without touching the geometry the
 * redesign is about. What is here is reference and feedback: how the shift is
 * going, whether the annotator is drifting, what a class means, where they just
 * were, and what the keys do.
 *
 * Four things the old panel did are gone on purpose:
 *   - the corpus-coverage hero bar (§7): with targets_per_instance = 2 it is a
 *     number the annotator cannot move, and a progress bar you cannot move is
 *     just a reminder that the work is long. Replaced by MY stream.
 *   - `my_median_ms` as an all-time scalar (§7): an average over every session
 *     ever cannot show today going badly. Replaced by a rolling rate over the
 *     last ROLLING_WINDOW items.
 *   - both preference checkboxes and their localStorage (decisions 4 and 5):
 *     auto-save is always on and definitions open from their info dot, so there
 *     is nothing left to prefer. There are no preferences on this route at all.
 *   - `?`-sheet parity. There is no `?` sheet (decision 5), which makes the KEYS
 *     block below the ONLY place the bindings are written down. It is therefore
 *     derived from lib/labelKeys — the same module the page's key handler and
 *     the tile badges read — so it cannot drift when a poweruser edits the
 *     taxonomy and a question gains a fifth answer.
 */

// The rolling window for the throughput read-out. Exported because the label
// prints it ("last 50") and the arithmetic uses it — one constant, no drift.
export const ROLLING_WINDOW = 50;

// Below this many samples a rate is noise dressed as a measurement; the block
// says it is still measuring instead of printing 3.2 items/min off two items.
const MIN_RATE_SAMPLE = 5;

// The route's dark surface tokens (§2.2). Declared by the `data-surface="label"`
// wrapper; the literal fallbacks keep the panel readable if it is ever rendered
// outside it. ui.tsx's Panel/Kbd/Divider are hardcoded to the light app theme
// (bg-surface, text-near-black, bg-border) and are deliberately NOT reused here
// — restyling them would change every other page, which §2.2 forbids.
const CARD = "var(--lbl-card, #1E2124)";
const TILE = "var(--lbl-tile, #262A2E)";
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const HAIRLINE = "var(--lbl-line, rgba(255, 255, 255, 0.09))";

/** Today's numbers for the annotator's own stream. Deliberately not LabelStats:
    that is a corpus-wide, all-time shape, and every figure here is per-session
    and owned by the page (the queue, the flag queue and the per-item clock all
    live there). */
export interface LabelStream {
  /** Instances answered today. */
  doneToday: number;
  /** What today can still yield — done plus what the queue can still serve.
      0 renders the bar empty rather than dividing by zero. */
  targetToday: number;
  /** Per-instance ACTIVE durations in ms, newest first (Label.tsx's
      activeElapsedMs, which banks tab-away time). Only the first
      ROLLING_WINDOW are read; the panel owns the arithmetic so the printed
      window and the computed window are the same number. */
  recentMs: readonly number[];
  /** Items flagged today (§3.9). */
  flags: number;
  /** Flagged items still owing their written explanation. The session cannot
      be marked complete while this is non-zero. */
  pendingNotes: number;
}

export interface LabelProgressProps {
  /** The live taxonomy's groups. Ordering and visibility are resolved here
      through lib/labelKeys, exactly as the tile row does it, so the letters in
      the legend are the letters on the tiles. Empty while the first fetch is in
      flight — the keys block says so rather than rendering nothing. */
  groups: LabelGroup[];
  stream: LabelStream;
  /** Opens the pending-notes queue (§3.9.3). Omitted -> the count is plain
      text, so the panel still renders before that page exists. */
  onOpenPendingNotes?: () => void;
  className?: string;
}

/* items/min over the last ROLLING_WINDOW instances. n/Σt rather than 60000/mean
   of a mean — same value, but it degrades sanely when one item was left open
   over a coffee break instead of being dominated by it. */
function itemsPerMinute(recentMs: readonly number[]): number | null {
  const window = recentMs.slice(0, ROLLING_WINDOW).filter((ms) => ms > 0);
  if (window.length < MIN_RATE_SAMPLE) return null;
  const total = window.reduce((a, b) => a + b, 0);
  return total > 0 ? (window.length * 60_000) / total : null;
}

function pct(fraction: number): string {
  return `${Math.round(fraction * 100)}%`;
}

function BlockHeading({ children }: { children: ReactNode }) {
  return (
    <h2
      className="text-[10px] font-semibold uppercase tracking-[0.14em] leading-none"
      style={{ color: INK_DIM }}
    >
      {children}
    </h2>
  );
}

function Block({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section
      className="px-4 py-3.5 border-t first:border-t-0 border-solid"
      style={{ borderColor: HAIRLINE }}
    >
      <BlockHeading>{title}</BlockHeading>
      <div className="mt-2.5">{children}</div>
    </section>
  );
}

/* A keycap for the dark surface. ui.tsx's <Kbd> is the shared one, but it is
   painted in the light theme's tokens (bg-surface-sunk, text-near-black); using
   it here would either look wrong or force a restyle that would follow it onto
   the manual and the taxonomy editor. Same shape, this route's colours. */
function LabelKbd({ children }: { children: ReactNode }) {
  return (
    <kbd
      className="inline-grid place-items-center min-w-[20px] h-5 px-1 rounded border box-border
                 font-mono text-[10px] leading-none"
      style={{ background: TILE, borderColor: HAIRLINE, color: INK }}
    >
      {children}
    </kbd>
  );
}

function LegendRow({ label, badges }: { label: string; badges: readonly string[] }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-[11.5px] leading-snug" style={{ color: INK_DIM }}>
        {label}
      </span>
      <span className="flex items-center gap-1 shrink-0">
        {badges.map((b) => (
          <LabelKbd key={b}>{b}</LabelKbd>
        ))}
      </span>
    </div>
  );
}

export function LabelProgress({
  groups,
  stream,
  onOpenPendingNotes,
  className,
}: LabelProgressProps) {
  const shown = visibleGroups(groups);
  const rate = itemsPerMinute(stream.recentMs);
  const fraction = stream.targetToday > 0 ? stream.doneToday / stream.targetToday : 0;
  // Space is a HOLD, not an action with a class binding, so LABEL_ACTIONS does
  // not carry it (§3.7). Print it here anyway — this legend is the only place
  // the bindings are written down — but check first, so that if the key map
  // ever gains it the legend lists it once rather than twice.
  const spaceIsBound = LABEL_ACTIONS.some((a) => a.key === " " || a.key === "Space");

  return (
    <aside
      className={
        "w-full rounded-xl border box-border overflow-hidden lg:sticky" +
        " lg:overflow-y-auto lg:overscroll-contain" +
        (className ? " " + className : "")
      }
      style={{
        background: CARD,
        borderColor: HAIRLINE,
        color: INK,
        // §2.4 gives the sticky offset literally. Set here rather than as a
        // utility class so the page does not have to restate it — it owns the
        // grid and the under-1100px drop, not this panel's parking spot.
        top: "calc(var(--app-header-h) + 16px)",
        // The panel is reference, never part of the per-item loop (§2.5) — but it
        // is naturally taller than a 1366x768 laptop, and because it shares the
        // grid row with the main column its height was setting the PAGE height.
        // That broke §2.1's one rule: the crop shrank to fit correctly, and the
        // annotator still had to scroll, because the column beside it was long.
        // Capping it to the viewport and letting it scroll internally keeps the
        // answering loop fixed on screen; overscroll-contain stops a flick inside
        // the panel from dragging the page once it bottoms out.
        // The surface above is clipped to the viewport, so this only has to fit
        // inside it: header + the 16px sticky offset + a 16px gutter below.
        maxHeight: "calc(100vh - var(--app-header-h) - 32px)",
      }}
    >
      <Block title="My stream">
        <div className="flex items-baseline gap-1.5">
          <span className="font-mono text-[20px] tabular-nums leading-none" style={{ color: INK }}>
            {stream.doneToday.toLocaleString()}
          </span>
          <span className="text-[12px]" style={{ color: INK_DIM }}>
            / {stream.targetToday.toLocaleString()} today
          </span>
          <span className="ml-auto font-mono text-[11px] tabular-nums" style={{ color: INK_DIM }}>
            {pct(fraction)}
          </span>
        </div>
        <div className="mt-2">
          <SplitBar fraction={fraction} leftColor={INK} rightColor={TILE} />
        </div>
        <p className="text-[11.5px] leading-snug mt-2" style={{ color: INK_DIM }}>
          {rate === null
            ? `measuring rate… (last ${ROLLING_WINDOW})`
            : `${rate.toFixed(1)} items/min (last ${ROLLING_WINDOW})`}
        </p>
        <p className="text-[11.5px] leading-snug mt-0.5" style={{ color: INK_DIM }}>
          {stream.flags.toLocaleString()} {stream.flags === 1 ? "flag" : "flags"}
          {" · "}
          {stream.pendingNotes > 0 && onOpenPendingNotes !== undefined ? (
            <button
              type="button"
              onClick={onOpenPendingNotes}
              className="underline underline-offset-2 cursor-pointer focus-visible:outline focus-visible:outline-2"
              style={{ color: INK, outlineColor: INK }}
            >
              {stream.pendingNotes.toLocaleString()} pending{" "}
              {stream.pendingNotes === 1 ? "note" : "notes"}
            </button>
          ) : (
            <>
              {stream.pendingNotes.toLocaleString()} pending{" "}
              {stream.pendingNotes === 1 ? "note" : "notes"}
            </>
          )}
        </p>
      </Block>



      <Block title="Keys">
        <div className="flex flex-col gap-1.5">
          {shown.length === 0 ? (
            <p className="text-[11.5px]" style={{ color: INK_DIM }}>
              waiting for the taxonomy…
            </p>
          ) : (
            <>
              {shown.map((g) => (
                <LegendRow
                  key={g.group_key}
                  label={g.name}
                  badges={optionKeysFor(g)
                    .map((o) => o.label)
                    .filter((l) => l !== "")}
                />
              ))}
              {/* The one thing the letters cannot say for themselves: they are
                  rebound on every handoff. Key 2 is Direct sun in one state and
                  Lying in the next — a varied mapping, which Schneider &
                  Shiffrin show never becomes automatic, so the display has to
                  carry the cue the annotator's memory will not. */}
              <p className="text-[10.5px] leading-snug mt-0.5 mb-1" style={{ color: INK_DIM }}>
                Only the question on screen listens — the letters rebind when it hands off.
              </p>
            </>
          )}
          {LABEL_ACTIONS.map((a) => (
            <LegendRow key={a.action} label={a.hint} badges={[a.label]} />
          ))}
          {spaceIsBound ? null : (
            <LegendRow label="hold — show the whole frame" badges={["Space"]} />
          )}
        </div>
      </Block>
    </aside>
  );
}
