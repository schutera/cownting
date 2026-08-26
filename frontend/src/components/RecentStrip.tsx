import type { CSSProperties } from "react";

/* The "Recent" block of the Label side panel (UX §2.5.4): the last ten items
 * this annotator answered, newest first, as bare crop thumbnails.
 *
 * It exists to make ArrowLeft AIMED rather than blind. With Save and Skip both
 * abolished, stepping back along the tape is the only recovery mechanism there
 * is, and an annotator who has to press ArrowLeft four times to find the cow
 * they mis-keyed will press it a fifth. Clicking a thumbnail jumps straight
 * there in `review` phase (§3.6), which is Prodigy's "ten most recent decisions
 * remain editable" sidebar and the thing that makes a Save-less flow feel safe.
 *
 * THUMBNAILS ONLY — the chosen classes are deliberately NOT rendered (§6.2.2).
 * The research recommended showing the two chosen icons per row; a visible list
 * of what you just answered is precisely what turns a correction into
 * harmonisation, and harmonisation raises inter-rater agreement while the
 * labels get worse — the one failure direction that looks like success. The
 * thumbnail is enough to aim at; it is not enough to make your run look tidy.
 *
 * Ten slots are ALWAYS rendered, empty ones as placeholders. Reserved geometry
 * is the same rule the definition slot follows: the panel must not grow by 43 px
 * on the eleventh item and shove the keys legend down the page while the
 * annotator is reading it.
 */

// Prodigy's `history_length: 10`, and the same ten items the page keeps in
// memory with their answers so a revisit costs no round trip (§3.5).
export const MAX_RECENT = 10;

export interface RecentItem {
  instance_key: string;
  /** Server-built crop URL, dropped straight into <img src> — a client-built
      one would go through withDs() and 404 for any day but the selected one. */
  crop_url: string;
}

export interface RecentStripProps {
  /** Newest first. Anything past MAX_RECENT is ignored rather than rendered,
      so a page that over-collects cannot change this block's height. */
  items: readonly RecentItem[];
  /** The instance on screen, so a revisited item is marked in the strip and the
      annotator can see how far back they have travelled. null on a fresh item. */
  currentKey: string | null;
  /** Jump to that item in `review` phase. The page owns the tape. */
  onJump: (instanceKey: string) => void;
  className?: string;
}

// The Label route's own dark surface (§2.2). The tokens are declared by the
// route's `data-surface="label"` wrapper; the literal fallbacks keep this
// component legible if it is ever mounted outside it (a test, the manual).
const TILE = "var(--lbl-tile, #262A2E)";
const INK = "var(--lbl-ink, #E8EAEC)";
const INK_DIM = "var(--lbl-ink-dim, #9AA1A7)";
const HAIRLINE = "rgba(255, 255, 255, 0.09)";

export function RecentStrip({ items, currentKey, onJump, className }: RecentStripProps) {
  const shown = items.slice(0, MAX_RECENT);
  // Fixed-length render: the filled slots, then placeholders up to ten.
  const blanks = Math.max(0, MAX_RECENT - shown.length);

  return (
    <div
      className={"grid grid-cols-5 gap-1.5" + (className ? " " + className : "")}
      style={{ gridTemplateColumns: "repeat(5, 40px)" }}
    >
      {shown.map((it, i) => {
        const isCurrent = it.instance_key === currentKey;
        // The selected thumbnail is marked by a ring and by aria-current, never
        // by hue alone (§5, WCAG 1.4.1).
        const style: CSSProperties = {
          background: TILE,
          borderColor: isCurrent ? INK : HAIRLINE,
          borderWidth: isCurrent ? 2 : 1,
          outlineColor: INK,
        };
        return (
          <button
            key={it.instance_key}
            type="button"
            onClick={() => onJump(it.instance_key)}
            aria-current={isCurrent ? "true" : undefined}
            // Position, not content: naming the classes here would leak into the
            // screen-reader path exactly what §6.2 keeps off the visual one.
            aria-label={i === 0 ? "review the item you just answered" : `review ${i + 1} items back`}
            title={i === 0 ? "1 item back" : `${i + 1} items back`}
            className="w-10 h-10 rounded-md overflow-hidden border box-border p-0 cursor-pointer
                       transition-opacity duration-150 hover:opacity-80
                       focus-visible:outline focus-visible:outline-2"
            style={style}
          >
            <img
              src={it.crop_url}
              alt=""
              loading="lazy"
              decoding="async"
              draggable={false}
              className="w-full h-full object-cover"
            />
          </button>
        );
      })}
      {Array.from({ length: blanks }, (_, i) => (
        <div
          key={`blank-${i}`}
          aria-hidden="true"
          className="w-10 h-10 rounded-md border border-dashed box-border"
          style={{ borderColor: HAIRLINE }}
        />
      ))}
      {shown.length === 0 ? (
        <p className="col-span-5 text-[11px] leading-snug mt-0.5" style={{ color: INK_DIM }}>
          Items you answer appear here — click one to go back to it.
        </p>
      ) : null}
    </div>
  );
}
