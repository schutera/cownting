import type { LabelClass } from "../lib/types";
import { ClassIcon } from "./ClassIcon";
import { InfoIcon } from "./ui";

/* The Label side panel's definition slot (UX §2.5.3).
 *
 * ONE definition, the one whose info dot was clicked last — decision 5 removes
 * the global "show definitions" toggle and the `I` binding, because nine
 * definitions expanded inline under nine options is the wall of text this
 * redesign exists to delete.
 *
 * The height is RESERVED, not fitted. This is the entire reason the slot lives
 * in the side panel instead of under the tile that was clicked: a definition
 * that opens where the annotator is looking pushes the tile row down between
 * the moment they decide and the moment they press, so the click lands on the
 * neighbouring answer. A fixed 240 px box that is empty most of the time costs
 * nothing — the panel is never needed to answer an item — and it guarantees
 * that reading a definition cannot move a target.
 *
 * Long definitions scroll INSIDE the box rather than growing it. The seeded
 * definitions are one or two operational sentences and fit, but a poweruser can
 * write an essay at runtime and that must not be able to reflow the panel.
 *
 * §6.9 additionally calls for a canonical positive and a near-miss negative
 * example crop, 96 px each, captioned yes/no — evidenced as the one instruction
 * change that measurably improves annotation quality. That needs two nullable
 * instance_key columns on `label_classes` which do not exist yet, so it is not
 * rendered here; the reserved height already has room for it.
 */

// UX §2.5.3 states the reserved height literally. It is a layout contract, not
// a style preference, so it is a named constant rather than a utility class.
export const DEFINITION_SLOT_H = 240;

// The route's dark surface tokens (§2.2), with literal fallbacks so the slot is
// still legible if it is mounted outside the `data-surface="label"` wrapper.
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const TILE = "var(--lbl-tile, #262A2E)";
const HAIRLINE = "rgba(255, 255, 255, 0.09)";

export interface DefinitionSlotProps {
  /** The class whose info dot was clicked last (§3.6), or null for the empty
      state. The page owns this: Escape clears it, and advancing does not —
      a definition read on the previous cow is still true on this one. */
  cls: LabelClass | null;
  /** Dismiss. Wired to the ✕ here and to Escape by the page (§3.7). */
  onClear: () => void;
  className?: string;
}

export function DefinitionSlot({ cls, onClear, className }: DefinitionSlotProps) {
  return (
    <div
      className={"rounded-lg border box-border overflow-hidden flex flex-col" + (className ? " " + className : "")}
      style={{ height: DEFINITION_SLOT_H, background: TILE, borderColor: HAIRLINE }}
    >
      {cls === null ? (
        <p
          className="p-3 text-[12px] leading-relaxed flex items-start gap-1.5"
          style={{ color: INK_DIM }}
        >
          <InfoIcon className="w-3.5 h-3.5 shrink-0 mt-[3px]" />
          <span>Click the info dot on an option to read its definition here.</span>
        </p>
      ) : (
        <>
          <div
            className="flex items-center gap-2 px-3 py-2 border-b shrink-0"
            style={{ borderColor: HAIRLINE }}
          >
            <ClassIcon name={cls.icon} className="w-[18px] h-[18px]" />
            <span className="text-[13px] font-semibold leading-tight" style={{ color: INK }}>
              {cls.name}
            </span>
            <button
              type="button"
              onClick={onClear}
              aria-label="close the definition"
              className="ml-auto shrink-0 w-6 h-6 grid place-items-center rounded cursor-pointer
                         text-[13px] leading-none transition-opacity duration-150 hover:opacity-70
                         focus-visible:outline focus-visible:outline-2"
              style={{ color: INK_DIM, outlineColor: INK }}
            >
              ✕
            </button>
          </div>
          {/* The definition itself. Scrolls; never grows the slot. */}
          <div className="px-3 py-2.5 overflow-y-auto grow">
            <p className="text-[12px] leading-relaxed" style={{ color: INK }}>
              {cls.description === "" ? "This class has no written definition yet." : cls.description}
            </p>
            {/* An escape class is a confession about the annotator, not a fact
                about the image, and its rate is monitored (§6.4). Saying so
                where the definition is read is cheaper than finding out from
                the escape-rate query three days later. */}
            {cls.is_escape ? (
              <p className="text-[11px] leading-snug mt-2" style={{ color: INK_DIM }}>
                An escape answer — use it when the pixels genuinely do not decide it, not to move on.
              </p>
            ) : null}
          </div>
        </>
      )}
    </div>
  );
}
