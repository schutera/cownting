/* The questions the cross-filter exists to answer.
 *
 * The controls used to be two rows of eight chips plus a swap button: 64
 * combinations offered flat, with nothing to say which of them mean anything.
 * Most of those pairings are degenerate ("within shade, the ratio of shade") or
 * tautological ("within under-panels, the ratio of shade" restates the
 * geometry), and the three that answer the study's actual question were no more
 * prominent than the sixty that do not.
 *
 * So the pivot keeps every combination reachable, and these name the ones worth
 * reaching for. Each is one click. `available` gates them on features that have
 * data — a preset for a dimension the corpus cannot fill yet is worse than no
 * preset, because it looks like a broken result rather than a missing input.
 */

export interface Preset {
  key: string;
  label: string;
  /** Shown as the caption under the result, so the number has a sentence. */
  question: string;
  group: string; // the "within" dimension
  ratio: string; // the composition inside each group
}

export const PRESETS: readonly Preset[] = [
  {
    key: "shade-by-hour",
    label: "Shade use through the day",
    question: "Of the cows visible in each hour, how many are in shade?",
    group: "hour",
    ratio: "sun",
  },
  {
    key: "doing-in-shade",
    label: "What they do in the shade",
    question: "Of the cows in each light condition, what are they doing?",
    group: "sun",
    ratio: "behaviour",
  },
  {
    key: "where-herd-sits",
    label: "Where the herd sits",
    question: "Of the cows in each count area, what are they doing?",
    group: "region",
    ratio: "behaviour",
  },
  {
    key: "panel-by-hour",
    label: "Panel shelter by hour",
    question: "Of the cows visible in each hour, how many are under a panel?",
    group: "hour",
    ratio: "panel",
  },
];

/** The preset matching the current pivot, if any — so a preset chip can show as
 *  active after a click, and go inactive the moment a dropdown is touched. */
export function activePreset(group: string, ratio: string): Preset | null {
  return PRESETS.find((p) => p.group === group && p.ratio === ratio) ?? null;
}

/** Presets whose two dimensions both have data. A preset pointing at an empty
 *  dimension renders as an empty chart, which reads as breakage. */
export function usablePresets(available: Record<string, boolean>): Preset[] {
  return PRESETS.filter(
    (p) => available[p.group] !== false && available[p.ratio] !== false,
  );
}

/* The sentence the controls read as. Built here rather than inline so the two
 * halves cannot drift apart, and so the wording is testable: "of cows <in each
 * X>, how many are <Y>?" is a conditional proportion stated the way a person
 * would ask it, which "Within X / ratio of Y" never was.
 *
 * `groupPhrase` deliberately carries the "in each" — a dimension label alone
 * ("Hour of day") does not read as a sentence, and putting the preposition in
 * the static text instead would break for dimensions that need a different one.
 */
export function groupPhrase(label: string): string {
  return `in each ${label.toLowerCase()}`;
}

export function ratioPhrase(label: string): string {
  return label.toLowerCase();
}

/** The whole question, for the caption under the chart. */
export function questionFor(
  group: string,
  ratio: string,
  labelOf: (key: string) => string,
): string {
  const preset = activePreset(group, ratio);
  if (preset) return preset.question;
  return `Of the cows ${groupPhrase(labelOf(group))}, what is the split by ${ratioPhrase(labelOf(ratio))}?`;
}
