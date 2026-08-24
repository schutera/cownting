import type { LabelClass, LabelGroup } from "./types";

// The Label page's keyboard map, derived from the LIVE taxonomy.
//
// Powerusers add, archive and reorder groups and classes at runtime, so a literal
// key -> class_key map is wrong the first time anyone edits the taxonomy — and
// wrong silently, because a stale binding either does nothing or selects the
// neighbouring answer, which looks like an annotator mistake rather than a bug.
// So a fixed ladder of physical keyboard rows is laid over whatever the taxonomy
// currently is: group i takes row i, class j takes the j-th unreserved character
// of that row. Two adjacent rows under one hand, spatially mirroring the two
// stacked question groups on screen.
//
// This module is the single source of truth for the <Kbd> badge on each option
// row, the permanent legend under the progress panel, the `?` sheet, the manual
// and the taxonomy editor's live reorder preview — they cannot drift, because
// they all read from here.

export type LabelAction =
  | "save"
  | "skip"
  | "flag"
  | "undo"
  | "definitions"
  | "hideRing"
  | "clear"
  | "help"
  | "close";

// `key` is the KeyboardEvent.key to match (lowercase for letters, since that is
// what an unshifted press reports); `label` is what <Kbd> renders; `hint` is the
// wording the legend, the `?` sheet and the manual all show, so renaming an
// action renames it everywhere at once.
export interface LabelActionKey {
  action: LabelAction;
  key: string;
  label: string;
  hint: string;
}

export const LABEL_ACTIONS: readonly LabelActionKey[] = [
  { action: "save", key: "Enter", label: "Enter", hint: "save" },
  { action: "skip", key: "s", label: "S", hint: "skip" },
  { action: "flag", key: "f", label: "F", hint: "flag" },
  { action: "undo", key: "u", label: "U", hint: "undo last" },
  { action: "definitions", key: "i", label: "I", hint: "toggle definitions" },
  { action: "hideRing", key: "h", label: "H", hint: "hold to hide ring" },
  { action: "clear", key: "Backspace", label: "Backspace", hint: "clear answers" },
  { action: "help", key: "?", label: "?", hint: "all keys" },
  { action: "close", key: "Escape", label: "Esc", hint: "close overlay" },
];

// The ladder. Row order is the group order, left-to-right within a row is the
// class order.
export const KEY_ROWS: readonly string[] = ["1234567890", "qwertyuiop", "asdfghjkl", "zxcvbnm"];

// Derived from LABEL_ACTIONS rather than hand-listed: rebinding skip from S to X
// has to take X out of the ladder in the same edit, or one keystroke would both
// skip the instance and select an answer on the instance that replaced it.
const RESERVED = new Set(
  LABEL_ACTIONS.filter((a) => a.key.length === 1).map((a) => a.key.toLowerCase()),
);

// `key` is null when the row ran out of unreserved characters — that class is
// mouse-only, and `label` is "" so a badge renders nothing rather than a stale one.
export interface LabelOptionKey {
  group_key: string;
  class_key: string;
  key: string | null;
  label: string;
}
// `row` is null when the group sits past the end of the ladder; the editor labels
// such a group mouse-only rather than pretending it has bindings.
export interface LabelGroupKeys {
  group_key: string;
  row: number | null;
  options: LabelOptionKey[];
}
// `byKey` is looked up with a lowercased KeyboardEvent.key. Collisions are
// impossible by construction: the rows are disjoint character sets and each row
// serves at most one group.
export interface LabelKeyMap {
  groups: LabelGroupKeys[];
  byKey: Record<string, LabelOptionKey>;
  byClass: Record<string, LabelOptionKey>;
}

// Ties on sort_order are real — everything a poweruser adds defaults to 100 — so
// the immutable key is the tiebreak. Falling back to whatever order the server
// happened to emit would reshuffle the hotkeys between reloads, and reshuffled
// hotkeys are the one thing muscle memory cannot survive. The display name is not
// usable as a tiebreak because a rename would move the keys.
function byOrder<T extends { sort_order: number }>(key: (row: T) => string) {
  return (a: T, b: T): number => {
    if (a.sort_order !== b.sort_order) return a.sort_order - b.sort_order;
    const ka = key(a);
    const kb = key(b);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  };
}

function rowChars(row: number): string[] {
  if (row < 0 || row >= KEY_ROWS.length) return [];
  return [...KEY_ROWS[row]].filter((c) => !RESERVED.has(c));
}

// Archived groups and classes get no bindings: they are not shown to annotators,
// and skipping them keeps the visible options and the ladder in step. The editor
// therefore also sees archived rows as key-less, which is the truth.
export function buildKeyMap(groups: LabelGroup[]): LabelKeyMap {
  const map: LabelKeyMap = { groups: [], byKey: {}, byClass: {} };
  const ordered = groups.filter((g) => g.active).sort(byOrder<LabelGroup>((g) => g.group_key));

  ordered.forEach((group, i) => {
    const row = i < KEY_ROWS.length ? i : null;
    const chars = row === null ? [] : rowChars(row);
    const classes = group.classes
      .filter((c) => c.active)
      .sort(byOrder<LabelClass>((c) => c.class_key));

    const options = classes.map((cls, j) => {
      const key = j < chars.length ? chars[j] : null;
      const option: LabelOptionKey = {
        group_key: group.group_key,
        class_key: cls.class_key,
        key,
        label: key === null ? "" : key.toUpperCase(),
      };
      if (key !== null) map.byKey[key] = option;
      map.byClass[cls.class_key] = option;
      return option;
    });

    map.groups.push({ group_key: group.group_key, row, options });
  });

  return map;
}

// Shift is deliberately NOT disqualifying: `?` is Shift+/ on most layouts, and a
// capslocked annotator sends "Q" for the same physical key as "q". Ctrl/Meta/Alt
// are, because those are the browser's and the OS's, not ours.
function isPlainPress(e: KeyboardEvent): boolean {
  return !e.ctrlKey && !e.metaKey && !e.altKey;
}

export function actionForEvent(e: KeyboardEvent): LabelAction | null {
  if (!isPlainPress(e)) return null;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const hit = LABEL_ACTIONS.find((a) => a.key === key);
  return hit === undefined ? null : hit.action;
}

export function optionForEvent(map: LabelKeyMap, e: KeyboardEvent): LabelOptionKey | null {
  if (!isPlainPress(e) || e.key.length !== 1) return null;
  return map.byKey[e.key.toLowerCase()] ?? null;
}

// The keys of one group, for the nudge that fires when Enter lands on an
// unanswered question ("Behaviour — Q W E R T"). A silent no-op there means the
// annotator presses Enter twice, assumes it worked and moves on.
export function groupKeyHint(map: LabelKeyMap, groupKey: string): string {
  const group = map.groups.find((g) => g.group_key === groupKey);
  if (group === undefined) return "";
  return group.options
    .map((o) => o.label)
    .filter((l) => l !== "")
    .join(" ");
}

// Typing targets that must swallow a hotkey. The three excluded input types are
// the point of the function: the answer options ARE native <input type="radio">
// elements (they carry grouping, aria-checked and arrow-key navigation for free),
// so the naive tagName === "INPUT" test kills every shortcut the instant an
// annotator clicks one option with the mouse — the most likely first interaction
// on the page, presenting as "the shortcuts randomly stop working".
const NON_TYPING_INPUTS = new Set(["radio", "checkbox", "button"]);

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) return !NON_TYPING_INPUTS.has(target.type);
  return false;
}
