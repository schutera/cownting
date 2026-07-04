// How well-*determined* a calibration fit is, computed live from the point counts
// the Calibration page already tracks — no save/backend round-trip needed. Covers
// the full per-camera calibration: the fisheye lines (undistortion), the center
// pairs, and the ground anchors.
//
// Mirrors the backend's polynomial-terms logic: a fit with more free terms needs
// more correspondences before it's actually constrained. An *exactly* determined
// fit (pts == terms) reproduces its own points perfectly (0 px reproj) but has no
// error margin and generalizes poorly — so it must read as NOT good here.
//
// NB: the term count jumps with point count (the backend picks a richer polynomial
// as you add points: deg1→deg2 at 6, deg2→deg3 at 10). So the ratio is NOT
// monotonic — adding one point can switch to a richer model and momentarily lower
// the margin. The `advice` below is computed ladder-aware so it only ever suggests
// counts that actually raise the score.

// Free terms for a warp given `n` correspondences (matches the backend's
// degree-selection ladder: deg3 → 10, deg2 → 6, else affine/deg1 → 3).
export function termsFor(n: number): number {
  if (n >= 10) return 10;
  if (n >= 6) return 6;
  return 3;
}

export type DeterminationStatus =
  | "underdetermined"
  | "exact"
  | "determined"
  | "well-determined";

export interface SubFit {
  name: string; // "lines" | "center" | "ground"
  pts: number;
  terms: number;
  ratio: number;
  color: string;
  binding: boolean; // the weakest stage — it caps the overall score
}

export interface Determination {
  ratio: number; // pts / terms of the binding (weakest) sub-fit
  fill: number; // 0..1 — bar fill fraction
  status: DeterminationStatus;
  label: string; // human status, e.g. "well-determined"
  color: string; // bar/status color
  binding: string; // the binding sub-fit, e.g. "center 5/6" or "ground 4/3"
  subs: SubFit[]; // every active stage, for the breakdown
  advice: string; // concrete next action targeting the weakest stage
}

// Palette (kept in sync with the page's existing inline colors).
const RED = "#e76f51";
const AMBER = "#e58a3c";
const GREEN = "#5f8b6a";

// Human name for what each stage needs more of.
const INPUTS: Record<string, string> = {
  lines: "fisheye lines",
  center: "center pairs",
  ground: "ground/fence anchors",
};

// Lerp between two #rrggbb colors by t in [0,1].
function mix(a: string, b: string, t: number): string {
  const ca = [1, 3, 5].map((i) => parseInt(a.slice(i, i + 2), 16));
  const cb = [1, 3, 5].map((i) => parseInt(b.slice(i, i + 2), 16));
  const c = ca.map((v, i) => Math.round(v + (cb[i] - v) * t));
  return "#" + c.map((v) => v.toString(16).padStart(2, "0")).join("");
}

// Free terms for a given stage at count `n` (mirrors the backend per stage).
function subTerms(name: string, n: number, centerPath: boolean): number {
  if (name === "lines") return 3; // undistortion gate: >= 3 good lines
  if (name === "center") return termsFor(n);
  // ground: the drop (center path) caps at deg2 (6 terms); the ground-only fit
  // follows the full ladder.
  return centerPath ? (n >= 6 ? 6 : 3) : termsFor(n);
}

function colorFor(pts: number, terms: number): string {
  if (pts < terms) return RED;
  const r = pts / terms;
  if (r <= 1) return AMBER;
  if (r >= 2) return GREEN;
  const t = r - 1;
  return t < 0.5 ? mix(RED, AMBER, t / 0.5) : mix(AMBER, GREEN, (t - 0.5) / 0.5);
}

// Smallest number of points to ADD to strictly raise this stage's ratio (searching
// past the degree jumps). Returns the count to add and the ratio it reaches.
function nextImprovement(
  name: string,
  pts: number,
  centerPath: boolean,
): { add: number; ratio: number } | null {
  const cur = pts / subTerms(name, pts, centerPath);
  for (let add = 1; add <= 16; add++) {
    const n = pts + add;
    const r = n / subTerms(name, n, centerPath);
    if (r > cur + 1e-9) return { add, ratio: r };
  }
  return null;
}

// Total points at which this stage becomes well-determined (ratio >= 2).
function strongTarget(name: string, pts: number, centerPath: boolean): number | null {
  for (let n = pts + 1; n <= pts + 80; n++) {
    if (n / subTerms(name, n, centerPath) >= 2) return n;
  }
  return null;
}

const cap = (s: string) => s.charAt(0).toUpperCase() + s.slice(1);

// The concrete "do this next" line for the weakest stage.
function adviseFor(sub: SubFit, centerPath: boolean): string {
  const what = INPUTS[sub.name] ?? "points";
  if (sub.ratio >= 2) return "Well-determined — solid margin across all stages.";
  if (sub.pts < sub.terms) {
    return `Add ${sub.terms - sub.pts} more ${what} — below the minimum to fit.`;
  }
  const step = nextImprovement(sub.name, sub.pts, centerPath);
  if (step) {
    return `Add ${step.add} more ${what} → ${step.ratio.toFixed(1)}× margin.`;
  }
  // Local sweet spot: only a richer (higher-degree) model improves it.
  const strong = strongTarget(sub.name, sub.pts, centerPath);
  const hint = sub.name === "ground" ? " — a fence adds several at once" : "";
  return strong
    ? `${cap(what)} has a good margin; a stronger fit wants ~${strong} total${hint}.`
    : `${cap(what)} has a good margin.`;
}

/**
 * Compute the determination signal from the live point counts.
 *
 * - `nCenter >= 3` → center_pillar path: sub-fits for the fisheye lines, the
 *   center warp, and the drop over the height-0 anchors.
 * - else → ground-only path: fisheye lines + a single ground fit.
 *
 * The binding (weakest) sub-fit sets the overall ratio/color; `advice` says what
 * to add to raise it, and `subs` is the per-stage breakdown.
 */
export function computeDetermination(
  nCenter: number,
  nGroundEff: number,
  nGoodLines: number,
): Determination {
  const centerPath = nCenter >= 3;
  const raw: { name: string; pts: number }[] = [{ name: "lines", pts: nGoodLines }];
  if (centerPath) raw.push({ name: "center", pts: nCenter });
  raw.push({ name: "ground", pts: nGroundEff });

  const subs: SubFit[] = raw.map(({ name, pts }) => {
    const terms = subTerms(name, pts, centerPath);
    return { name, pts, terms, ratio: pts / terms, color: colorFor(pts, terms), binding: false };
  });

  // Binding sub-fit = weakest pts/terms ratio.
  let binding = subs[0];
  for (const s of subs) if (s.ratio < binding.ratio) binding = s;
  binding.binding = true;

  const ratio = binding.ratio;
  const bindingLabel = `${binding.name} ${binding.pts}/${binding.terms}`;
  const advice = adviseFor(binding, centerPath);
  const base = { ratio, binding: bindingLabel, subs, advice };

  if (binding.pts < binding.terms) {
    return { ...base, fill: 0, status: "underdetermined", label: "underdetermined — add more points", color: RED };
  }
  if (ratio === 1) {
    return { ...base, fill: 0.08, status: "exact", label: "exact fit — no error margin", color: AMBER };
  }
  if (ratio >= 2) {
    return { ...base, fill: 1, status: "well-determined", label: "well-determined", color: GREEN };
  }
  const t = ratio - 1; // 0..1
  const fill = 0.08 + t * (1 - 0.08);
  const color = t < 0.5 ? mix(RED, AMBER, t / 0.5) : mix(AMBER, GREEN, (t - 0.5) / 0.5);
  return { ...base, fill, status: "determined", label: "determined", color };
}
