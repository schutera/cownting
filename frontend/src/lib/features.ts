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
