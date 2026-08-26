import { ClassIcon } from "./ClassIcon";
import { InfoIcon } from "./ui";

/* One answer tile: [digit badge][glyph][word], with an info dot in the corner.
 *
 * WHY A BUTTON AND NOT A RADIO (M3 labeling UX §3.8). The old option rows were
 * `sr-only` `<input type="radio">` inside a `role="radiogroup"`. A native
 * radiogroup moves the checked radio on Left/Right/Up/Down and fires `onChange`,
 * so with auto-save always on, ONE mouse click on an option armed ArrowLeft to
 * write a label instead of navigating the tape — a silent wrong answer, which is
 * the worst failure this screen can have. Exempting `radio` from the typing
 * guard does not help: only `preventDefault()` stops the native behaviour, and
 * the page's window listener runs after the input has already acted. Buttons
 * have no native arrow behaviour at all, so the arrows belong unambiguously to
 * the page's capture-phase handler. The price is a roving tabindex, which the
 * panel supplies through `focusable`.
 *
 * WHY 112 x 108. That is 2.5x WCAG 2.2 SC 2.5.5's 44x44 AAA target, and the
 * 10px gutter between tiles satisfies SC 2.5.8's spacing clause. The previous
 * rows were 42px tall and full-width — already at the AAA scale in the useless
 * dimension while wasting all the width, which is what put the furthest answer
 * 737px below the animal being judged. Badge, glyph and word are one visual
 * token with no interior whitespace, after CVAT's `1: Shaded` adjacency.
 *
 * THE WORD STAYS. NN/g is explicit that icon labels must be visible at all times
 * without interaction: the icon is the ACQUISITION aid and the label is the
 * DISAMBIGUATION. "Head probing" is a coined domain term no glyph will ever
 * carry alone. The compression comes from the tile layout, not from deleting
 * text — so the label wraps to two lines and is never truncated.
 *
 * THE INFO DOT IS A SIBLING, NOT A CHILD (§3.6). A click inside the tile button
 * would also activate it, so nesting would make "read the definition" silently
 * answer the question. It opens the class definition in the side panel's
 * reserved slot; it never answers, never reflows, and never covers the crop.
 */

// The tile box, exported because the panel's grid has to lay out exactly these
// numbers: a grid column narrower than the tile would clip the word, and one
// wider would break the left-aligned start position tile 1's muscle memory aims
// at. One source, so the two cannot drift.
export const TILE_W = 112;
export const TILE_H = 108;
export const TILE_GAP = 10;

// Inset of the badge and of the info dot's 24x24 hit box from the tile edge,
// chosen so both sit centred on the same y (7 + 20/2 === 5 + 24/2) and so the
// info glyph itself keeps ~10px of clear space from the tile's own edge (§3.6).
const BADGE_INSET = 7;
const INFO_INSET = 5;

export interface OptionTileProps {
  /** The immutable class key, echoed back on activation so the caller never has
      to match on the display name (which a poweruser can rename). */
  classKey: string;
  /** LabelClass.name — the word under the glyph. */
  name: string;
  /** LabelClass.icon, a ClassIcon vocabulary name; unknown renders the dot. */
  icon: string;
  /** The digit from numberKeysFor(), or "" for an option past the ninth, which
      is mouse-only. The badge box is reserved either way so a keyless tile does
      not shorten the row's top line. */
  keyLabel: string;
  /** This class is the group's current answer: filled, inverted, ringed, ticked
      (§5.2) — four channels, so it survives the greyscale check. */
  selected: boolean;
  /** The item was reached with ArrowLeft. Adds the second, detached rule around
      a selected tile (§2.8's `═`), so the answer stays unambiguous inside a
      panel that is itself dashed and marked REVIEWING. */
  reviewing: boolean;
  /** The single tab stop of the group (§3.8's roving tabindex). Exactly one tile
      in a panel may be true. */
  focusable: boolean;
  /** This class's definition is the one currently in the side panel slot. */
  definitionOpen: boolean;
  /** id of the side panel's definition slot, for `aria-controls`. Omitted when
      the slot is not mounted (narrow viewports drop the side panel). */
  definitionSlotId?: string;
  /** Activation. Only pointer/focus activation reaches this component — the
      digit keys are resolved by the page's own capture-phase handler — so the
      caller can safely tag these as `input_mode: "mouse"` telemetry. */
  onSelect: (classKey: string) => void;
  onOpenDefinition: (classKey: string) => void;
}

/* The tick that appears in the badge row on the chosen tile. Drawn rather than
   typed: a "✓" character renders at a different weight and baseline on every
   platform, and this one has to read at 12px on an accent fill. */
function CheckMark() {
  return (
    <svg
      viewBox="0 0 24 24"
      width="13"
      height="13"
      fill="none"
      stroke="currentColor"
      strokeWidth="3"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M5 12.5 9.5 17 19 7" />
    </svg>
  );
}

export function OptionTile({
  classKey,
  name,
  icon,
  keyLabel,
  selected,
  reviewing,
  focusable,
  definitionOpen,
  definitionSlotId,
  onSelect,
  onOpenDefinition,
}: OptionTileProps) {
  // The latch is same-frame by construction: there is deliberately NO transition
  // on the tile's fill or text colour. A keypress whose visible state change is
  // animated in is the CVAT #8400 failure — the annotator cannot tell a slow UI
  // from a dead key, presses again, and the second press lands on the next cow.
  const face = selected
    ? {
        background: "var(--lbl-accent)",
        color: "var(--lbl-bg)",
        borderColor: "transparent",
        // inset ring per §5.2; the outer pair is the review double-rule (§2.8),
        // both drawn as shadows so neither state changes the tile's box.
        boxShadow: reviewing
          ? "inset 0 0 0 2px var(--lbl-accent), 0 0 0 1px var(--lbl-bg), 0 0 0 3px var(--lbl-accent)"
          : "inset 0 0 0 2px var(--lbl-accent)",
      }
    : {
        background: "var(--lbl-tile)",
        color: "var(--lbl-ink)",
        borderColor: "color-mix(in srgb, var(--lbl-ink) 14%, transparent)",
      };

  return (
    <div className="relative" style={{ width: TILE_W, height: TILE_H }}>
      <button
        type="button"
        aria-pressed={selected}
        tabIndex={focusable ? 0 : -1}
        onClick={() => onSelect(classKey)}
        style={{ ...face, padding: BADGE_INSET }}
        className={
          "absolute inset-0 flex flex-col items-center rounded-lg border box-border cursor-pointer " +
          "focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--lbl-ink)] " +
          (selected ? "" : "hover:border-[var(--lbl-ink-dim)]")
        }
      >
        {/* Badge row. The tick sits left of the info dot, which is a sibling of
            this button and therefore painted over the row's right end. */}
        <span className="flex w-full items-center justify-between" style={{ height: 20, paddingRight: 26 }}>
          <span
            className="grid place-items-center rounded-md font-mono text-[12px] font-bold leading-none"
            style={{
              width: 20,
              height: 20,
              color: selected ? "var(--lbl-bg)" : "var(--lbl-accent)",
              background: selected
                ? "color-mix(in srgb, var(--lbl-bg) 22%, transparent)"
                : "color-mix(in srgb, var(--lbl-accent) 16%, transparent)",
            }}
          >
            {keyLabel}
          </span>
          {selected ? <CheckMark /> : null}
        </span>

        {/* Glyph band. Fixed height so a one-line and a two-line label put their
            glyphs on the same baseline across the row. */}
        <span className="grid w-full flex-1 place-items-center">
          <ClassIcon name={icon} />
        </span>

        {/* Two lines at 12/14 = 28px, reserved whether or not the word wraps, so
            the row's glyphs never jump. `break-words` guards a long
            poweruser-added class name; nothing is ever ellipsised. */}
        <span
          className="w-full text-center text-[12px] leading-[14px] break-words"
          style={{ height: 28 }}
        >
          {name}
        </span>
      </button>

      <button
        type="button"
        aria-expanded={definitionOpen}
        aria-controls={definitionSlotId}
        aria-label={`definition of “${name}”`}
        onClick={() => onOpenDefinition(classKey)}
        style={{
          width: 24,
          height: 24,
          top: INFO_INSET,
          right: INFO_INSET,
          // Selection wins over the open state: on a filled tile the accent is
          // the background, so an accent info dot would vanish into it.
          color: selected ? "var(--lbl-bg)" : definitionOpen ? "var(--lbl-accent)" : "var(--lbl-ink-dim)",
          opacity: definitionOpen ? 1 : 0.65,
        }}
        className={
          "absolute grid place-items-center rounded-full cursor-pointer transition-opacity duration-150 " +
          "hover:opacity-100 " +
          "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--lbl-ink)]"
        }
      >
        <InfoIcon className="w-[14px] h-[14px]" />
      </button>
    </div>
  );
}
