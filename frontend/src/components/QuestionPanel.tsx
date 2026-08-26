import { useEffect, useRef } from "react";
import type { CSSProperties } from "react";
import type { LabelGroup } from "../lib/types";
import { numberKeysFor, visibleClasses } from "../lib/labelKeys";
import { ClassIcon } from "./ClassIcon";
import { OptionTile, TILE_GAP, TILE_H, TILE_W } from "./OptionTile";

/* The one active question (M3 labeling UX §2.4, §2.6-2.8).
 *
 * EXACTLY ONE QUESTION IS ON SCREEN. The old `LabelGroup.tsx` stacked every
 * group as a full-width list, which put the furthest answer 737px below the
 * animal being judged and pushed the second question and the Save button off a
 * 1366x768 viewport entirely. This renders one framed field with one row of
 * tiles; Q2 replaces Q1 in the same rectangle rather than sitting greyed-out
 * beneath it, because a greyed-out second block is still a form (§2.7).
 *
 * THE COLOUR FLIP IS INFORMATION, NOT DECORATION (§1.2, §5.1). Key `2` means
 * *Direct sun* in one state and *Lying* in the next — a varied mapping across
 * contexts, and Schneider & Shiffrin (1977) show varied mapping never becomes
 * automatic. The annotator's memory cannot carry the rebind, so the display
 * has to: warm field + amber accent + `shade` glyph + `SUN EXPOSURE` + `[1/2]`
 * + `●○` + four tiles, against cool field + teal accent + a different glyph +
 * `BEHAVIOUR` + `[2/2]` + `●●` + five tiles. Seven channels, of which only two
 * are colour, so the distinction survives greyscale and both common dichromacies
 * (WCAG 2.2 SC 1.4.1). A WEAK difference between the two states would be the
 * worst possible outcome: it invites the overlearned response and produces a
 * valid class, no error, and an auto-advance.
 *
 * THE GEOMETRY IS FIXED, NOT FITTED. Panel height is a constant (§2.3's budget:
 * 24 prompt + 8 gap + 108 tiles + 2x10 padding) and the tile row reserves the
 * width of the WIDEST question via `reserveOptions`. Sun exposure is 4 tiles =
 * 478px and Behaviour 5 = 600px (6 = 722px once `behaviour.not_visible` lands,
 * §6.3), so a fitted frame would resize at the handoff — and anything that moves
 * between the moment the annotator decides and the moment they press is a
 * mis-click. The row is LEFT-ALIGNED and never centred for the same reason:
 * centring would slide tile 1 horizontally between a 4-up and a 5-up question,
 * and tile 1's position is what muscle memory aims at.
 *
 * ARROWS ARE NOT OURS (§3.8). The tiles are `<button>`s in a plain
 * `role="group"` with a single roving tabindex — never `<input type="radio">` in
 * a `role="radiogroup"`, which moves the checked radio on Left/Right and fires
 * `onChange`. With auto-save always on, that turns one mouse click on an option
 * into an armed ArrowLeft that writes a label instead of moving the tape. This
 * component therefore binds no keys at all: the digits and the arrows are
 * resolved by the page's capture-phase window listener, and everything that
 * arrives here is a pointer or focus activation.
 *
 * NOTHING HERE IS ASYNC. There is no `busy` prop and no disabled state, because
 * the previous screen awaited the POST before accepting the next keypress and
 * dropped presses in total silence — the annotator presses again and it lands on
 * the NEXT cow (§3.5). Selection is a synchronous state change; the network is
 * somewhere else entirely.
 */

// §2.3's arithmetic, as constants rather than utility classes: these are a
// layout contract with the crop's `clamp()` — every pixel added here comes off
// the crop on a 1366x768 machine — not a style preference.
const PROMPT_H = 24;
const PROMPT_GAP = 8;
const PANEL_PAD = 10;

/** The panel's outer height, fixed for every question and every tile count.
    Exported because the crop above it is sized by subtraction (§2.3), so the
    page cannot be allowed to re-measure this and drift from it. */
export const PANEL_H = PROMPT_H + PROMPT_GAP + TILE_H + 2 * PANEL_PAD;

/* Per-question tone (§5.1). Indexed by the question's POSITION, not by
   `group_key`: powerusers add, rename and reorder groups at runtime, so a
   `group_key -> colour` table would be wrong the first time anyone edits the
   taxonomy — and wrong invisibly. Position is also what the annotator actually
   experiences, since it is the alternation that marks the digit rebind.

   Two tones only. §2.2 is explicit that there is no third hue anywhere in the
   answer area, so a third question would alternate back to the warm tone rather
   than introduce one; adjacent questions still differ, which is the property
   that carries the rebind. The literal fallbacks keep the panel readable if it
   is ever mounted outside the `data-surface="label"` wrapper (a test, a story). */
interface QuestionTone {
  accent: string;
  field: string;
}
const QUESTION_TONES: readonly QuestionTone[] = [
  { accent: "var(--lbl-q1, #E0A03C)", field: "var(--lbl-q1-field, #2A2418)" },
  { accent: "var(--lbl-q2, #3FB8AE)", field: "var(--lbl-q2-field, #152526)" },
];

const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";

/* React's CSSProperties has no index signature, so a custom property in a style
   object is a type error. Declaring the one we set is a real type rather than an
   `as` cast past the checker — and `--lbl-accent` has to be set HERE, on the
   panel, because OptionTile reads it for its fill, its badge and its inset ring.
   That is the whole mechanism by which the tiles inherit the active question's
   colour without either component knowing which question is active. */
interface PanelStyle extends CSSProperties {
  "--lbl-accent": string;
}

/* The handoff (§3.4) and the dead-key shake (§3.3), as a stylesheet because
   both are keyframed and Tailwind's animation utilities are configured in
   index.css, which this feature is not allowed to extend.

   Incoming tiles run 50-140ms of the 140ms handoff with `backwards` fill, so
   they are invisible for the first 50ms while the field colour is still
   crossfading — the two halves do not overlap into mush. Under
   `prefers-reduced-motion` the translate is dropped for a plain 80ms fade.

   The shake SURVIVES reduced motion on purpose. It is the only feedback a
   pressed-but-unbound digit produces, and CVAT #8400 (keys 0-8 worked, key 9
   silently no-op'd) is exactly what a silent dead key costs. 4px over 120ms is
   not a vestibular trigger; a keypress with no response at all is a data bug. */
const PANEL_CSS = `
@keyframes lbl-qp-tile-in {
  from { opacity: 0; transform: translateY(6px); }
  to   { opacity: 1; transform: none; }
}
@keyframes lbl-qp-tile-fade {
  from { opacity: 0; }
  to   { opacity: 1; }
}
@keyframes lbl-qp-shake {
  0%, 100% { transform: translateX(0); }
  25%      { transform: translateX(-4px); }
  75%      { transform: translateX(4px); }
}
.lbl-qp-tiles > * { animation: lbl-qp-tile-in 90ms 50ms ease-out both; }
.lbl-qp-tiles[data-shake="on"] { animation: lbl-qp-shake 120ms ease-in-out; }
@media (prefers-reduced-motion: reduce) {
  .lbl-qp-tiles > * {
    animation: lbl-qp-tile-fade 80ms ease-out both;
  }
}
`;

/* Drawn, not typed. "✎", "✽" and "●" render at a different weight, baseline and
   sometimes a different picture on every platform, and two of them are carrying
   state that WCAG 1.4.1 requires to be non-colour. A 12px glyph that is
   sometimes an emoji is not a state channel. */
function PencilMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="12"
      height="12"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M4 20h4L20 8l-4-4L4 16Z" />
    </svg>
  );
}

function AsteriskMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="11"
      height="11"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.4"
      strokeLinecap="round"
      aria-hidden="true"
    >
      <path d="M12 4v16M4.9 7.5l14.2 9M19.1 7.5l-14.2 9" />
    </svg>
  );
}

/* The step pip, `●○` / `●●` (§5.1). Rendered as boxes rather than as characters
   for the reason above, and filled-vs-hollow is a shape difference, so it reads
   in greyscale. With the `[n/2]` counter beside it, this is the pair of cues
   that tells a sequential-flow annotator a second question exists at all. */
function StepPips({ index, count }: { index: number; count: number }) {
  return (
    <span className="flex items-center gap-[3px]" aria-hidden="true">
      {Array.from({ length: count }, (_, i) => (
        <span
          key={i}
          className="rounded-full box-border"
          style={{
            width: 6,
            height: 6,
            background: i <= index ? "var(--lbl-accent)" : "transparent",
            border: i <= index ? "none" : `1px solid ${INK_DIM}`,
          }}
        />
      ))}
    </span>
  );
}

export interface QuestionPanelProps {
  /** The ACTIVE question, and only it. The page owns the sequence; this
      component never renders a second group, greyed-out or otherwise (§2.7). */
  group: LabelGroup;
  /** 0-based position of `group` among visibleGroups(), which decides both the
      `[n/N]` counter and the tone. Position rather than group_key, so a
      poweruser reordering the questions reorders the colours with them. */
  stepIndex: number;
  /** How many questions this item has — visibleGroups().length. Rendered
      literally as `[1/2]`, stolen verbatim from CVAT's attribute-switcher. */
  stepCount: number;
  /** Tile count of the WIDEST question on this item, i.e.
      max(visibleClasses(g).length). The tile row reserves that width so the
      frame does not resize at the handoff (§2.7). Pass the per-item maximum,
      not this group's own count. */
  reserveOptions: number;
  /** This group's current answer, or null. Single-select only: `multi_select`
      is a taxonomy flag this screen does not implement, because one active
      question binding 1..9 with press-to-advance has no multi-select reading. */
  selectedClassKey: string | null;
  /** The item was reached with ArrowLeft (§2.8). Adds the dashed outline and the
      REVIEWING marker; going back must never look like a fresh item. This
      component only DISPLAYS the phase — suppressing auto-advance on a revisit
      is the page's job, and is the §1.6 trap. */
  reviewing: boolean;
  /** class_key whose definition is currently in the side panel's slot, or null.
      Not local state: Escape clears the slot from the page, and the slot
      outlives this panel across the handoff. */
  openDefinitionKey: string | null;
  /** id of the side panel's definition slot, for the info dots' `aria-controls`.
      Omitted when the panel is not mounted (below 1100px it drops, §2.4). */
  definitionSlotId?: string;
  /** A tile was activated by pointer or keyboard focus. Digit presses do NOT
      come through here — the page resolves those itself — so the page may tag
      these as `input_mode: "mouse"` telemetry. Both keys are passed because the
      `answered` event carries `{group_key, class_key}` and splitting a class_key
      on its dot to recover the group is exactly the key-parsing that
      labelKeys.ts exists to prevent. */
  onSelect: (groupKey: string, classKey: string) => void;
  /** Info dot. Never answers the question (§3.6) — the dot is a sibling of the
      tile button, not a child. */
  onOpenDefinition: (classKey: string) => void;
  /** Bump this number to shake the tile row for 120ms (§3.3's unbound digit,
      §3.7's ArrowRight on an incomplete item). A counter rather than a boolean
      because two dead keys in a row must produce two shakes, and a boolean that
      is already true produces none. Left undefined, nothing ever shakes. */
  shakeNonce?: number;
  /** Prompt-row glyph (§5.1's third channel). Defaults to the first visible
      class's icon, which yields `shade` for Sun exposure as specified; groups
      carry no icon of their own in the taxonomy, so a page that wants §5.1's
      `probe` for Behaviour passes it explicitly. Never hardcode by group_key. */
  glyph?: string;
  className?: string;
}

export function QuestionPanel({
  group,
  stepIndex,
  stepCount,
  reserveOptions,
  selectedClassKey,
  reviewing,
  openDefinitionKey,
  definitionSlotId,
  onSelect,
  onOpenDefinition,
  shakeNonce,
  glyph,
  className,
}: QuestionPanelProps) {
  // Both derive from visibleClasses(), so index i is the same option in both and
  // the badge can never show a digit the page's handler resolves to a different
  // class. This is the single-source rule labelKeys.ts states: render from
  // numberKeysFor(), never from `group.classes`.
  const classes = visibleClasses(group);
  const keys = numberKeysFor(group);

  const tone = QUESTION_TONES[stepIndex % QUESTION_TONES.length];
  const titleId = `lbl-question-${group.group_key}`;

  // Reserve the widest row, never this row: `reserveOptions` may legitimately be
  // smaller than this group's count if a caller passes the wrong thing, and a
  // clipped tile is worse than a wide frame.
  const columns = Math.max(reserveOptions, classes.length, 1);
  const reservedW = columns * TILE_W + (columns - 1) * TILE_GAP;

  const rowRef = useRef<HTMLDivElement>(null);
  const lastShake = useRef(shakeNonce);
  useEffect(() => {
    if (shakeNonce === lastShake.current) return;
    lastShake.current = shakeNonce;
    const el = rowRef.current;
    if (el === null) return;
    // Restart rather than re-trigger: an animation already running does not
    // replay on a class change, and the second dead key in a row is precisely
    // the one the annotator needs to see.
    el.removeAttribute("data-shake");
    void el.offsetWidth; // forced reflow, the only way to rewind a CSS animation
    el.setAttribute("data-shake", "on");
    const timer = window.setTimeout(() => el.removeAttribute("data-shake"), 160);
    return () => window.clearTimeout(timer);
  }, [shakeNonce]);

  const unanswered = reviewing && selectedClassKey === null;

  const panelStyle: PanelStyle = {
    "--lbl-accent": tone.accent,
    background: tone.field,
    borderLeftStyle: "solid",
    borderLeftWidth: 3,
    borderLeftColor: tone.accent,
    borderRadius: 10,
    padding: PANEL_PAD,
    height: PANEL_H,
    color: INK,
    // The 140ms crossfade of §3.4's handoff. Colour only: the panel itself never
    // moves, which is what lets the eye stay where it already is.
    transition: "background-color 140ms ease, border-left-color 140ms ease",
    // `outline` rather than a border, so the review state cannot change the
    // panel's box and shift the tile row by 2px on the way in (§2.8).
    outline: reviewing ? `2px dashed ${INK_DIM}` : undefined,
    outlineOffset: reviewing ? 2 : undefined,
  };

  return (
    <div className={"box-border w-full" + (className ? " " + className : "")} style={panelStyle}>
      <style>{PANEL_CSS}</style>

      {/* Prompt row, 24px (§2.4). */}
      <div className="flex items-center gap-2" style={{ height: PROMPT_H }}>
        {/* §5.3's `✽`: on a revisited item an unanswered question is marked by
            shape AND word, after V7's required-property star — never by colour
            alone, and never by the absence of something. */}
        {unanswered ? (
          <span className="shrink-0" style={{ color: INK }}>
            <AsteriskMark />
          </span>
        ) : null}

        <ClassIcon
          name={glyph ?? classes[0]?.icon ?? "dot"}
          className="w-4 h-4 shrink-0"
        />

        <h2
          id={titleId}
          className="min-w-0 truncate text-[13px] font-semibold uppercase tracking-[0.08em] leading-none"
          style={{ color: INK }}
        >
          {group.name}
        </h2>

        {reviewing ? (
          <span
            className="flex items-center gap-1 shrink-0 text-[11px] font-semibold uppercase tracking-[0.08em] leading-none"
            style={{ color: INK_DIM }}
          >
            <PencilMark />
            reviewing
          </span>
        ) : null}

        {unanswered ? (
          <span
            className="shrink-0 text-[11px] uppercase tracking-[0.08em] leading-none"
            style={{ color: INK }}
          >
            unanswered
          </span>
        ) : null}

        {/* The counter is the load-bearing cue of §5.1: on a sequential flow it
            is the only thing that says a second question exists. Literal
            brackets, monospaced so `[1/2]` and `[2/2]` occupy identical width
            and the flip is a change of digit rather than a change of layout. */}
        <span
          className="ml-auto flex items-center gap-2 shrink-0 font-mono text-[12px] leading-none tabular-nums"
          style={{ color: INK_DIM }}
        >
          <span>{`[${stepIndex + 1}/${stepCount}]`}</span>
          <StepPips index={stepIndex} count={stepCount} />
        </span>
      </div>

      {/* Tile row, 108px (§2.4). `key` on the group so the handoff animation
          replays when the question changes and NOT when the item advances while
          the question stays Q1 — §3.4 requires that reset to be instant, under
          the crop's crossfade, so the annotator never sees a half-swapped panel. */}
      <div
        key={group.group_key}
        ref={rowRef}
        role="group"
        aria-labelledby={titleId}
        className="lbl-qp-tiles"
        style={{
          marginTop: PROMPT_GAP,
          display: "grid",
          gridAutoFlow: "column",
          gridAutoColumns: `${TILE_W}px`,
          gap: TILE_GAP,
          // Left-aligned, never centred: tile 1 must not move between a 4-up and
          // a 5-up question. `minWidth` reserves the widest row so the frame
          // holds its size across the handoff.
          justifyContent: "start",
          height: TILE_H,
          minWidth: reservedW,
        }}
      >
        {classes.map((cls, i) => (
          <OptionTile
            key={cls.class_key}
            classKey={cls.class_key}
            name={cls.name}
            icon={cls.icon}
            keyLabel={keys[i].label}
            selected={cls.class_key === selectedClassKey}
            reviewing={reviewing}
            // Roving tabindex (§3.8): the answered tile if there is one, else the
            // first, so a Tab into the panel lands where the eye already is and
            // Shift+Tab out of it is one keystroke.
            focusable={selectedClassKey === null ? i === 0 : cls.class_key === selectedClassKey}
            definitionOpen={cls.class_key === openDefinitionKey}
            definitionSlotId={definitionSlotId}
            onSelect={(classKey) => onSelect(group.group_key, classKey)}
            onOpenDefinition={onOpenDefinition}
          />
        ))}
      </div>
    </div>
  );
}
