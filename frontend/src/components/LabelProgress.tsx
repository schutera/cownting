import type { ReactNode } from "react";
import type { LabelClassCount, LabelGroup, LabelGroupConsensus } from "../lib/types";
import {
  LABEL_ACTIONS,
  optionKeysFor,
  visibleClasses,
  visibleGroups,
} from "../lib/labelKeys";
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
// Two tones the palette did not need until there were bars to draw. INK_BAR is
// the unconfirmed mass — present, but visibly not the same claim as the INK
// segment nested inside it. INK_FAINT is for a row that is real but inert: a
// class sitting at zero, or one the taxonomy has retired.
const INK_BAR = "var(--lbl-ink-bar, rgba(232, 234, 236, 0.30))";
const INK_FAINT = "var(--lbl-ink-faint, #6B7278)";
const HAIRLINE = "var(--lbl-line, rgba(255, 255, 255, 0.09))";

/** Today's numbers for the annotator's own stream. Deliberately not LabelStats:
    that is a corpus-wide, all-time shape, and every figure here is per-session
    and owned by the page (the queue, the flag queue and the per-item clock all
    live there). */
export interface LabelStream {
  /** Instances this annotator has answered, ALL TIME. Read from the server's
      persisted effort stats, never from the in-memory tape: the tape resets on
      every reload, so a session counter showed a returning annotator 0 next to
      work they had actually done and could still see in the store. */
  done: number;
  /** done plus what the queue can still serve to this annotator. 0 renders the
      bar empty rather than dividing by zero. */
  target: number;
  /** Per-instance ACTIVE durations in ms, newest first (Label.tsx's
      activeElapsedMs, which banks tab-away time). Only the first
      ROLLING_WINDOW are read; the panel owns the arithmetic so the printed
      window and the computed window are the same number. */
  recentMs: readonly number[];
  /** Items this annotator has flagged, ALL TIME, from the same persisted
      stats as `done` and for the same reason (§3.9). */
  flags: number;
  /** Flagged items still owing their written explanation. The session cannot
      be marked complete while this is non-zero. */
  pendingNotes: number;
  /** M6: cows with body points from this annotator, and cows marked too
      small, from the same persisted stats as `done`. Undefined against a server
      that predates the step — the line is then left out, not printed as 0. */
  bodyPoints?: number;
  tooSmall?: number;
  /** The body-points backfill: cows that have answers and no body points from
      anyone. Undefined once the backlog is empty, and the line goes away. */
  pointsBacklog?: number;
}

export interface LabelProgressProps {
  /** The live taxonomy's groups. Ordering and visibility are resolved here
      through lib/labelKeys, exactly as the tile row does it, so the letters in
      the legend are the letters on the tiles. Empty while the first fetch is in
      flight — the keys block says so rather than rendering nothing. */
  groups: LabelGroup[];
  stream: LabelStream;
  /** Corpus-wide per-class instance counts (LabelStats.classes). Undefined while
      the first stats fetch is in flight, or against a server predating the
      field; the block says so rather than rendering zeros, which would be
      indistinguishable from a corpus nobody has labeled yet. */
  classes?: LabelClassCount[];
  /** Per-question totals over the same population (LabelStats.class_groups). */
  classGroups?: LabelGroupConsensus[];
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

/* ------------------------------------------------- the class-distribution block

   What the corpus is MADE OF, corpus-wide, counted in instances. This is the one
   figure on the page that is not about the annotator: My stream says how the
   shift is going, this says what the shift is producing — that Head probing has
   two examples and nothing will ever learn it from them.

   It survives the §7 objection that killed the old coverage hero bar ("a
   progress bar you cannot move is just a reminder that the work is long")
   because it is not a progress bar. There is no denominator to fill and no
   completion to fall short of; it is a shape. An annotator who sees one class
   starving is looking at something a poweruser can act on — reweight the queue,
   split a class, or rewrite a definition nobody is picking.

   The counting rule lives in the SQL (labels_db.SQL_CLASS_DISTRIBUTION) and is
   restated here only where the DISPLAY has to honour it: `instances` counts an
   instance every answering annotator agreed on, which today is overwhelmingly
   single-opinion instances, so `confirmed` is drawn INSIDE the same bar rather
   than given a row of its own. One bar, two truths, and no way to read the
   weaker number as if it were the stronger one. */

export interface ClassBarRow {
  class_key: string;
  name: string;
  instances: number;
  confirmed: number;
  archived: boolean;
  /** 0..1 against the group's LARGEST class, not against the group total: the
      question this block answers is "which class is starved", and that is a
      comparison between classes. Normalising to the total would hand a
      six-class group a 39% winner and squash every rare class into the same
      invisible sliver. */
  fraction: number;
  /** 0..1 of THIS class's own instances — the bar within the bar. */
  confirmedFraction: number;
}

export interface ClassBlockGroup {
  group_key: string;
  name: string;
  rows: ClassBarRow[];
  /** Instances answered for this question, taken from the server's roll-up
      rather than summed over `rows`: a multi-select instance appears on several
      rows, so a sum would over-count it. */
  instances: number;
  contested: number;
  confirmed: number;
}

/** Join the live taxonomy to the corpus counts.

    Rendered rows are the VISIBLE classes plus any archived class that still
    holds instances. Dropping archived classes outright would silently delete
    real work from the picture the moment a poweruser tidies the taxonomy;
    keeping archived-but-empty ones would pad every group with rows that only
    ever say zero. Ordering is by instances descending — the starved class is
    then always the bottom row, which is the row this block exists for — with
    the taxonomy's own display order as the tiebreak, so equal counts sit in the
    same order as the tiles instead of shuffling between renders. */
export function classBlocks(
  groups: LabelGroup[],
  counts: readonly LabelClassCount[],
  groupTotals: readonly LabelGroupConsensus[],
): ClassBlockGroup[] {
  const byClass = new Map(counts.map((c) => [c.class_key, c]));
  const byGroup = new Map(groupTotals.map((g) => [g.group_key, g]));
  return visibleGroups(groups).map((g) => {
    const live = visibleClasses(g);
    const order = new Map(live.map((c, i) => [c.class_key, i]));
    const merged = [
      ...live.map((c) => ({
        class_key: c.class_key,
        name: c.name,
        archived: false,
        count: byClass.get(c.class_key),
      })),
      ...counts
        .filter(
          (c) =>
            c.group_key === g.group_key &&
            !c.active &&
            c.instances > 0 &&
            !order.has(c.class_key),
        )
        .map((c) => ({
          class_key: c.class_key,
          name: c.class_name,
          archived: true,
          count: c,
        })),
    ].map((r) => ({
      class_key: r.class_key,
      name: r.name,
      archived: r.archived,
      instances: r.count?.instances ?? 0,
      confirmed: r.count?.confirmed ?? 0,
    }));
    // Against the largest CLASS, taken after the merge so an archived class that
    // is still the biggest one scales its group honestly.
    const max = merged.reduce((m, r) => Math.max(m, r.instances), 0);
    const rows: ClassBarRow[] = merged
      .map((r) => ({
        ...r,
        fraction: max > 0 ? r.instances / max : 0,
        confirmedFraction: r.instances > 0 ? r.confirmed / r.instances : 0,
      }))
      .sort((a, b) => {
        if (a.instances !== b.instances) return b.instances - a.instances;
        // Archived rows have no display order of their own; park them after the
        // live ones rather than at whatever index the map happens not to hold.
        const oa = order.get(a.class_key) ?? Number.MAX_SAFE_INTEGER;
        const ob = order.get(b.class_key) ?? Number.MAX_SAFE_INTEGER;
        if (oa !== ob) return oa - ob;
        return a.class_key < b.class_key ? -1 : a.class_key > b.class_key ? 1 : 0;
      });
    const total = byGroup.get(g.group_key);
    return {
      group_key: g.group_key,
      name: g.name,
      rows,
      instances: total?.instances ?? 0,
      contested: total?.contested ?? 0,
      confirmed: total?.confirmed ?? 0,
    };
  });
}

/* The bar within the bar. The outer segment is this class against the group's
   largest; the brighter inner segment is the part of it two or more annotators
   agreed on. With targets_per_instance = 2 still unmet across the corpus that
   inner segment is empty everywhere — which is the message, not a rendering
   bug, and it fills in on its own as coverage arrives. */
function ClassBar({ row }: { row: ClassBarRow }) {
  return (
    <div className="flex items-center gap-2">
      <span
        className="text-[11.5px] leading-snug truncate min-w-0 grow"
        style={{ color: row.archived ? INK_FAINT : INK_DIM }}
        title={row.archived ? `${row.name} — archived class` : row.name}
      >
        {row.name}
        {row.archived ? (
          <span className="text-[10px]" style={{ color: INK_FAINT }}>
            {" · archived"}
          </span>
        ) : null}
      </span>
      <span
        className="h-1.5 w-[64px] shrink-0 rounded-full overflow-hidden flex"
        style={{ background: TILE }}
        aria-hidden="true"
      >
        <span
          className="h-full rounded-full flex"
          style={{ width: `${row.fraction * 100}%`, background: INK_BAR }}
        >
          <span
            className="h-full rounded-full"
            style={{ width: `${row.confirmedFraction * 100}%`, background: INK }}
          />
        </span>
      </span>
      <span
        className="font-mono text-[11px] tabular-nums text-right w-[30px] shrink-0"
        style={{ color: row.instances === 0 ? INK_FAINT : INK }}
      >
        {row.instances.toLocaleString()}
      </span>
    </div>
  );
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
  classes,
  classGroups,
  onOpenPendingNotes,
  className,
}: LabelProgressProps) {
  const shown = visibleGroups(groups);
  // Undefined (not yet fetched, or an older server) is NOT the same as empty,
  // and the block below says so instead of drawing a row of honest-looking
  // zeros. Only once both arrays have arrived is a zero a real zero.
  const blocks =
    classes === undefined || classGroups === undefined
      ? null
      : classBlocks(groups, classes, classGroups);
  const rate = itemsPerMinute(stream.recentMs);
  const fraction = stream.target > 0 ? stream.done / stream.target : 0;
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
            {stream.done.toLocaleString()}
          </span>
          <span className="text-[12px]" style={{ color: INK_DIM }}>
            / {stream.target.toLocaleString()} labeled
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
        {stream.bodyPoints !== undefined ? (
          <p className="text-[11.5px] leading-snug mt-0.5" style={{ color: INK_DIM }}>
            {stream.bodyPoints.toLocaleString()} {stream.bodyPoints === 1 ? "cow" : "cows"} with
            body points
            {stream.tooSmall ? ` · ${stream.tooSmall.toLocaleString()} too small` : ""}
          </p>
        ) : null}
        {stream.pointsBacklog !== undefined ? (
          <p className="text-[11.5px] leading-snug mt-0.5" style={{ color: INK }}>
            {stream.pointsBacklog.toLocaleString()}{" "}
            {stream.pointsBacklog === 1 ? "cow still needs" : "cows still need"} body points
          </p>
        ) : null}
      </Block>

      <Block title="Classes labeled">
        {blocks === null ? (
          <p className="text-[11.5px]" style={{ color: INK_DIM }}>
            counting the corpus…
          </p>
        ) : blocks.length === 0 ? (
          <p className="text-[11.5px]" style={{ color: INK_DIM }}>
            waiting for the taxonomy…
          </p>
        ) : (
          <div className="flex flex-col gap-3">
            {blocks.map((b) => (
              <div key={b.group_key} className="flex flex-col gap-1">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="text-[11.5px] font-medium" style={{ color: INK }}>
                    {b.name}
                  </span>
                  <span
                    className="font-mono text-[11px] tabular-nums"
                    style={{ color: INK_DIM }}
                  >
                    {b.instances.toLocaleString()}
                  </span>
                </div>
                {b.rows.map((r) => (
                  <ClassBar key={r.class_key} row={r} />
                ))}
                {/* Contested instances belong to no class row, so without this
                    line the bars would simply come up short of the header and
                    look like a bug. It is absent at zero rather than printing
                    "0 contested", which reads as a warning that isn't one. */}
                {b.contested > 0 ? (
                  <p className="text-[10.5px] leading-snug" style={{ color: INK_FAINT }}>
                    {b.contested.toLocaleString()} contested — annotators disagree, counted
                    under no class
                  </p>
                ) : null}
              </div>
            ))}
            {/* What the bars mean, once, at the bottom — and specifically what
                the INNER segment means, since today it is empty everywhere and
                an unexplained empty segment reads as a broken bar rather than
                as "nobody has second-labeled anything yet". */}
            <p className="text-[10.5px] leading-snug" style={{ color: INK_FAINT }}>
              {blocks.some((b) => b.confirmed > 0)
                ? "Instances everyone who answered agreed on; the brighter part had two or more annotators."
                : "Instances everyone who answered agreed on — none has a second annotator yet."}
            </p>
          </div>
        )}
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
