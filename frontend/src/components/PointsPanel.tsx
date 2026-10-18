import type { ReactNode } from "react";
import { oneClick } from "../lib/clicks";
import {
  POINT_COUNT,
  POINT_STEPS,
  canHide,
  isComplete,
  nextIndex,
  type PointDraft,
} from "../lib/pointSteps";
import { PANEL_H } from "./QuestionPanel";
import { StickCow } from "./StickCow";

/* The body-points step's controls (docs/roadmap/M6_body_points.md §3.2).
 *
 * It occupies QuestionPanel's rectangle at exactly PANEL_H, the layout contract
 * MaskPanel documents: the crop above is sized by subtracting fixed chrome from
 * the viewport, so a taller panel would resize the photograph the labeler is
 * clicking on at the moment the step changes.
 *
 * The click itself happens on the PHOTO; this panel only says which point is
 * next, shows where it is on a cow, and carries the three things a click cannot
 * say. Amber is the geometry steps' colour (the outline is amber too): placing
 * points is a statement about the animal's shape, not an answer to a question,
 * and the panel must not borrow a question's tone.
 */

const HAIRLINE = "var(--lbl-line, rgba(255, 255, 255, 0.09))";
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const CARD = "var(--lbl-card, #1E2124)";
const TILE = "var(--lbl-tile, #262A2E)";
const ACCENT = "#F0B460";

export interface PointsPanelProps {
  draft: PointDraft;
  /** The labeler's stored verdict for this cow — used only to say "marked too
      small" on a revisit, so the panel does not look empty. */
  savedTooSmall: boolean;
  /** This step's position and the item's step count, for the `[2/4]` counter. */
  stepNumber: number;
  stepCount: number;
  onHide: () => void;
  onUndo: () => void;
  onTooSmall: () => void;
  /** Enter: accept all five and go on. The fifth point never does this by
      itself — the labeler sees the whole set first. */
  onAccept: () => void;
}

export function PointsPanel({
  draft,
  savedTooSmall,
  stepNumber,
  stepCount,
  onHide,
  onUndo,
  onTooSmall,
  onAccept,
}: PointsPanelProps) {
  const next = nextIndex(draft);
  const step = next === null ? null : POINT_STEPS[next];

  let title: ReactNode;
  let body: ReactNode;
  if (savedTooSmall && draft.length === 0) {
    title = "Marked too small";
    body = "→ keeps it. Click the photo to place points instead.";
  } else if (step === null) {
    title = "All five points are given";
    body = (
      <>
        <span style={{ color: INK }}>Enter saves them</span> and goes on to the questions.
        Backspace takes back the last point.
      </>
    );
  } else {
    title = (
      <>
        <span style={{ color: INK_DIM }}>
          {next! + 1} of {POINT_COUNT} ·{" "}
        </span>
        {step.label}
      </>
    );
    body = (
      <>
        {step.where} <span style={{ color: INK }}>Click it on the photo.</span>
      </>
    );
  }

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
          Body points
        </span>
        <span className="ml-auto font-mono text-[11px] tabular-nums" style={{ color: INK_DIM }}>
          {`[${stepNumber}/${stepCount}]`}
        </span>
      </div>

      <div className="flex gap-3 min-h-0" style={{ marginTop: 8, flex: 1 }}>
        <div className="shrink-0">
          <StickCow draft={draft} next={next} accent={ACCENT} ink={INK} dim={INK_DIM} />
        </div>
        <div className="min-w-0 flex-1 flex flex-col">
          <div className="text-[14px] font-semibold leading-tight">{title}</div>
          <p className="text-[12px] leading-snug mt-1" style={{ color: INK_DIM }}>
            {body}
          </p>
          <div className="flex items-center gap-2 mt-auto">
            <KeyButton keyLabel="X" onClick={onHide} disabled={!canHide(draft)}>
              Can't see it
            </KeyButton>
            <KeyButton keyLabel="⌫" onClick={onUndo} disabled={draft.length === 0}>
              Undo
            </KeyButton>
            <KeyButton keyLabel="S" onClick={onTooSmall}>
              Too small
            </KeyButton>
            {/* Last and amber, like the outline step's Enter: it is the one
                action that saves the set, and it waits for all five. */}
            <KeyButton keyLabel="Enter" onClick={onAccept} disabled={!isComplete(draft)} primary>
              Save points
            </KeyButton>
          </div>
        </div>
      </div>
    </div>
  );
}

function KeyButton({
  keyLabel,
  children,
  onClick,
  disabled,
  primary,
}: {
  keyLabel: string;
  children: ReactNode;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={oneClick(onClick)}
      // A click must not leave focus here: the panel stays mounted from one
      // backlog cow to the next, and a focused button is one the browser
      // "clicks" on Enter — on a cow nobody has looked at yet.
      onMouseDown={(e) => e.preventDefault()}
      disabled={disabled}
      className="inline-flex items-center gap-1.5 h-8 px-2.5 rounded-lg border box-border text-[12px] cursor-pointer
                 transition-opacity duration-150 hover:opacity-80 disabled:opacity-40 disabled:cursor-default
                 focus-visible:outline focus-visible:outline-2"
      style={{
        background: CARD,
        borderColor: primary ? ACCENT : HAIRLINE,
        color: INK,
        outlineColor: INK,
      }}
    >
      <span
        className="grid place-items-center rounded px-1 font-mono text-[11px] font-bold leading-none"
        style={{
          minWidth: 18,
          height: 18,
          background: primary ? ACCENT : HAIRLINE,
          color: primary ? "#14161A" : undefined,
        }}
      >
        {keyLabel}
      </span>
      {children}
    </button>
  );
}
