import type { LabelClass, LabelGroup } from "./types";

// The Label page's keyboard map, derived from the LIVE taxonomy.
//
// Powerusers add, archive and reorder groups and classes at runtime, so a literal
// key -> class_key map is wrong the first time anyone edits the taxonomy — and
// wrong silently, because a stale binding either does nothing or selects the
// neighbouring answer, which looks like an annotator mistake rather than a bug.
//
// The old module laid a ladder of physical keyboard ROWS over the taxonomy: one
// row per group, so Sun exposure took 1234 and Behaviour took QWERT and both
// listened at once. That ladder is gone. Exactly ONE group is active at a time,
// so there is only ever one set of answer keys to learn: the active group binds
// its options to Y X C V B N, in that order, in the order the options are shown.
// Answering advances to the next unanswered group, which means the same six keys
// mean something different a moment later — that is the point. One row to
// memorise was the cost of listening to both groups at once, and nothing listens
// to both any more.
//
// Y X C V B N is the QWERTZ bottom row, chosen because the annotators are on
// German keyboards. That makes this module's job harder than picking digits
// ever did: 1-9 print the same character in the same place on every layout, but
// letters do not — QWERTZ swaps Y and Z relative to QWERTY, so the physical key
// that prints "Y" here is the one a US-labelled layout calls Z. OPTION_KEYS
// below therefore carries both the printed `letter` (what the keycap shows, and
// what the <Kbd> badge renders) and the layout-independent `code` (what
// KeyboardEvent.code reports for that physical key, always named after its
// US-QWERTY position, e.g. "KeyZ" for the key that prints "Y"). Matching is
// done on `code` first, so a keypress is judged by which key the annotator's
// finger is actually on, never by which OS layout happens to be active.
//
// This module is the single source of truth for the <Kbd> badge on each option
// row, the side panel that lists every binding, and any future manual entry. They
// cannot drift, because they all read from here.

// ArrowLeft is the undo. There is no separate undo action: moving back along the
// tape re-presents the previous instance WITH the answers already given, so
// correcting a mis-key is the same gesture as looking at what you just did, and
// the annotator never has to know that a correction writes a new version.
export type LabelAction = "prev" | "next" | "flag" | "close" | "inspect" | "clean";

// `key` is the KeyboardEvent.key to match (lowercase for letters, since that is
// what an unshifted press reports); `label` is what <Kbd> renders; `hint` is the
// wording the side panel and the manual both show, so renaming an action renames
// it everywhere at once.
export interface LabelActionKey {
  action: LabelAction;
  key: string;
  label: string;
  hint: string;
  /** One or two words for the inline footer legend, where `hint` is too long.
      Carried here rather than mapped at each call site so a legend can never
      mislabel a binding, and so adding an action cannot leave a legend printing
      the wrong word for it. */
  short: string;
}

// The complete action list. Nothing else is bound: no Enter (auto-save is always
// on and is not configurable), no S (skipping is not a thing an annotator can
// do), no U (ArrowLeft), no I (definitions live behind the info icon), no `?`
// (the side panel and the inline footer legend both read this table directly,
// so neither can list something this table doesn't have, or drift on a count
// hand-written here), no Backspace (re-answering a group overwrites it, and a
// cleared item would only ever be a half-saved one).
export const LABEL_ACTIONS: readonly LabelActionKey[] = [
  { action: "prev", key: "ArrowLeft", label: "←", hint: "previous — review and correct", short: "back" },
  { action: "next", key: "ArrowRight", label: "→", hint: "next", short: "next" },
  // A HOLD, not a toggle — the page owns the keydown/keyup pair and returns
  // before this table is consulted, so listing it here is purely so every legend
  // that reads LABEL_ACTIONS advertises it. It was previously bound but written
  // down only as trailing prose in one footer, which is a binding nobody finds.
  { action: "inspect", key: " ", label: "Space", hint: "hold to see the whole frame", short: "hold for full frame" },
  // Also a HOLD, not a toggle — same pattern as "inspect" above, and for the
  // same reason: the page owns its own keydown/keyup pair, and this entry
  // exists purely so every legend that reads LABEL_ACTIONS advertises it.
  { action: "clean", key: "h", label: "H", hint: "hold to hide the ring and scrim", short: "hold to clean" },
  { action: "flag", key: "f", label: "F", hint: "can't answer this — flag it", short: "flag" },
  { action: "close", key: "Escape", label: "Esc", hint: "close the flag dialog", short: "close" },
];

// Answer keys: the QWERTZ bottom row, positionally matched to the group's
// VISIBLE options in display order — option 0 gets Y, option 1 gets X, and so
// on. `letter` is what the keycap prints on a German keyboard (and what the
// <Kbd> badge shows); `code` is KeyboardEvent.code's layout-independent name
// for that same physical key, always spelled after its US-QWERTY position —
// hence "KeyZ" for the key that prints "Y", because QWERTZ swaps Y and Z
// relative to QWERTY. A seventh option is mouse-only rather than wrapping onto
// a second row — a class list that long is a taxonomy problem, and silently
// reusing an already-bound key is the kind of thing an annotator finds by
// mis-pressing it.
const OPTION_KEYS: readonly { letter: string; code: string }[] = [
  { letter: "Y", code: "KeyZ" },
  { letter: "X", code: "KeyX" },
  { letter: "C", code: "KeyC" },
  { letter: "V", code: "KeyV" },
  { letter: "B", code: "KeyB" },
  { letter: "N", code: "KeyN" },
];

// Derived from LABEL_ACTIONS rather than hand-listed, exactly as the old ladder's
// RESERVED set was: rebinding an action onto one of these letters has to take
// that letter out of the answer keys in the same edit, or one keystroke would
// both fire the action and select an answer on whatever instance replaced this
// one. (Checked here: none of Y X C V B N collide with the current reserved set
// — f, h, and space — so this is a no-op today, but it stays load-bearing the
// moment a future action claims one of those six letters.)
const RESERVED = new Set(
  LABEL_ACTIONS.filter((a) => a.key.length === 1).map((a) => a.key.toLowerCase()),
);

// `key` is null when the option sits past the sixth — that class is mouse-only,
// and `label` is "" so a badge renders nothing rather than a stale one. Both
// carry the printed LETTER (e.g. "Y"), not a digit.
// `index` is the 0-based position in the group's VISIBLE options, which is what
// resolveLabelKey() returns and what the page indexes its own option list with.
export interface LabelOptionKey {
  group_key: string;
  class_key: string;
  index: number;
  key: string | null;
  label: string;
}

// Ties on sort_order are real — everything a poweruser adds defaults to 100 — so
// the immutable key is the tiebreak. Falling back to whatever order the server
// happened to emit would reshuffle the hotkeys between reloads, and reshuffled
// hotkeys are the one thing muscle memory cannot survive. The display name is not
// usable as a tiebreak because a rename would move the keys. This mirrors the
// server's own `ORDER BY sort_order, class_key`, so the two agree by construction
// rather than by luck.
function byOrder<T extends { sort_order: number }>(key: (row: T) => string) {
  return (a: T, b: T): number => {
    if (a.sort_order !== b.sort_order) return a.sort_order - b.sort_order;
    const ka = key(a);
    const kb = key(b);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  };
}

// The options of one group, in the order they are DISPLAYED. Exported because the
// answer keys are positional: if the page rendered a different order than this
// one, every badge would be a lie. Render from this, not from `group.classes`.
// Archived classes are excluded — they are not shown to annotators, and skipping
// them keeps the visible options and the answer keys in step.
export function visibleClasses(group: LabelGroup): LabelClass[] {
  return group.classes.filter((c) => c.active).sort(byOrder<LabelClass>((c) => c.class_key));
}

// Groups in the order they are answered: sun exposure first, behaviour second.
// Archived groups are dropped for the same reason archived classes are.
export function visibleGroups(groups: LabelGroup[]): LabelGroup[] {
  return groups.filter((g) => g.active).sort(byOrder<LabelGroup>((g) => g.group_key));
}

// The Y X C V B N bindings for ONE group — the ACTIVE one. Every group gets
// the same six letters, because only one of them is listening at any moment.
export function optionKeysFor(group: LabelGroup): LabelOptionKey[] {
  return visibleClasses(group).map((cls, i) => {
    const candidate = i < OPTION_KEYS.length ? OPTION_KEYS[i].letter : null;
    const key = candidate !== null && !RESERVED.has(candidate.toLowerCase()) ? candidate : null;
    return {
      group_key: group.group_key,
      class_key: cls.class_key,
      index: i,
      key,
      label: key ?? "",
    };
  });
}

// Shift is deliberately NOT disqualifying: a capslocked annotator sends "F" for
// the same physical key as "f", and refusing it would present as "the flag key
// stopped working". Ctrl/Meta/Alt are disqualifying, because those chords are the
// browser's and the OS's, not ours.
function isPlainPress(e: KeyboardEvent): boolean {
  return !e.ctrlKey && !e.metaKey && !e.altKey;
}

// What a keypress means. `index` is a 0-based position into the ACTIVE group's
// visible options — the same index optionKeysFor() stamps on each option — so the
// page looks the class up rather than parsing keys a second time and drifting.
export type LabelKeyHit =
  | { kind: "option"; index: number }
  | { kind: "action"; action: LabelAction };

// Resolve a keypress against the active group. `optionCount` is how many options
// that group is showing; pass 0 when no group is listening (the flag dialog is
// open, the queue is empty), which leaves the actions live and the answer keys
// inert rather than firing an answer at nothing.
//
// Actions are matched FIRST so the RESERVED guard above cannot be bypassed by a
// letter that is also an action. Callers still have to apply isTypingTarget(): the
// flag dialog's textarea must swallow every binding here EXCEPT "close", because
// Esc has to shut the dialog from inside it.
export function resolveLabelKey(e: KeyboardEvent, optionCount: number): LabelKeyHit | null {
  if (!isPlainPress(e)) return null;
  const key = e.key.length === 1 ? e.key.toLowerCase() : e.key;
  const action = LABEL_ACTIONS.find((a) => a.key === key);
  if (action !== undefined) return { kind: "action", action: action.action };

  // Single-winner option match. `code` is the physical key and is checked
  // across the WHOLE table before any letter is considered; the letter pass
  // below only runs if nothing matched by code. This has to be two full
  // passes, not a per-entry (code === ... || letter === ...) OR: on QWERTZ,
  // physical KeyZ reports key "y" and physical KeyY reports key "z" — printed
  // letters and physical codes cross over between entries — so interleaving
  // the two tests could let one physical keypress satisfy a DIFFERENT entry's
  // letter before the loop ever reached that key's own `code` match, handing
  // one keystroke to two options depending on iteration order. `code` is
  // authoritative; `key` is only a fallback for environments that don't
  // report `code` at all.
  let at = -1;
  for (let i = 0; i < OPTION_KEYS.length; i++) {
    if (OPTION_KEYS[i].code === e.code) {
      at = i;
      break;
    }
  }
  if (at < 0 && key.length === 1) {
    const letter = key.toUpperCase();
    for (let i = 0; i < OPTION_KEYS.length; i++) {
      if (OPTION_KEYS[i].letter === letter) {
        at = i;
        break;
      }
    }
  }
  if (at < 0 || at >= optionCount) return null;
  return { kind: "option", index: at };
}

// Typing targets that must swallow a hotkey. The three excluded input types are
// the point of the function: the answer options ARE native <input type="radio">
// elements (they carry grouping, aria-checked and arrow-key navigation for free),
// so the naive tagName === "INPUT" test kills every shortcut the instant an
// annotator clicks one option with the mouse — the most likely first interaction
// on the page, presenting as "the shortcuts randomly stop working". It matters
// more now than it did: ArrowLeft and ArrowRight ARE the navigation, and a radio
// group that swallowed them would strand the annotator on one instance.
const NON_TYPING_INPUTS = new Set(["radio", "checkbox", "button"]);

export function isTypingTarget(target: EventTarget | null): boolean {
  if (!(target instanceof HTMLElement)) return false;
  if (target.isContentEditable) return true;
  if (target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement) return true;
  if (target instanceof HTMLInputElement) return !NON_TYPING_INPUTS.has(target.type);
  return false;
}
