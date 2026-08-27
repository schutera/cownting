import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ClassIcon } from "./ClassIcon";
import { InfoIcon } from "./ui";
import { DefinitionCard, DEFINITION_CARD_MAX_H, DEFINITION_CARD_W } from "./DefinitionSlot";

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
 * answer the question. It opens the class definition in a POPOVER anchored to
 * this tile (round 2 — the side panel and its reserved slot are gone); the dot
 * itself never answers, never reflows the tile, and never grows the tile's box.
 *
 * THE POPOVER IS PORTALED, NOT AN IN-FLOW ABSOLUTE CHILD (§3.6 round 2). The
 * label route's root (`data-surface="label"` in Label.tsx) pins itself to the
 * viewport with `overflow: hidden` on purpose — that is what keeps the per-item
 * loop from ever growing a page scrollbar. Anything positioned relative to a
 * normal ancestor inside that subtree is at the mercy of that clip the moment
 * it needs to extend past the root's box (a short viewport, a tile near an
 * edge). Portaling straight to `document.body` and positioning with `fixed`
 * coordinates taken from the tile's own `getBoundingClientRect()` sidesteps
 * every ancestor's overflow and every ancestor's stacking context in one move,
 * which a merely-absolute element inside this tree cannot promise.
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

// Clear space kept between the tile and the popover, and the minimum margin
// kept from the viewport's own edge so the card never sits flush against the
// browser chrome. Not part of DefinitionSlot's card sizing (see that file's
// header) — this is purely the anchoring geometry, which only this component
// computes.
const POPOVER_GAP = 8;
const VIEWPORT_MARGIN = 8;
// A stacking value comfortably above anything else this route paints (the
// inspect-zoom crop only reaches z-index 5), so the popover is never buried by
// a later sibling's own stacking context even though it is portaled to the
// end of <body> and would normally win on paint order alone.
const POPOVER_Z = 1000;

export interface OptionTileProps {
  /** The immutable class key, echoed back on activation so the caller never has
      to match on the display name (which a poweruser can rename). */
  classKey: string;
  /** LabelClass.name — the word under the glyph. */
  name: string;
  /** LabelClass.icon, a ClassIcon vocabulary name; unknown renders the dot. */
  icon: string;
  /** The display key from optionKeysFor() — a short string (a letter, or a
      letter once the taxonomy rebinds), or "" for an option past the ninth,
      which is mouse-only. Rendered verbatim: this component makes no
      assumption about its charset or length, only that it fits the 20x20
      badge. The badge box is reserved either way so a keyless tile does not
      shorten the row's top line. */
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
  /** This class's definition is the one currently open. At most one tile across
      the whole page may be true at a time — the page owns that as a single
      `class_key | null`, never per-tile local state, so opening a second
      definition closes the first by construction. */
  definitionOpen: boolean;
  /** LabelClass.description for this tile's class, shown in the popover body
      when `definitionOpen` is true. Rendered even when `definitionOpen` is
      false is harmless (nothing reads it), but the caller only has to pass it
      once it is known — an empty/missing value renders the same "not written
      yet" copy DefinitionSlot's card always has. */
  definition?: string;
  /** Activation. Only pointer/focus activation reaches this component — the
      digit keys are resolved by the page's own capture-phase handler — so the
      caller can safely tag these as `input_mode: "mouse"` telemetry. */
  onSelect: (classKey: string) => void;
  onOpenDefinition: (classKey: string) => void;
  /** Dismiss an OPEN definition without opening a different one — click
      outside the popover, and choosing this tile's own answer, both funnel
      through here. Deliberately a separate callback from `onOpenDefinition`:
      routing a close through that one would re-fire the `info_opened`
      telemetry event (SQL_INFO_ICON_PRESSURE) on every dismissal, which is a
      single-open signal, not a some-open signal. Optional so a caller that has
      not wired it yet still renders; without it the popover can still be
      dismissed by Escape (the page already clears `openDefinitionKey` on it)
      but not by clicking away. The page must ALSO clear the open definition
      when the active item changes (ArrowRight, auto-advance, jumping to a
      recent item) — this component has no way to observe that on its own,
      since advancing does not necessarily unmount it (the next item's first
      question is frequently the same group, so the same class_key can still
      be "open" for a tile that now belongs to a different animal). */
  onCloseDefinition?: () => void;
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
  definition,
  onSelect,
  onOpenDefinition,
  onCloseDefinition,
}: OptionTileProps) {
  // The latch is same-frame by construction: there is deliberately NO transition
  // on the tile's fill or text colour. A keypress whose visible state change is
  // animated in is the CVAT #8400 failure — the annotator cannot tell a slow UI
  // from a dead key, presses again, and the second press lands on the next cow.
  const face = selected
    ? {
        background: "var(--lbl-accent)",
        color: "var(--lbl-on-accent)",
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

  // id the popover mounts under, so aria-controls keeps pointing at a real
  // element whenever the popover is actually open; dots are legal in an HTML
  // id, and class_key is globally unique, so this cannot collide across tiles.
  const popoverId = `lbl-definition-${classKey}`;

  const tileRef = useRef<HTMLDivElement>(null);
  const cardRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  // Anchoring math (§3.6 round 2). Runs before paint so the two-pass
  // measure-then-place below never flashes at the wrong spot: the first pass
  // renders the card off-screen and `visibility: hidden` (still fully laid
  // out and measurable, unlike `display: none`), this effect reads its REAL
  // size, and the resulting `setPos` is flushed by React before the browser
  // paints — `useLayoutEffect`'s whole contract.
  useLayoutEffect(() => {
    if (!definitionOpen) {
      setPos(null);
      return;
    }
    const reposition = () => {
      const tile = tileRef.current;
      if (tile === null) return;
      const tileRect = tile.getBoundingClientRect();
      // Falls back to the card's own reserved maximum only if the portal has
      // not mounted yet, which should not happen by the time this runs — kept
      // as a defensive floor rather than a real code path.
      const cardH = cardRef.current?.offsetHeight || DEFINITION_CARD_MAX_H;
      const cardW = cardRef.current?.offsetWidth || DEFINITION_CARD_W;
      // Prefer ABOVE the tile row (§3.6 round 2: cover as little of the crop
      // above it as the content needs, never the footer legend below), and
      // fall back to below only when there truly is not room above.
      const fitsAbove = tileRect.top - POPOVER_GAP - cardH >= VIEWPORT_MARGIN;
      const top = fitsAbove
        ? tileRect.top - POPOVER_GAP - cardH
        : Math.min(
            tileRect.bottom + POPOVER_GAP,
            window.innerHeight - cardH - VIEWPORT_MARGIN,
          );
      // Left-align with the tile, like the tile row itself (§2.7's left
      // alignment), clamped so a tile near the right edge does not push the
      // card off-screen.
      const left = Math.min(
        Math.max(tileRect.left, VIEWPORT_MARGIN),
        window.innerWidth - cardW - VIEWPORT_MARGIN,
      );
      setPos({ top, left });
    };
    reposition();
    // The route's own root is pinned with `overflow: hidden` specifically so
    // the page never scrolls (see Label.tsx), so a `scroll` here almost never
    // fires from within it — this only guards a future ancestor that does
    // scroll (a test harness, a story). Resize is the real, common case: the
    // window changing size while a definition is open must not leave the card
    // floating over empty space or clipped at the new edge.
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
    };
  }, [definitionOpen, definition]);

  // Click-outside dismissal. A click on THIS tile (either button) is
  // deliberately excluded so re-clicking the info dot is left to
  // `onOpenDefinition`'s own existing semantics rather than being pre-empted
  // here, and so clicking this tile's own answer button is handled once, by
  // that button's own onClick, rather than twice.
  useEffect(() => {
    if (!definitionOpen) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target;
      if (!(target instanceof Node)) return;
      if (tileRef.current?.contains(target)) return;
      if (cardRef.current?.contains(target)) return;
      onCloseDefinition?.();
    };
    document.addEventListener("mousedown", onPointerDown, true);
    return () => document.removeEventListener("mousedown", onPointerDown, true);
  }, [definitionOpen, onCloseDefinition]);

  return (
    <div ref={tileRef} className="relative" style={{ width: TILE_W, height: TILE_H }}>
      <button
        type="button"
        aria-pressed={selected}
        tabIndex={focusable ? 0 : -1}
        onClick={() => {
          onSelect(classKey);
          // Choosing an answer dismisses any open definition outright (§3.6
          // round 2's dismissal list) — called unconditionally rather than
          // only when THIS tile's own definition is open, which costs nothing
          // when nothing is open (the page's `openDefinitionKey` is already
          // null) and needs no per-tile bookkeeping to get right.
          onCloseDefinition?.();
        }}
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
              color: selected ? "var(--lbl-on-accent)" : "var(--lbl-accent)",
              background: selected
                ? "color-mix(in srgb, var(--lbl-on-accent) 22%, transparent)"
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
        aria-haspopup="dialog"
        aria-controls={popoverId}
        aria-label={`definition of “${name}”`}
        onClick={() => onOpenDefinition(classKey)}
        style={{
          width: 24,
          height: 24,
          top: INFO_INSET,
          right: INFO_INSET,
          // Selection wins over the open state: on a filled tile the accent is
          // the background, so an accent info dot would vanish into it.
          color: selected ? "var(--lbl-on-accent)" : definitionOpen ? "var(--lbl-accent)" : "var(--lbl-ink-dim)",
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

      {definitionOpen
        ? createPortal(
            <DefinitionCard
              ref={cardRef}
              id={popoverId}
              name={name}
              icon={icon}
              definition={definition ?? ""}
              onClose={() => onCloseDefinition?.()}
              style={{
                position: "fixed",
                top: pos?.top ?? -9999,
                left: pos?.left ?? -9999,
                // Hidden rather than unmounted for the first, pre-measurement
                // frame: unmounted would mean no `cardRef` to measure and no
                // way out of the chicken-and-egg (see the layout effect
                // above).
                visibility: pos === null ? "hidden" : "visible",
                zIndex: POPOVER_Z,
              }}
            />,
            document.body,
          )
        : null}
    </div>
  );
}
