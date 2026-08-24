import type { LabelClass, LabelGroup } from "../lib/types";
import type { LabelKeyMap } from "../lib/labelKeys";
import { InfoIcon, Kbd } from "./ui";

/* The question stack: one block per group, one styled row per answer option.
 *
 * The options are visually-hidden NATIVE inputs (`radio`, or `checkbox` for a
 * multi-select group) inside styled <label> rows — grouping, `aria-checked`,
 * arrow-key navigation and screen-reader announcement come for free, versus a
 * roving-tabindex reimplementation to get wrong. `Chip` is deliberately not
 * reused: it renders a bare <button> with no `type`, no `disabled` and no aria
 * passthrough. labelKeys.isTypingTarget() excludes radio/checkbox from the
 * hotkey guard for exactly this structure, so clicking an option with the mouse
 * does not kill the shortcuts.
 *
 * Definitions expand INLINE beneath the row, never in a tooltip or popover: a
 * popover covers the crop — the exact pixels the definition must be compared
 * against — and tooltips are hover-only, so dead on touch, invisible to a
 * keyboard, and gone mid-read. The disclosure region is always mounted (hidden,
 * not unmounted) so `aria-controls` always points at a real element.
 *
 * All state lives in the page: it owns `answers` (its hotkey handler drives the
 * same onAnswer), and it owns `openDefs` — persisting it (cownting.label.defs),
 * toggling it all at once on `I`, and POSTing `info_opened` when one opens.
 */

export interface LabelGroupListProps {
  /** The live taxonomy's groups (taxonomy.groups). Archived groups/classes never
      render: iteration order comes from `keys`, which drops them. */
  groups: LabelGroup[];
  /** buildKeyMap(groups) — built ONCE by the page and shared with its hotkey
      handler, so a row's badge can never show a key the handler ignores. Also
      the render order, which keeps screen order and key-ladder order in step. */
  keys: LabelKeyMap;
  /** Current selections in the submit body's shape: a class_key for a
      single-select group, a class_key list for a multi-select one. */
  answers: Record<string, string | string[]>;
  /** Fired on any option activation, mouse or arrow-key. The page implements the
      semantics once — set for single-select, toggle for multi-select — so its
      hotkeys and these inputs cannot behave differently. */
  onAnswer: (groupKey: string, classKey: string) => void;
  /** group_keys / class_keys whose definition is disclosed. The two namespaces
      cannot collide: a class_key always contains a dot, a group_key never does. */
  openDefs: ReadonlySet<string>;
  onToggleDef: (key: string) => void;
  /** True while a submit is in flight or no item is on screen. Definitions stay
      readable — only the inputs lock. */
  disabled?: boolean;
}

/* One (i) disclosure trigger. A SIBLING of the option's <label>, not a child:
   a click inside a label also activates its input, so nesting would make
   "read the definition" silently answer the question. */
function InfoButton({
  open,
  controls,
  label,
  onClick,
}: {
  open: boolean;
  controls: string;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={controls}
      aria-label={label}
      onClick={onClick}
      className={
        "grid shrink-0 place-items-center w-6 h-6 rounded-full transition-colors duration-150 " +
        (open ? "text-accent bg-accent-soft" : "text-gray-tertiary hover:text-accent hover:bg-accent-soft")
      }
    >
      <InfoIcon className="w-4 h-4" />
    </button>
  );
}

interface OptionProps {
  group: LabelGroup;
  cls: LabelClass;
  /** The <Kbd> text from labelKeys — "" when the row ran out of characters, in
      which case the option is mouse-only and no badge renders. */
  keyLabel: string;
  checked: boolean;
  open: boolean;
  disabled: boolean;
  onAnswer: (groupKey: string, classKey: string) => void;
  onToggleDef: (key: string) => void;
}

function Option({ group, cls, keyLabel, checked, open, disabled, onAnswer, onToggleDef }: OptionProps) {
  const defId = `label-def-${cls.class_key}`;
  return (
    <div
      className={
        "rounded-xl border transition-colors duration-150 focus-within:border-accent " +
        (checked ? "border-accent bg-accent-soft" : "border-border bg-surface hover:border-accent") +
        (disabled ? " opacity-60" : "")
      }
    >
      <div className="flex items-center pr-1.5">
        <label className="flex flex-1 items-center gap-2.5 pl-3 pr-2 py-2 cursor-pointer">
          <input
            type={group.multi_select ? "checkbox" : "radio"}
            name={group.group_key}
            value={cls.class_key}
            checked={checked}
            disabled={disabled}
            onChange={() => onAnswer(group.group_key, cls.class_key)}
            className="sr-only"
          />
          {keyLabel !== "" ? <Kbd>{keyLabel}</Kbd> : null}
          <span className={"text-[13px] " + (checked ? "text-accent-deep font-medium" : "text-text")}>
            {cls.name}
          </span>
        </label>
        <InfoButton
          open={open}
          controls={defId}
          label={`definition of “${cls.name}”`}
          onClick={() => onToggleDef(cls.class_key)}
        />
      </div>
      {/* description is required server-side, so there is always something here */}
      <div id={defId} hidden={!open} className="px-3 pb-2.5">
        <p className="text-[12px] leading-snug text-gray-mid">{cls.description}</p>
      </div>
    </div>
  );
}

export function LabelGroupList({
  groups,
  keys,
  answers,
  onAnswer,
  openDefs,
  onToggleDef,
  disabled = false,
}: LabelGroupListProps) {
  const byGroup = new Map(groups.map((g) => [g.group_key, g]));

  return (
    <div className="flex flex-col gap-5">
      {keys.groups.map((gk) => {
        const group = byGroup.get(gk.group_key);
        if (group === undefined) return null;
        const byClass = new Map(group.classes.map((c) => [c.class_key, c]));
        const headingId = `label-q-${group.group_key}`;
        const defId = `label-def-${group.group_key}`;
        const groupOpen = openDefs.has(group.group_key);
        const val = answers[group.group_key];
        const hasDescription = group.description !== null && group.description !== "";

        return (
          <div
            key={group.group_key}
            role={group.multi_select ? "group" : "radiogroup"}
            aria-labelledby={headingId}
            aria-required={!group.multi_select && group.required ? true : undefined}
          >
            <div className="flex items-center gap-1.5">
              <span id={headingId} className="text-sm font-medium text-near-black">
                {group.name}
              </span>
              {group.multi_select ? (
                <span className="text-[11px] text-gray-tertiary">· pick all that apply</span>
              ) : null}
              {hasDescription ? (
                <InfoButton
                  open={groupOpen}
                  controls={defId}
                  label={`about “${group.name}”`}
                  onClick={() => onToggleDef(group.group_key)}
                />
              ) : null}
            </div>
            {hasDescription ? (
              <div id={defId} hidden={!groupOpen}>
                <p className="text-[12px] leading-snug text-gray-mid mt-1">{group.description}</p>
              </div>
            ) : null}

            <div className="flex flex-col gap-2 mt-2">
              {gk.options.map((opt) => {
                const cls = byClass.get(opt.class_key);
                if (cls === undefined) return null;
                const checked = group.multi_select
                  ? Array.isArray(val) && val.includes(cls.class_key)
                  : val === cls.class_key;
                return (
                  <Option
                    key={cls.class_key}
                    group={group}
                    cls={cls}
                    keyLabel={opt.label}
                    checked={checked}
                    open={openDefs.has(cls.class_key)}
                    disabled={disabled}
                    onAnswer={onAnswer}
                    onToggleDef={onToggleDef}
                  />
                );
              })}
            </div>
          </div>
        );
      })}
    </div>
  );
}
