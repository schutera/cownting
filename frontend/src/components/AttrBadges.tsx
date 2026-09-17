import type { FrameDetection } from "../lib/types";
import { ClassGlyph } from "./ClassIcon";

/* The crop model's verdicts, drawn on the frame.
 *
 * One badge per animal, anchored to the top-left of its box: a dark pill with
 * two glyphs — what it is doing, and the light it is in. The glyphs are the
 * taxonomy's own (the same ones on the annotator's answer tiles), so the icon
 * on the image is the icon the label was written with.
 *
 * The pill is the whole design. Text over a photo needs a tonal ground, and a
 * busy overlay of masks needs something quieter than more colour; a 72%-black
 * pill with white line glyphs reads on grass, on panels and on a white cow, and
 * it is the ONLY thing added to the image. No per-class colour — colour is
 * already taken by the mask palette underneath — and no box outline; the mask
 * already says where the animal is.
 *
 * THREE STATES, NOT TWO. A crop the model looked at and declined to name a
 * posture for (`unsure`, attr_behaviour NULL with a rev) gets a question glyph
 * where the posture would be, at reduced weight. A crop the gate rejected gets
 * the whole badge struck through and faded — it is drawn only when the gate is
 * off, and then it must not pass as a cow. A crop the model never looked at
 * gets nothing: an absent badge is "unscored", and drawing a placeholder there
 * would make the unscored day look annotated.
 *
 * Sizes are in natural pixels divided by `invScale`'s inverse — i.e. the
 * caller passes 1/zoom and everything here scales by it — so a badge is the
 * same size on screen at every zoom, like the viewer's own markers.
 */

const BEHAVIOUR_GLYPH: Record<string, string> = {
  "behaviour.feeding": "grass",
  "behaviour.lying": "lying",
  "behaviour.standing": "standing",
  "behaviour.not_visible": "eye-off",
};
const SUN_GLYPH: Record<string, string> = {
  "sun_exposure.shaded": "shade",
  "sun_exposure.direct_sun": "sun",
  "sun_exposure.not_visible": "eye-off",
};

// On-screen size of the badge, in px, before the zoom counter-scale. 18px is
// the smallest height at which a 1.75-unit stroke on a 24-unit glyph still
// resolves as a line rather than a smudge.
const BADGE_H = 18;
const GLYPH = 12;
const PAD = 3;

export function scoredCount(dets: readonly FrameDetection[]): number {
  return dets.filter((d) => d.attr_model_rev !== null).length;
}

/** The SVG layer. Render inside an <svg viewBox="0 0 W H"> in natural px. */
export function AttrBadges({
  dets,
  naturalWidth,
  invScale = 1,
  screenScale = 1,
}: {
  dets: readonly FrameDetection[];
  naturalWidth: number;
  /** 1/zoom from the pan-zoom viewer; keeps badges a constant on-screen size. */
  invScale?: number;
  /** natural px per CSS px of the displayed image, for the plain <img> path
      where the layer is CSS-scaled with the image and has no zoom to counter. */
  screenScale?: number;
}) {
  const k = invScale * screenScale;
  const h = BADGE_H * k;
  const g = GLYPH * k;
  const pad = PAD * k;
  const w = pad * 3 + g * 2;
  const r = h / 2;

  return (
    <g>
      {dets.map((d) => {
        if (d.attr_model_rev === null) return null; // unscored: nothing to say
        const rejected = d.attr_is_cow === false;
        const unsure = d.attr_behaviour === null;
        const beh = unsure ? "question" : (BEHAVIOUR_GLYPH[d.attr_behaviour ?? ""] ?? "dot");
        const sun = SUN_GLYPH[d.attr_sun ?? ""] ?? "dot";
        // Anchor just above the box's top-left corner; clamp to the frame so a
        // cow at the very top edge still gets its badge inside the image.
        const x = Math.max(0, Math.min(d.bbox_x1, naturalWidth - w));
        const y = Math.max(0, d.bbox_y1 - h - pad);
        const title = [
          rejected ? "not a cow (gate)" : null,
          unsure
            ? `posture unsure (${Math.round((d.attr_behaviour_p ?? 0) * 100)}%)`
            : `${(d.attr_behaviour ?? "").split(".").pop()} ${Math.round((d.attr_behaviour_p ?? 0) * 100)}%`,
          `${(d.attr_sun ?? "").split(".").pop()} ${Math.round((d.attr_sun_p ?? 0) * 100)}%`,
        ]
          .filter(Boolean)
          .join(" · ");
        return (
          <g key={d.detection_id} opacity={rejected ? 0.35 : 1}>
            <title>{title}</title>
            <rect x={x} y={y} width={w} height={h} rx={r} fill="#111" fillOpacity={0.72} />
            <g
              fill="none"
              stroke="#fff"
              strokeWidth={1.75}
              strokeLinecap="round"
              strokeLinejoin="round"
              color="#fff"
            >
              <g
                transform={`translate(${x + pad} ${y + (h - g) / 2}) scale(${g / 24})`}
                opacity={unsure ? 0.6 : 1}
                vectorEffect="non-scaling-stroke"
              >
                <ClassGlyph name={beh} />
              </g>
              <g transform={`translate(${x + pad * 2 + g} ${y + (h - g) / 2}) scale(${g / 24})`}>
                <ClassGlyph name={sun} />
              </g>
            </g>
            {rejected ? (
              <line
                x1={x}
                y1={y + h}
                x2={x + w}
                y2={y}
                stroke="#fff"
                strokeWidth={1.5 * k}
                strokeLinecap="round"
              />
            ) : null}
          </g>
        );
      })}
    </g>
  );
}

/** The key under the image. Glyph + word, so the badges are never colour- or
    shape-alone; tertiary text, one line, no chrome. */
export function AttrLegend({ unsure, total }: { unsure: number; total: number }) {
  const item = (name: string, label: string, dim = false) => (
    <span className="inline-flex items-center gap-1" style={{ opacity: dim ? 0.6 : 1 }}>
      <svg
        viewBox="0 0 24 24"
        className="w-3.5 h-3.5"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.75}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <ClassGlyph name={name} />
      </svg>
      {label}
    </span>
  );
  return (
    <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 font-mono text-[11px] text-gray-tertiary">
      {item("grass", "feeding")}
      {item("lying", "lying")}
      {item("standing", "standing")}
      {item("question", `unsure${total ? ` · ${unsure} of ${total}` : ""}`, true)}
      <span className="text-border">|</span>
      {item("sun", "sun")}
      {item("shade", "shade")}
      {item("eye-off", "not visible")}
    </div>
  );
}
