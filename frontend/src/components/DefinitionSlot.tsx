import { forwardRef } from "react";
import type { CSSProperties } from "react";
import { ClassIcon } from "./ClassIcon";

/* The class-definition popover's CARD (M3 labeling UX §3.6, round 2).
 *
 * This file used to be the side panel's DEFINITION SLOT: a fixed 240px box,
 * always mounted, that reserved its own layout space so a definition could
 * open without moving the tile row underneath the annotator's hand. The side
 * panel is being removed from the label screen in this pass, and the slot's
 * reservation strategy goes with it — a definition now opens as a POPOVER
 * anchored to the tile whose info dot was clicked (OptionTile.tsx owns the
 * anchoring). This file is repurposed to be that popover's CARD: the visual
 * shell and content, sized to what it actually holds instead of a fixed
 * height, and rendered by whatever positions it.
 *
 * WHY THIS COMPONENT DOES NOT KNOW ITS OWN POSITION. Anchoring math needs the
 * card's rendered height — a long definition wraps to more lines than a short
 * one — which is only knowable after a first paint, and it needs the tile's
 * bounding rect, which this component never sees. OptionTile does that
 * measuring (it has the tile ref and the portal), and hands the result in
 * through `style`. That split means a definition growing a line never has to
 * teach this file about the viewport math OptionTile already owns to avoid
 * clipping.
 *
 * There is no more "nothing selected" empty state: the old slot was always
 * mounted and showed a placeholder prompt when idle (§2.5.3's `cls: null`
 * branch). A popover only exists while something is open, so that branch is
 * gone with it — OptionTile simply does not render this card at all when its
 * tile's definition isn't the open one.
 */

// The route's dark surface tokens (§2.2), with literal fallbacks so the card
// is still legible if it is ever mounted outside the `data-surface="label"`
// wrapper (a test, a story).
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const TILE = "var(--lbl-tile, #262A2E)";
const HAIRLINE = "var(--lbl-line, rgba(255, 255, 255, 0.09))";

// A card wider than this reads as a line-length problem before it reads as a
// popover; narrower and a poweruser's two-sentence definition wraps so hard
// it looks broken. OptionTile clamps this against the viewport edge rather
// than this file doing it, because only the caller knows how close the
// anchor tile sits to that edge.
export const DEFINITION_CARD_W = 260;
// Long definitions scroll INSIDE the card rather than growing it without
// bound — a poweruser can write an essay at runtime, exactly as the old slot
// guarded against. OptionTile's above/below flip is computed off this same
// number (it is the worst-case height before the card has painted once), so
// an unbounded card would make that math a lie.
export const DEFINITION_CARD_MAX_H = 240;

export interface DefinitionCardProps {
  /** LabelClass.name, for the header. */
  name: string;
  /** LabelClass.icon, for the header. */
  icon: string;
  /** LabelClass.description. Empty renders the same "not written yet" copy
      the old side-panel slot showed, so a poweruser who has not filled in a
      definition yet sees an actionable message rather than a blank card. */
  definition: string;
  /** The card's own ✕. Escape and click-outside are the anchoring caller's
      job (it owns the portal and the document listener); this is only the
      pointer affordance printed inside the card itself. */
  onClose: () => void;
  /** DOM id this card mounts under, so the info dot's `aria-controls` keeps
      pointing at a real element while the card is open. */
  id?: string;
  className?: string;
  /** Positioning, sizing and visibility — entirely the caller's to set. This
      component never reads window/viewport state itself (see header). */
  style?: CSSProperties;
}

export const DefinitionCard = forwardRef<HTMLDivElement, DefinitionCardProps>(
  function DefinitionCard({ name, icon, definition, onClose, id, className, style }, ref) {
    return (
      <div
        ref={ref}
        id={id}
        role="dialog"
        aria-label={`definition of “${name}”`}
        className={
          "rounded-lg border box-border overflow-hidden flex flex-col shadow-lg" +
          (className ? " " + className : "")
        }
        style={{
          width: DEFINITION_CARD_W,
          maxHeight: DEFINITION_CARD_MAX_H,
          background: TILE,
          borderColor: HAIRLINE,
          ...style,
        }}
      >
        <div
          className="flex items-center gap-2 px-3 py-2 border-b shrink-0"
          style={{ borderColor: HAIRLINE }}
        >
          <ClassIcon name={icon} className="w-[18px] h-[18px]" />
          <span
            className="text-[13px] font-semibold leading-tight truncate"
            style={{ color: INK }}
          >
            {name}
          </span>
          <button
            type="button"
            onClick={onClose}
            aria-label="close the definition"
            className="ml-auto shrink-0 w-6 h-6 grid place-items-center rounded cursor-pointer
                       text-[13px] leading-none transition-opacity duration-150 hover:opacity-70
                       focus-visible:outline focus-visible:outline-2"
            style={{ color: INK_DIM, outlineColor: INK }}
          >
            ✕
          </button>
        </div>
        {/* The definition itself. Scrolls; never grows the card past
            DEFINITION_CARD_MAX_H, which is the number the anchoring math
            outside this file relies on. */}
        <div className="px-3 py-2.5 overflow-y-auto grow">
          <p className="text-[12px] leading-relaxed" style={{ color: INK }}>
            {definition === "" ? "This class has no written definition yet." : definition}
          </p>
        </div>
      </div>
    );
  },
);
