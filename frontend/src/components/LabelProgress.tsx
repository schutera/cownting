import type { LabelGroup, LabelStats } from "../lib/types";
import type { LabelKeyMap } from "../lib/labelKeys";
import { LABEL_ACTIONS } from "../lib/labelKeys";
import { Divider, Kbd, Panel, SectionLabel, SplitBar, Stat } from "./ui";

/* The Label page's side panel: effort stats, the PERMANENT key legend, and the
 * persisted preferences.
 *
 * The legend is derived entirely from lib/labelKeys — the same map the page's
 * hotkey handler and the option-row badges read — so it cannot drift from the
 * real bindings when a poweruser edits the taxonomy. It is permanent, not behind
 * `?`: the annotator learning the ladder glances sideways, they do not open an
 * overlay over the crop they are judging.
 *
 * Preferences are CONTROLLED props, not local state: the page owns persistence
 * (localStorage) because it is the thing that acts on them — auto-submit lives
 * inside its submit logic and the definitions toggle is the same state `I`
 * flips. A second copy here would disagree with the hotkey the first time both
 * were used in one session. The checkboxes are native inputs, which
 * labelKeys.isTypingTarget() exempts from the hotkey guard — clicking one does
 * not kill the shortcuts.
 */

export interface LabelProgressProps {
  /** null while the first fetch is in flight — a quiet placeholder renders. */
  stats: LabelStats | null;
  /** The live taxonomy's groups, for display names — `keys` carries only keys. */
  groups: LabelGroup[];
  /** buildKeyMap(groups), the same instance the page's hotkey handler uses. */
  keys: LabelKeyMap;
  /** Save automatically once every required question is answered. Persisted by
      the page; default OFF — defensible only because U restores the last item
      with its selections intact. */
  autoSubmit: boolean;
  onAutoSubmitChange: (on: boolean) => void;
  /** Whether class definitions are disclosed — the same state `I` toggles. */
  defsShown: boolean;
  onDefsShownChange: (on: boolean) => void;
  className?: string;
}

/* One legend line: what it does on the left, the key(s) on the right. */
function LegendRow({ label, badges }: { label: string; badges: string[] }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="text-[12px] text-gray-mid">{label}</span>
      {badges.length > 0 ? (
        <span className="flex items-center gap-1 shrink-0">
          {badges.map((b) => (
            <Kbd key={b}>{b}</Kbd>
          ))}
        </span>
      ) : (
        <span className="text-[11px] text-gray-tertiary shrink-0">mouse only</span>
      )}
    </div>
  );
}

function PrefRow({
  checked,
  onChange,
  title,
  hint,
}: {
  checked: boolean;
  onChange: (on: boolean) => void;
  title: string;
  hint: string;
}) {
  return (
    <label className="flex items-start gap-2.5 cursor-pointer">
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
        className="accent-accent mt-1"
      />
      <span className="text-[12px] leading-snug text-gray-mid">
        {title}
        <span className="block text-[11px] text-gray-tertiary mt-0.5">{hint}</span>
      </span>
    </label>
  );
}

export function LabelProgress({
  stats,
  groups,
  keys,
  autoSubmit,
  onAutoSubmitChange,
  defsShown,
  onDefsShownChange,
  className,
}: LabelProgressProps) {
  const nameOf = new Map(groups.map((g) => [g.group_key, g.name]));

  // The scope the server ACTUALLY applied — echoed so the panel explains itself
  // if a dataset/camera filter ever narrows what these numbers cover.
  const scoped = stats !== null && (stats.filters.dataset !== null || stats.filters.camera !== null);
  const scope = !scoped
    ? "across every day and camera"
    : "scoped to " +
      [stats?.filters.dataset, stats?.filters.camera].filter((v) => v != null).join(" · ");

  return (
    <Panel className={className}>
      <SectionLabel>PROGRESS</SectionLabel>
      <div className="font-display text-xl text-near-black leading-none mt-1">Labeling effort</div>
      <div className="text-[12px] text-gray-tertiary mt-1.5">{scope}</div>

      {stats === null ? (
        <p className="font-mono text-[11px] text-gray-tertiary mt-4">loading…</p>
      ) : (
        <>
          <div className="grid grid-cols-2 gap-x-4 gap-y-4 mt-5">
            <Stat value={stats.my_labeled.toLocaleString()} label="labeled by you" />
            <Stat value={stats.my_skipped.toLocaleString()} label="skipped by you" />
            <Stat
              value={stats.my_median_ms === null ? "—" : (stats.my_median_ms / 1000).toFixed(1)}
              unit={stats.my_median_ms === null ? undefined : "s"}
              label="median per label"
            />
            <Stat value={stats.remaining.toLocaleString()} label="left in your queue" />
          </div>

          <div className="mt-5">
            <div className="flex items-baseline justify-between gap-2">
              <span className="text-[12px] text-gray-mid">corpus coverage</span>
              <span className="font-mono text-[11px] tabular-nums text-gray-tertiary">
                {stats.pool_covered.toLocaleString()}/{stats.pool_total.toLocaleString()} at target
              </span>
            </div>
            <div className="mt-1.5">
              <SplitBar
                fraction={stats.pool_total > 0 ? stats.pool_covered / stats.pool_total : 0}
                leftColor="var(--color-accent)"
                rightColor="var(--color-surface-sunk)"
              />
            </div>
            <p className="text-[11px] leading-snug text-gray-tertiary mt-1.5">
              {stats.pool_labeled.toLocaleString()} of {stats.pool_total.toLocaleString()} instances
              have at least one answer.
            </p>
          </div>

          {/* With auth off every row is written by annotator 'local', so agreement
              is undefined by construction — say so rather than print a number. */}
          {stats.auth_disabled ? (
            <p className="text-[11px] leading-snug text-gray-tertiary mt-3">
              Sign-in is off, so every answer is recorded as “local” — agreement between
              annotators is undefined until accounts are in use.
            </p>
          ) : (
            <p className="text-[11px] leading-snug text-gray-tertiary mt-3">
              {stats.annotators.toLocaleString()}{" "}
              {stats.annotators === 1 ? "annotator has" : "annotators have"} contributed so far.
            </p>
          )}
        </>
      )}

      <Divider label="keys" />

      <div className="flex flex-col gap-1.5">
        {keys.groups.map((gk) => (
          <LegendRow
            key={gk.group_key}
            label={nameOf.get(gk.group_key) ?? gk.group_key}
            badges={gk.options.map((o) => o.label).filter((l) => l !== "")}
          />
        ))}
        {LABEL_ACTIONS.map((a) => (
          <LegendRow key={a.action} label={a.hint} badges={[a.label]} />
        ))}
      </div>

      <Divider label="preferences" />

      <div className="flex flex-col gap-3">
        <PrefRow
          checked={autoSubmit}
          onChange={onAutoSubmitChange}
          title="Save automatically when every question is answered"
          hint="Off by default — U always brings the last item back with its answers intact."
        />
        <PrefRow
          checked={defsShown}
          onChange={onDefsShownChange}
          title="Show the answer definitions"
          hint="Also on I. Worth leaving open until the definitions feel obvious."
        />
      </div>
    </Panel>
  );
}
