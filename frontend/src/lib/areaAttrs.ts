import {
  ACTIVE_COLOR,
  REST_COLOR,
  FEED_COLOR,
  CHART_MUTED,
  SHELTER_COLOR,
  OPEN_COLOR,
} from "./palette";
import { UNSURE_COLOR } from "./features";
import type { AreaAttrs } from "./api";
import type { PostureBreakdown } from "./types";

/* What the occupancy map draws around each count-area badge.
 *
 * Kept out of AreaMap.tsx because AreaMap is a <canvas>: the drawing cannot be
 * asserted on, but every DECISION it makes can be, and these are the decisions
 * — which composition the ring shows, where it falls back to, and what the
 * aura's colour means. Pure functions, no canvas, no React.
 *
 * Two sources describe the same animals. `posture` is the segmenter's geometric
 * proxy (mask elongation: standing / lying / unknown), present since M1 and
 * available on every row. `attrs` is the crop model's read (feeding / lying /
 * standing, or an abstention), present only where it has scored. The model is
 * strictly better where it exists — it separates feeding from standing, which
 * elongation cannot — so it wins, and the proxy is what an unscored day still
 * gets rather than an empty ring.
 */

export type RingSource = "model" | "posture" | "none";

export interface RingSegment {
  key: string;
  label: string;
  value: number;
  color: string;
}

export interface Ring {
  source: RingSource;
  segments: RingSegment[]; // zero-valued classes removed; [] iff nothing to draw
  total: number;
}

/** The badge ring: the crop model's behaviour mix where it has scored this
 *  area, else the segmenter's posture proxy, else nothing. */
export function ringFor(
  attrs: AreaAttrs | undefined,
  posture: PostureBreakdown | undefined,
): Ring {
  if (attrs && attrs.scored > 0) {
    // `unsure` is drawn, never dropped or folded into a posture: it is the
    // share of animals the model looked at and declined to call, and a ring
    // that hid it would overstate how much of the area is understood.
    const segments = [
      { key: "feeding", label: "feeding", value: attrs.feeding, color: FEED_COLOR },
      { key: "lying", label: "lying", value: attrs.lying, color: REST_COLOR },
      { key: "standing", label: "standing", value: attrs.standing, color: ACTIVE_COLOR },
      { key: "unsure", label: "unsure", value: attrs.unsure, color: UNSURE_COLOR },
    ].filter((s) => s.value > 0);
    // scored > 0 with every bucket empty cannot happen from the API, but a
    // ring of zero segments would divide by zero below, so fall through.
    if (segments.length > 0) {
      return { source: "model", segments, total: attrs.scored };
    }
  }
  if (posture) {
    const total = posture.standing + posture.lying + posture.unknown;
    const segments = [
      { key: "standing", label: "standing", value: posture.standing, color: ACTIVE_COLOR },
      { key: "lying", label: "resting", value: posture.lying, color: REST_COLOR },
      { key: "unknown", label: "unknown", value: posture.unknown, color: CHART_MUTED },
    ].filter((s) => s.value > 0);
    if (total > 0 && segments.length > 0) {
      return { source: "posture", segments, total };
    }
  }
  return { source: "none", segments: [], total: 0 };
}

export interface LightTint {
  key: "direct_sun" | "shaded" | "sun_nv";
  label: string;
  color: string;
  share: number; // dominant / scored, 0..1
}

/* The light this area's animals are in, as ONE colour for the badge's aura.
 *
 * The aura is an existing element — a soft radial glow whose radius already
 * encodes the count — so tinting it costs no new ink, and sun-versus-shade is
 * the question the whole agrivoltaics study turns on. It replaces the aura's
 * previous meaning (the sign of the change since the last render); the pulse
 * on change survives, so "this area just moved" is still visible, while the
 * hue now carries something that is true of the animals rather than of the
 * scrubber.
 *
 * Dominant class, not a blend: three hues mixed by share would land on mud
 * that reads as a fourth category. Ties go to the more informative claim —
 * a real light reading over "could not tell" — because a 50/50 split between
 * sun and no-reading is still an area with sun in it.
 */
export function lightTintFor(attrs: AreaAttrs | undefined): LightTint | null {
  if (!attrs || attrs.scored <= 0) return null;
  const options: LightTint[] = [
    { key: "direct_sun", label: "in sun", color: OPEN_COLOR, share: attrs.direct_sun },
    { key: "shaded", label: "in shade", color: SHELTER_COLOR, share: attrs.shaded },
    { key: "sun_nv", label: "no light reading", color: CHART_MUTED, share: attrs.sun_nv },
  ];
  let best: LightTint | null = null;
  for (const o of options) {
    if (o.share <= 0) continue;
    // Strictly greater keeps the earlier (more informative) option on a tie,
    // and the array is ordered sun, shade, no-reading for exactly that reason.
    if (best === null || o.share > best.share) best = o;
  }
  if (best === null) return null;
  return { ...best, share: best.share / attrs.scored };
}

/** The ring legend for a whole map: model wherever any visible area has been
 *  scored, else the proxy. One key per map, not one per badge. */
export function ringLegend(
  rings: readonly Ring[],
): { source: RingSource; entries: { key: string; label: string; color: string }[] } {
  const source: RingSource = rings.some((r) => r.source === "model")
    ? "model"
    : rings.some((r) => r.source === "posture")
      ? "posture"
      : "none";
  if (source === "none") return { source, entries: [] };
  // Every class the source CAN show, not only the ones currently non-zero: a
  // key that changes as cows move is a key nobody can learn.
  const entries =
    source === "model"
      ? [
          { key: "feeding", label: "feeding", color: FEED_COLOR },
          { key: "lying", label: "lying", color: REST_COLOR },
          { key: "standing", label: "standing", color: ACTIVE_COLOR },
          { key: "unsure", label: "unsure", color: UNSURE_COLOR },
        ]
      : [
          { key: "standing", label: "standing", color: ACTIVE_COLOR },
          { key: "lying", label: "resting", color: REST_COLOR },
          { key: "unknown", label: "unknown", color: CHART_MUTED },
        ];
  return { source, entries };
}
