import type { ReactNode } from "react";
import type { LabelIconName } from "../lib/types";

/* The per-class glyph shown on every answer tile.
 *
 * Icons cannot be hardcoded per class_key ANYWHERE in the frontend: powerusers
 * add and archive classes at runtime, so a `class_key -> glyph` table would be
 * wrong the first time anyone edits the taxonomy, and wrong invisibly (a new
 * class would simply have no icon while every other tile had one). The icon
 * therefore travels with the class as a NAME from a fixed vocabulary, and this
 * component is the only place a name becomes pixels.
 *
 * A name, never markup: a poweruser must not be able to store SVG that the app
 * would then inject, and the strict CSP forbids external assets, so no emoji
 * (which renders as a different picture on every OS and cannot be recoloured)
 * and no sprite sheet or icon font. Everything here is inline, stroked in
 * `currentColor`, so a tile's glyph inherits whatever colour the tile is already
 * using — near-black when the tile is filled with the question accent, --lbl-ink
 * when it is not — with no per-state variants to keep in sync.
 *
 * THE DESIGN RULE (M3 labeling UX §4). Options must separate on ONE gross
 * pre-attentive dimension each — silhouette, mass distribution, orientation.
 * Single-feature search runs in parallel; a CONJUNCTION of features collapses to
 * serial search, at which point the icon is just a slower word. The previous set
 * failed this: `shade` (arc + bars + 1.3-1.8 unit ticks) and `sun` (disc + eight
 * rays) shared a silhouette at 16px, which is the Lucide `sun` / `sun-dim`
 * failure mode — and Sun exposure is the variable this study turns on. So:
 *
 *   - Sun exposure is coded by SHAPE PRIMITIVE (§4.1): filled diagonal slab /
 *     radially symmetric starburst / corner-to-corner slash / closed ring.
 *   - Behaviour is coded by POSITION RELATIVE TO A SHARED GROUND LINE (§4.2).
 *     All four postural glyphs carry an identical 1.75px line at y=20 from x=2
 *     to x=22; the body differs only in where it sits against that line and
 *     where its head points. Four separate pictures become one comparison.
 *
 * Nine names, EIGHT distinct glyphs: `question` is deliberately reused for both
 * escape classes so it becomes a question-independent "I don't know" landmark,
 * and `eye-off` is reused for behaviour.not_visible when it lands.
 *
 * Rendering constants are §4's, not taste: 24x24 canvas rendered at 26px (never
 * below 24 — Heroicons ships no 20px OUTLINE set at all, which is a professional
 * icon team's verdict that thin strokes stop resolving there), and stroke-width
 * 1.75 rather than 2 because light-on-dark strokes bloom (Material Symbols
 * exposes a `grade` axis and suggests -50 for reversed contrast for this).
 *
 * An unknown or empty name renders the neutral dot rather than nothing. That is
 * a layout requirement as much as a robustness one: the tile reserves a fixed
 * 26px glyph band, so a missing glyph would leave a hole in the tile grid. A
 * build talking to a newer server (or an operator who has not picked an icon
 * yet) degrades to a bullet and keeps its geometry.
 */

/* THIRD_PARTY_LICENSES
 *
 * `eye-off` and `question` are derived from Lucide (https://lucide.dev),
 * ISC License. Copyright (c) for portions of Lucide are held by Cole Bemis
 * 2013-2022 as part of Feather (MIT). All other copyright (c) for Lucide are
 * held by Lucide Contributors 2022.
 *   Permission to use, copy, modify, and/or distribute this software for any
 *   purpose with or without fee is hereby granted, provided that the above
 *   copyright notice and this permission notice appear in all copies.
 *   THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
 *   WITH REGARD TO THIS SOFTWARE.
 *
 * `shade` takes its horizon-and-partial-disc idea from Phosphor Icons
 * (https://phosphoricons.com) `sun-horizon`, MIT License,
 * Copyright (c) 2023 Phosphor Icons.
 *   Permission is hereby granted, free of charge, to any person obtaining a copy
 *   of this software and associated documentation files (the "Software"), to
 *   deal in the Software without restriction. THE SOFTWARE IS PROVIDED "AS IS",
 *   WITHOUT WARRANTY OF ANY KIND.
 */

// The vocabulary, exported for the taxonomy editor's picker: the list an
// operator may CHOOSE from has to be the list this file can DRAW, or a picked
// icon would silently come back as a dot. Mirrors labels_db.CLASS_ICONS.
export const CLASS_ICON_NAMES: readonly LabelIconName[] = [
  "shade",
  "sun",
  "eye-off",
  "question",
  "grass",
  "lying",
  "standing",
  "probe",
  "dot",
];

// Keyed by the union rather than by `string`, so adding a name to LabelIconName
// without drawing it is a compile error instead of a silent dot in the UI.
// Paths are stroke-only on a 24x24 canvas; the three fills declare
// `fill="currentColor" stroke="none"` locally rather than the <svg> carrying two
// sets of defaults.
const GLYPHS: Record<LabelIconName, ReactNode> = {
  // Shaded — a SOLID DIAGONAL SLAB, the only filled diagonal mass in the set,
  // with a partial sun disc emerging above it and clean empty space below. In a
  // solar field the shade IS a panel edge, so this is domain-literal rather than
  // a weather symbol. Rays are deliberately omitted: the slab carries the
  // meaning, and 1.6-unit rays are 1.3px at 20px, i.e. ink with no signal.
  shade: (
    <>
      <path d="M4.2 11.2a3.8 3.8 0 0 1 7.6 0" />
      <path d="M1.6 15.4 22.4 6.0 22.4 10.6 1.6 20.0 Z" fill="currentColor" stroke="none" />
    </>
  ),
  // Direct sun — a RADIAL STARBURST WITH A FILLED CORE, the only radially
  // symmetric glyph. The centre is filled, not hollow: that single change is
  // what separates it from `shade` under the Lucide sun/sun-dim failure mode,
  // because the two then share no structural element at all — one is a filled
  // lower-left-heavy diagonal mass, the other a filled centred disc.
  sun: (
    <>
      <circle cx="12" cy="12" r="4.2" fill="currentColor" stroke="none" />
      <path d="M12 1.5v3.2M12 19.3v3.2M1.5 12h3.2M19.3 12h3.2" />
      <path d="M4.6 4.6 6.9 6.9M17.1 17.1 19.4 19.4M19.4 4.6 17.1 6.9M6.9 17.1 4.6 19.4" />
    </>
  ),
  // Not visible — a FULL-FRAME SLASH, the only corner-to-corner single line.
  // Simplified from the previous four-path version so it survives 20px; the
  // slash is the load-bearing element. It shares a diagonal with `shade`, so the
  // two are separated by MASS rather than by angle: a filled quad against a
  // 1.75px line.
  "eye-off": (
    <>
      <path d="M3.2 12s3.6-6.2 8.8-6.2S20.8 12 20.8 12s-3.6 6.2-8.8 6.2S3.2 12 3.2 12Z" />
      <circle cx="12" cy="12" r="2.6" />
      <path d="M2 2 22 22" />
    </>
  ),
  // Cannot tell — a FULL-FRAME CLOSED RING, the only closed outline touching the
  // frame. Radius 9.5 rather than 9 so it reads as full-frame against the
  // postural glyphs, whose ground line runs the full width. Shared by both
  // escape classes on purpose: one landmark for "I don't know", in both
  // questions, so it never has to be re-learned when the digits rebind.
  question: (
    <>
      <circle cx="12" cy="12" r="9.5" />
      <path d="M9.3 9.2a2.8 2.8 0 1 1 3.8 2.7c-.8.35-1.1 1-1.1 1.75v.45" />
      <path d="M12 17.6h.01" />
    </>
  ),
  // Feeding — head DOWN, muzzle touching the ground line, two blades at the
  // muzzle. Bottom-left heavy with a descending diagonal: the mass sits low and
  // left where every other postural glyph sits high or right.
  grass: (
    <>
      <path d="M2 20h20" />
      <rect x="11" y="8.2" width="9" height="6" rx="3" />
      <path d="M11.4 10.6 5.6 18.8" />
      <path d="M13.6 14.2V20M18 14.2V20" />
      <path d="M3.4 20c0-1.9.5-3 1.6-3.9M7.4 20c0-1.9-.5-3-1.6-3.9" />
    </>
  ),
  // Lying — body FLUSH ON the line, zero clearance, head raised slightly. It
  // differs from `standing` by exactly one thing, the absence of the leg gap,
  // which is a pure figure/ground difference that survives to 16px — and it is
  // precisely the discriminator the seeded definitions already hand annotators.
  lying: (
    <>
      <path d="M2 20h20" />
      <rect x="3.6" y="14.4" width="12.4" height="5.6" rx="2.8" />
      <path d="M15.6 16.4 18.6 13.6" />
      <circle cx="19.8" cy="12.2" r="1.8" />
    </>
  ),
  // Standing — the same body RAISED, with 6.6 units of visible negative space
  // under it. The leg gap is the whole message.
  standing: (
    <>
      <path d="M2 20h20" />
      <rect x="4.4" y="8" width="11.6" height="5.4" rx="2.6" />
      <path d="M6.8 13.4V20M13.6 13.4V20" />
      <path d="M15.6 9.6 18.6 7" />
      <circle cx="19.8" cy="5.6" r="1.8" />
    </>
  ),
  // Head probing — standing body, neck HORIZONTAL, crossing into an open bracket
  // at the right frame edge. Reaching THROUGH something is the class, and the
  // bracket carries it. Against `standing` the differentiator is neck
  // ORIENTATION (diagonal-up-to-a-circle vs horizontal-into-a-bracket), which is
  // on the parallel-detectable list, so this stays a single-feature difference
  // rather than a conjunction. The previous glyph was a downward arrow between
  // two dashes — i.e. the download icon — and carried no ground line, which
  // broke the family.
  probe: (
    <>
      <path d="M2 20h20" />
      <rect x="2.6" y="8.4" width="9.4" height="5" rx="2.5" />
      <path d="M4.8 13.4V20M10 13.4V20" />
      <path d="M12 10.4h6.2" />
      <path d="M17.2 6.2h4.4v8h-4.4" />
    </>
  ),
  // The fallback. Filled, so it reads as a deliberate bullet rather than as an
  // icon that failed to load.
  dot: <circle cx="12" cy="12" r="3.2" fill="currentColor" stroke="none" />,
};

function isIconName(name: string): name is LabelIconName {
  return (CLASS_ICON_NAMES as readonly string[]).includes(name);
}

export interface ClassIconProps {
  /** LabelClass.icon — a vocabulary name. Deliberately `string`, not
      LabelIconName: the server may hand back a name a newer build introduced,
      and that has to degrade to the dot rather than make the type lie. */
  name: string;
  /** The size, as utility classes. Defaults to the 26px the answer tiles use
      (§4: never below 24px, where 1.75px strokes stop resolving). The taxonomy
      editor passes its own size for inline body copy. `shrink-0` is applied
      regardless — an icon that collapses in a flex row takes the row's text
      alignment with it. */
  className?: string;
}

/* Always renders an <svg> of the requested size, aria-hidden because the class
   name beside it already carries the meaning — announcing "sun graphic" before
   "Direct sun" is noise for a screen reader, not information. */
export function ClassIcon({ name, className = "w-[26px] h-[26px]" }: ClassIconProps) {
  const glyph = isIconName(name) ? GLYPHS[name] : GLYPHS.dot;
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.75"
      strokeLinecap="round"
      strokeLinejoin="round"
      className={"shrink-0 " + className}
      aria-hidden="true"
    >
      {glyph}
    </svg>
  );
}
