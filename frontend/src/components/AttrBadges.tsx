import type { FrameDetection } from "../lib/types";
import { ClassGlyph } from "./ClassIcon";

/* The crop model's verdicts, drawn on the frame.
 *
 * One badge per animal, anchored to the top-left of its box: a dark pill with
 * two glyphs — what it is doing, and the light it is in. The glyphs are the
 * taxonomy's own (the same ones on the annotator's answer tiles), so the icon
 * on the image is the icon the label was written with. The one deliberate
 * exception: sun_exposure.not_visible is drawn as a CLOUD here, not the
 * annotator's eye-off slash, because on this corpus the class is the overcast
 * day (M5 §10) and the dashboard's question is "what was the light?", not
 * "could you see it?".
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
 * HOVER. Under the badges sits one invisible hit shape per scored animal — its
 * traced outline where the segmenter left one, its box otherwise. Pointing at
 * an animal raises its badge to the top of the paint order and doubles it, so
 * in a frame of twenty the question "which badge is this cow's?" has an
 * answer. Only the hit shapes take pointer events; the badges themselves never
 * do, so panning and zooming pass straight through them.
 *
 * Sizes below are CSS px ON SCREEN. They are multiplied by `invScale`
 * (1/zoom) and `screenScale` (natural px per CSS px at zoom 1) to land in the
 * natural-px coordinate space this layer is drawn in, so a badge is the same
 * size on screen at every zoom and every viewport width. The first version
 * forgot the second factor on the zoom-viewer path and drew 18-px badges at
 * ~8 px on a 1920-px frame — legible to nobody.
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
  "sun_exposure.not_visible": "cloud",
};

// On-screen size of the badge in CSS px. 22 px tall with 14-px glyphs is the
// smallest that reads at arm's length over a photo; the stroke is heavier than
// the legend's (2.5 vs 1.75 glyph units) because white-on-dark on a busy
// ground needs more line than grey-on-white beside a word.
const BADGE_H = 22;
const GLYPH = 14;
const PAD = 4;
const STROKE = 2.5;
const HOVER_SCALE = 2;

export function scoredCount(dets: readonly FrameDetection[]): number {
  return dets.filter((d) => d.attr_model_rev !== null).length;
}

function describe(d: FrameDetection, unsure: boolean, rejected: boolean): string {
  const sunWord = (d.attr_sun ?? "").split(".").pop() ?? "";
  return [
    rejected ? "not a cow (gate)" : null,
    unsure
      ? `posture unsure (${Math.round((d.attr_behaviour_p ?? 0) * 100)}%)`
      : `${(d.attr_behaviour ?? "").split(".").pop()} ${Math.round((d.attr_behaviour_p ?? 0) * 100)}%`,
    `${sunWord === "not_visible" ? "no direct light" : sunWord} ${Math.round((d.attr_sun_p ?? 0) * 100)}%`,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** The SVG layer. Render inside an <svg viewBox="0 0 W H"> in natural px. */
export function AttrBadges({
  dets,
  naturalWidth,
  invScale = 1,
  screenScale = 1,
  hovered = null,
  onHover,
}: {
  dets: readonly FrameDetection[];
  naturalWidth: number;
  /** 1/zoom from the pan-zoom viewer; keeps badges a constant on-screen size. */
  invScale?: number;
  /** natural px per CSS px of the displayed image, for the plain <img> path
      where the layer is CSS-scaled with the image and has no zoom to counter. */
  screenScale?: number;
  /** detection_id under the pointer; its badge is drawn last and doubled. */
  hovered?: number | null;
  /** Without it no hit shapes are drawn and the layer is purely visual. */
  onHover?: (id: number | null) => void;
}) {
  const k = invScale * screenScale;
  const h = BADGE_H * k;
  const g = GLYPH * k;
  const pad = PAD * k;
  const w = pad * 3 + g * 2;
  const r = h / 2;

  const scored = dets.filter((d) => d.attr_model_rev !== null); // unscored: nothing to say
  // Paint order is z-order in SVG: the hovered one goes last, i.e. on top.
  const order =
    hovered == null
      ? scored
      : [...scored.filter((d) => d.detection_id !== hovered), ...scored.filter((d) => d.detection_id === hovered)];

  return (
    <g>
      {onHover
        ? scored.map((d) => {
            const rejected = d.attr_is_cow === false;
            const unsure = d.attr_behaviour === null;
            const hit = {
              fill: "transparent",
              stroke: "none",
              style: { pointerEvents: "all" as const },
              onPointerEnter: () => onHover(d.detection_id),
              onPointerLeave: () => onHover(null),
            };
            return d.outline && d.outline.length >= 3 ? (
              <polygon key={d.detection_id} points={d.outline.map((p) => `${p[0]},${p[1]}`).join(" ")} {...hit}>
                <title>{describe(d, unsure, rejected)}</title>
              </polygon>
            ) : (
              <rect
                key={d.detection_id}
                x={d.bbox_x1}
                y={d.bbox_y1}
                width={d.bbox_x2 - d.bbox_x1}
                height={d.bbox_y2 - d.bbox_y1}
                {...hit}
              >
                <title>{describe(d, unsure, rejected)}</title>
              </rect>
            );
          })
        : null}
      {order.map((d) => {
        const rejected = d.attr_is_cow === false;
        const unsure = d.attr_behaviour === null;
        const lifted = d.detection_id === hovered;
        const beh = unsure ? "question" : (BEHAVIOUR_GLYPH[d.attr_behaviour ?? ""] ?? "dot");
        const sun = SUN_GLYPH[d.attr_sun ?? ""] ?? "dot";
        // Anchor just above the top-left corner of the box; clamp to the frame
        // so a cow at the very top edge still gets its badge inside the image.
        const x = Math.max(0, Math.min(d.bbox_x1, naturalWidth - w));
        const y = Math.max(0, d.bbox_y1 - h - pad);
        // Grow from the corner nearest the animal — the bottom-left, so the
        // badge stays put where it points and expands away from the cow — but
        // from the top-left when it is pinned to the top edge, or it would
        // grow out of the frame.
        const originY = y < h ? y : y + h;
        return (
          <g
            key={d.detection_id}
            opacity={rejected ? 0.35 : 1}
            style={{
              transform: `scale(${lifted ? HOVER_SCALE : 1})`,
              transformOrigin: `${x}px ${originY}px`,
              transformBox: "view-box",
              transition: "transform 120ms ease-out",
            }}
          >
            <rect x={x} y={y} width={w} height={h} rx={r} fill="#111" fillOpacity={lifted ? 0.85 : 0.72} />
            <g
              fill="none"
              stroke="#fff"
              strokeWidth={STROKE}
              strokeLinecap="round"
              strokeLinejoin="round"
              color="#fff"
            >
              <g
                transform={`translate(${x + pad} ${y + (h - g) / 2}) scale(${g / 24})`}
                opacity={unsure ? 0.6 : 1}
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
      {item("cloud", "no direct light")}
      <span className="ml-auto">point at a cow to lift its badge</span>
    </div>
  );
}
