// Warm & approachable palette (see index.css @theme for the source of truth).
export const CHART_PRIMARY = "#5f8b6a";   // sage — trend line/area
export const CHART_MUTED = "#c9c2b4";     // hairline / axis

// Occupancy heat ramp (amber -> terracotta). Stacks toward hot cores.
export const HEAT = "#e58a3c";
export const HEAT_HOT = "#d5513a";

// Resting vs. active split.
export const REST_COLOR = "#5f8b6a";      // sage
export const ACTIVE_COLOR = "#e58a3c";    // amber

// Under panels vs. open sky split. Teal reads as "shade/shelter" and stays
// distinct from the fence's terracotta orange on the heatmap overlay.
export const SHELTER_COLOR = "#3f8f9c";   // teal — under the panels (shade)
export const OPEN_COLOR = "#e7b84b";      // sun — out in the open

// Generic accent series (kept for any legacy chart usage).
export const ACCENT_COLORS = ["#5f8b6a", "#e58a3c", "#d5513a", "#7c9c8a", "#c9a24b", "#8a6f52"];

// Stable per-camera colour shared by the heatmap dots and the seg-stack bars,
// so a colour means the same camera everywhere. Keyed by index in `cameras`.
export function cameraColor(cameras: string[], cam: string): string {
  const i = cameras.indexOf(cam);
  return ACCENT_COLORS[(i >= 0 ? i : 0) % ACCENT_COLORS.length];
}
