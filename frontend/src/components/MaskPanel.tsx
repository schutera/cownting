import { PANEL_H } from "./QuestionPanel";

/* The outline tool's controls (docs/roadmap/M4a_instance_mask_fixup.md §5.3).
 *
 * It occupies QuestionPanel's rectangle at exactly PANEL_H, which is a LAYOUT
 * CONTRACT, not a coincidence: the crop above is sized by subtracting fixed
 * chrome from the viewport, so a panel one pixel taller than the question panel
 * would resize the photograph the moment the toggle flipped — and the annotator
 * would be dragging nodes on an image that just moved under the cursor.
 *
 * The colours are the page's --lbl-* tokens, matching FlagRow rather than the
 * paper surface: this rectangle sits inside the dark card.
 */

const HAIRLINE = "var(--lbl-line, rgba(255, 255, 255, 0.09))";
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const CARD = "var(--lbl-card, #1E2124)";
const TILE = "var(--lbl-tile, #262A2E)";
const ALARM = "var(--lbl-alarm, #C8CDD2)";
const ACCENT = "#F0B460";

export interface MaskPanelProps {
  nodeCount: number;
  /** Overlap with the model's polygon, 0..1, or null when there was none to
      compare against (a bbox-seeded outline). Shown because a "correction" that
      is really a redraw is worth seeing AS IT HAPPENS — the same number the
      server recomputes and stores as `iou_source`. */
  iou: number | null;
  dirty: boolean;
  seededFromBbox: boolean;
  saving: boolean;
  onSave: () => void;
  onRevert: () => void;
  onRemove: () => void;
  onCancel: () => void;
}

export function MaskPanel({
  nodeCount,
  iou,
  dirty,
  seededFromBbox,
  saving,
  onSave,
  onRevert,
  onRemove,
  onCancel,
}: MaskPanelProps) {
  return (
    <div
      className="box-border w-full flex flex-col"
      style={{
        background: TILE,
        borderLeft: `3px solid ${ACCENT}`,
        borderRadius: 10,
        padding: 10,
        height: PANEL_H,
        color: INK,
      }}
    >
      <div className="flex items-center gap-2" style={{ height: 24 }}>
        <span className="text-[13px] font-semibold uppercase tracking-[0.08em] leading-none">
          ✎ Outline
        </span>
        <span className="ml-auto font-mono text-[11px] tabular-nums" style={{ color: INK_DIM }}>
          {nodeCount} points
          {iou !== null ? ` · ${Math.round(iou * 100)}% of the original` : ""}
        </span>
      </div>

      <p className="text-[12px] leading-snug mt-2" style={{ color: INK_DIM }}>
        {seededFromBbox
          ? "No outline was stored for this animal — drag the corners onto its edge, and click an edge to add a point."
          : "Drag a point onto the animal's edge · click an edge to add a point · double-click a point to remove it"}
        {" · "}
        <span style={{ color: INK }}>scroll to zoom</span> for a closer look or more
        room around the animal
      </p>

      <div className="flex items-center gap-2 mt-auto">
        <PanelButton onClick={onSave} disabled={!dirty || saving} primary>
          Save outline
        </PanelButton>
        <PanelButton onClick={onRevert} disabled={!dirty || saving}>
          Revert
        </PanelButton>
        {/* The false-positive verdict. It sits apart from the two edit actions
            and carries the alarm border, because it is the only button here
            that ENDS the item — everything else returns to the questions. */}
        <PanelButton onClick={onRemove} disabled={saving} alarm>
          Not a cow — remove
        </PanelButton>
        <PanelButton onClick={onCancel} disabled={saving} className="ml-auto">
          Cancel
        </PanelButton>
      </div>

      <div className="text-[11px] mt-2" style={{ color: INK_DIM }}>
        <Kbd>Enter</Kbd> save · <Kbd>R</Kbd> revert · <Kbd>H</Kbd> hold to hide ·{" "}
        <Kbd>scroll</Kbd> zoom · <Kbd>Esc</Kbd> back to the questions
      </div>
    </div>
  );
}

function PanelButton({
  children,
  onClick,
  disabled,
  primary,
  alarm,
  className,
}: {
  children: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
  alarm?: boolean;
  className?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={
        "inline-flex items-center h-8 px-3 rounded-lg border box-border text-[12px] cursor-pointer " +
        "transition-opacity duration-150 hover:opacity-80 disabled:opacity-40 disabled:cursor-default " +
        "focus-visible:outline focus-visible:outline-2 " +
        (className ?? "")
      }
      style={{
        background: primary ? ACCENT : CARD,
        borderColor: alarm ? ALARM : primary ? ACCENT : HAIRLINE,
        color: primary ? "#14161A" : alarm ? ALARM : INK,
        outlineColor: INK,
      }}
    >
      {children}
    </button>
  );
}

function Kbd({ children }: { children: React.ReactNode }) {
  return (
    <span
      className="inline-block rounded px-1 font-mono"
      style={{ border: `1px solid ${HAIRLINE}`, color: INK }}
    >
      {children}
    </span>
  );
}
