// Presentation-only feature registry for the cross-filter analysis view.
// The SQL that produces each feature's crosstab lives in the backend; here we
// only carry how a feature is labelled, what kind of axis it is, and how its
// category values map to the shared palette. Keys MUST match the backend.
import {
  ACTIVE_COLOR,
  REST_COLOR,
  CHART_MUTED,
  SHELTER_COLOR,
  OPEN_COLOR,
  ACCENT_COLORS,
  HEAT,
  heatRamp,
  cameraColor,
} from "./palette";

export type FeatureKind = "categorical" | "temporal_frame" | "temporal_hour";

// The model looked and declined to name a class. Lighter than CHART_MUTED
// ('unscored' / 'not visible') so the two greys read apart in a strip; the
// legend carries the words either way.
export const UNSURE_COLOR = "#e4dfd5";

export interface FeatureDef {
  key: string;
  label: string;
  kind: FeatureKind;
  colorFor: (value: string, domain: (string | number)[], cameras: string[]) => string;
  dropUnknown?: boolean;
}

export const FEATURES: FeatureDef[] = [
  {
    key: "posture",
    label: "Posture",
    kind: "categorical",
    dropUnknown: true,
    colorFor: (value) =>
      value === "standing" ? ACTIVE_COLOR : value === "lying" ? REST_COLOR : CHART_MUTED,
  },
  {
    key: "panel",
    label: "Under panels",
    kind: "categorical",
    dropUnknown: true,
    colorFor: (value) =>
      value === "under panel" ? SHELTER_COLOR : value === "open" ? OPEN_COLOR : CHART_MUTED,
  },
  {
    key: "shade",
    label: "Shade",
    kind: "categorical",
    dropUnknown: true,
    colorFor: (value) =>
      value === "shade" ? SHELTER_COLOR : value === "sun" ? OPEN_COLOR : CHART_MUTED,
  },
  // --- M5: the crop classifier's judgements (docs/roadmap/M5_instance_attrs.md).
  //
  // `dropUnknown` is deliberately OFF on all three, unlike Posture/Shade above.
  // Their unknown bucket is 'unscored', which is not a missing value but a
  // statement about coverage: scoring is resumable and yields to uploads, so a
  // half-scored corpus is an ordinary state. Hiding those rows would leave a
  // pivot whose total silently disagrees with every other total on the page.
  // 'unsure' (the model looked and declined) and 'unscored' (it never looked)
  // are both greys, and deliberately different greys: they are different facts,
  // and the legend names each. Never folded together.
  {
    key: "sun",
    label: "Sun exposure",
    kind: "categorical",
    colorFor: (value) =>
      value === "shaded"
        ? SHELTER_COLOR
        : value === "direct_sun"
          ? OPEN_COLOR
          : value === "unsure"
            ? UNSURE_COLOR
            : CHART_MUTED,
  },
  {
    key: "behaviour",
    label: "Behaviour",
    kind: "categorical",
    // Shares Posture's colours where the classes mean the same thing, so the
    // learned behaviour and the geometric posture read as comparable when
    // pivoted against each other — which is exactly the comparison worth making
    // while the elongation proxy is still in place.
    colorFor: (value) =>
      value === "standing"
        ? ACTIVE_COLOR
        : value === "lying"
          ? REST_COLOR
          : value === "feeding"
            ? ACCENT_COLORS[0]
            : value === "unsure"
              ? UNSURE_COLOR
              : CHART_MUTED,
  },
  {
    key: "region",
    label: "Count area",
    kind: "categorical",
    colorFor: (value, domain) => ACCENT_COLORS[domain.indexOf(value) % 6],
  },
  {
    key: "camera",
    label: "Camera",
    kind: "categorical",
    colorFor: (value, _domain, cameras) => cameraColor(cameras, value),
  },
  {
    key: "hour",
    label: "Hour of day",
    kind: "temporal_hour",
    // Flat heat when used as the "Within" axis (one strip each); a ramp across
    // the day's hours when used as a ratio segment, so the 24 slices stay legible.
    colorFor: (value, domain) => {
      const i = domain.map(String).indexOf(value);
      if (domain.length <= 1 || i < 0) return HEAT;
      return heatRamp(i / (domain.length - 1));
    },
  },
  {
    key: "frame",
    label: "Time of day",
    kind: "temporal_frame",
    colorFor: () => HEAT,
  },
];

const BY_KEY: Record<string, FeatureDef> = Object.fromEntries(
  FEATURES.map((f) => [f.key, f]),
);

export const featureByKey = (k: string): FeatureDef => BY_KEY[k];
