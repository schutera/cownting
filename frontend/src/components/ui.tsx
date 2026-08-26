import type { ReactNode, CSSProperties } from "react";

/* The shared text-input skin. Lifted verbatim from Admin.tsx's local `inputCls`
   so the taxonomy editor's fields match the account forms rather than drifting
   into a third variant. Width is deliberately NOT baked in — callers append
   `w-full` where the field should fill its column. */
export const INPUT_CLS =
  "bg-bg border border-border rounded-xl px-3 h-10 box-border text-sm text-text " +
  "focus:outline-none focus:border-accent transition-colors";

/* Soft, rounded card on white. Used for the heatmap, spot-check, etc. */
export function Card({
  children,
  className,
  accent,
  delay,
}: {
  children: ReactNode;
  className?: string;
  accent?: string;
  delay?: number;
}) {
  const style: CSSProperties = {
    animationDelay: `${delay ?? 0}ms`,
    ...(accent ? { borderTop: `3px solid ${accent}` } : {}),
  };
  return (
    <div
      className={
        "bg-surface border border-border rounded-2xl shadow-[0_1px_2px_rgba(43,42,38,0.04),0_8px_24px_-12px_rgba(43,42,38,0.10)] animate-fade-slide-in" +
        (className ? " " + className : "")
      }
      style={style}
    >
      {children}
    </div>
  );
}

/* The side panel container. Sticky on desktop, sits under the heatmap on mobile. */
export function Panel({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <aside
      className={
        "bg-surface border border-border rounded-2xl shadow-[0_1px_2px_rgba(43,42,38,0.04),0_8px_24px_-12px_rgba(43,42,38,0.10)] p-5 sm:p-6 animate-fade-slide-in" +
        (className ? " " + className : "")
      }
    >
      {children}
    </aside>
  );
}

/* A single stat row in the side panel: big number + friendly label. */
export function Stat({
  value,
  label,
  unit,
  accent,
  size = "md",
}: {
  value: ReactNode;
  label: ReactNode;
  unit?: ReactNode;
  accent?: string;
  size?: "hero" | "md";
}) {
  const numClass =
    size === "hero"
      ? "font-display text-5xl sm:text-6xl leading-none tabular-nums"
      : "font-display text-3xl leading-none tabular-nums";
  return (
    <div>
      <div className="flex items-baseline gap-1.5">
        <span className={numClass} style={{ color: accent ?? "var(--color-near-black)" }}>
          {value}
        </span>
        {unit ? <span className="text-sm text-gray-tertiary">{unit}</span> : null}
      </div>
      <div className="text-[13px] text-gray-mid mt-1.5">{label}</div>
    </div>
  );
}

/* Legacy KPI tile — kept for compatibility, restyled to the warm theme. */
export function KpiTile({
  value,
  label,
  accent,
  delay,
}: {
  value: ReactNode;
  label: ReactNode;
  accent?: string;
  delay?: number;
}) {
  const style: CSSProperties = { animationDelay: `${delay ?? 0}ms` };
  return (
    <div
      className="bg-surface border border-border rounded-2xl px-4 py-3 animate-fade-slide-in"
      style={style}
    >
      <div
        className="font-display text-4xl tabular-nums text-near-black"
        style={accent ? { color: accent } : undefined}
      >
        {value}
      </div>
      <div className="text-[12px] text-gray-tertiary mt-1.5">{label}</div>
    </div>
  );
}

/* A two-tone proportion bar, e.g. resting vs. active. */
export function SplitBar({
  fraction,
  leftColor,
  rightColor,
}: {
  fraction: number; // 0..1 filled from the left
  leftColor: string;
  rightColor: string;
}) {
  const pct = Math.max(0, Math.min(1, fraction)) * 100;
  return (
    <div className="h-2.5 w-full rounded-full overflow-hidden flex" style={{ background: rightColor }}>
      <div style={{ width: `${pct}%`, background: leftColor }} />
    </div>
  );
}

/* Small, gently-tracked label. Sentence case — friendlier than uppercase mono. */
export function SectionLabel({
  children,
  className,
}: {
  children: ReactNode;
  className?: string;
}) {
  return (
    <span
      className={
        "text-[12px] font-medium tracking-[0.02em] text-gray-tertiary" +
        (className ? " " + className : "")
      }
    >
      {children}
    </span>
  );
}

/* A keycap badge. The Label page is keyboard-first, so the same binding has to
   appear on the option row, in the permanent legend, in the `?` sheet and in the
   manual — all fed from lib/labelKeys.ts, all rendered through this, so they
   cannot drift apart. `min-w-6` keeps `1`, `Q` and `?` the same width, which is
   what stops a column of badges looking ragged. */
export function Kbd({ children, className }: { children: ReactNode; className?: string }) {
  return (
    <kbd
      className={
        "inline-grid place-items-center min-w-6 h-6 px-1.5 align-middle rounded-md " +
        "border border-border border-b-2 bg-surface-sunk " +
        "font-mono text-[11px] leading-none text-near-black" +
        (className ? " " + className : "")
      }
    >
      {children}
    </kbd>
  );
}

/* Pill toggle (camera picker, hourly/daily). */
export function Chip({
  children,
  active,
  onClick,
}: {
  children: ReactNode;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={
        "text-[13px] px-3 py-1.5 rounded-full border transition-colors duration-150 " +
        (active
          ? "bg-accent text-white border-accent"
          : "bg-surface text-gray-mid border-border hover:border-accent hover:text-accent-deep")
      }
    >
      {children}
    </button>
  );
}

export function Button({
  children,
  onClick,
  variant = "primary",
  disabled,
  className,
}: {
  children: ReactNode;
  onClick?: () => void;
  variant?: "primary" | "ghost";
  disabled?: boolean;
  className?: string;
}) {
  const base =
    variant === "primary"
      ? "bg-accent text-white text-sm font-medium px-5 py-2.5 rounded-full hover:opacity-90 active:scale-95 transition-all duration-150"
      : "border border-border text-text text-sm px-5 py-2.5 rounded-full hover:border-accent hover:text-accent-deep transition-colors duration-150";
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={
        base +
        (disabled ? " opacity-50 pointer-events-none" : "") +
        (className ? " " + className : "")
      }
    >
      {children}
    </button>
  );
}

/* Reusable "the box is working" indicator: a spinning ring + a label, shown
   anywhere the app waits on the backend (starting with localize-after-save).
   Pass `done` to swap the spinner for a check so it reads as a settled
   confirmation. Font size is inherited so callers can size it in context;
   pure CSS/SVG (Tailwind's animate-spin), no dependencies. */
export function Working({
  label,
  done = false,
  className,
}: {
  label: ReactNode;
  done?: boolean;
  className?: string;
}) {
  return (
    <span
      role="status"
      aria-live="polite"
      className={
        "inline-flex items-center gap-2 text-accent" + (className ? " " + className : "")
      }
    >
      {done ? (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" aria-hidden="true">
          <path
            d="M5 12.5l4.5 4.5L19 7"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      ) : (
        <svg
          className="animate-spin"
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          aria-hidden="true"
        >
          <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="3" strokeOpacity="0.25" />
          <path d="M21 12a9 9 0 0 0-9-9" stroke="currentColor" strokeWidth="3" strokeLinecap="round" />
        </svg>
      )}
      <span>{label}</span>
    </span>
  );
}

/* The settings cog shown over a camera image (opens the area editor). Shared so
   the camera tile and the manual render the exact same icon — change it once. */
export function CogIcon({ className = "w-4 h-4" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="3" />
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
    </svg>
  );
}

/* The (i) disclosure that reveals a class definition on the Label page. Shared
   with the manual for the same reason as CogIcon: the annotator is told to press
   the icon that looks like THIS one. Sized by the caller — it sits inline with
   13px option text on the label rows and with body copy in the manual. */
export function InfoIcon({ className = "w-4 h-4" }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={className}
      aria-hidden="true"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16.5v-5" />
      <path d="M12 7.75h.01" />
    </svg>
  );
}

export function Divider({ label }: { label?: string }) {
  return (
    <div className="flex items-center gap-4 my-6">
      <div className="flex-1 h-px bg-border" />
      {label ? <SectionLabel>{label}</SectionLabel> : null}
      <div className="flex-1 h-px bg-border" />
    </div>
  );
}
