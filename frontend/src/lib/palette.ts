// Warm & approachable palette (see index.css @theme for the source of truth).
export const CHART_PRIMARY = "#5f8b6a";   // sage — trend line/area
export const CHART_MUTED = "#c9c2b4";     // hairline / axis

// Occupancy heat ramp (amber -> terracotta). Stacks toward hot cores.
export const HEAT = "#e58a3c";
export const HEAT_HOT = "#d5513a";

// Position `t` in [0,1] along the amber->terracotta heat ramp. Used to give the
// hours of the day distinct-but-related hues when time-of-day is a ratio segment.
export function heatRamp(t: number): string {
  const c = Math.max(0, Math.min(1, t));
  const lerp = (a: number, b: number) => Math.round(a + (b - a) * c);
  const hex = (s: string) => [1, 3, 5].map((i) => parseInt(s.slice(i, i + 2), 16));
  const [r1, g1, b1] = hex(HEAT);
  const [r2, g2, b2] = hex(HEAT_HOT);
  const to = (n: number) => n.toString(16).padStart(2, "0");
  return `#${to(lerp(r1, r2))}${to(lerp(g1, g2))}${to(lerp(b1, b2))}`;
}

// Resting vs. active split.
export const REST_COLOR = "#5f8b6a";      // sage
export const ACTIVE_COLOR = "#e58a3c";    // amber

// Under panels vs. open sky split. Teal reads as "shade/shelter" and stays
// distinct from the fence's terracotta orange on the heatmap overlay.
export const SHELTER_COLOR = "#3f8f9c";   // teal — under the panels (shade)
export const OPEN_COLOR = "#e7b84b";      // sun — out in the open

// Generic accent series — 12 curated, mutually distinguishable warm/earthy hues,
// ordered so the low-index cameras (the common case) sit far apart on the wheel.
export const ACCENT_COLORS = [
  "#5f8b6a", // sage green
  "#e58a3c", // amber
  "#4c92a6", // teal
  "#d5513a", // terracotta
  "#c9a24b", // gold
  "#9b6bb0", // plum
  "#8a6f52", // brown
  "#5b7bc4", // slate blue
  "#cf6f9c", // rose
  "#7ba23f", // olive
  "#7a6bc0", // indigo
  "#a8518a", // magenta
];

// HSL -> hex (h in degrees, s/l in [0,1]). Used to synthesise extra camera
// colours past the curated palette.
function hslToHex(h: number, s: number, l: number): string {
  const a = s * Math.min(l, 1 - l);
  const f = (n: number) => {
    const k = (n + h / 30) % 12;
    const c = l - a * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(255 * c)
      .toString(16)
      .padStart(2, "0");
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

// Stable per-camera colour shared by the heatmap dots and the seg-stack bars,
// so a colour means the same camera everywhere. Keyed by index in `cameras`.
// The first 12 cameras use the curated palette; beyond that, colours are
// generated on a golden-angle hue rotation so any N stays distinct (never wraps).
export function cameraColor(cameras: string[], cam: string): string {
  const i = cameras.indexOf(cam);
  const idx = i >= 0 ? i : 0;
  if (idx < ACCENT_COLORS.length) return ACCENT_COLORS[idx];
  const hue = ((idx - ACCENT_COLORS.length) * 137.508 + 20) % 360;
  return hslToHex(hue, 0.45, 0.52);
}
