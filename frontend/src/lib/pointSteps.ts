import type { BodyPoint, PointName } from "./types";

/* The body-points step's rules (docs/roadmap/M6_body_points.md §3), with no
 * React in them.
 *
 * The labeler places five points in a FIXED order. The draft is the list of
 * points given so far: entry i is POINT_STEPS[i], `[x, y]` in full-frame px or
 * null for "can't see". Its length is the whole state — the next point to ask
 * for is simply the next index — so there is no cursor to fall out of step with
 * the list, and undo is dropping the last entry.
 *
 * Every function returns a NEW draft and never mutates its argument. A refused
 * action returns the argument itself, so the page can tell "nothing happened"
 * (and shake) by identity.
 */

export interface PointStep {
  name: PointName;
  /** What the panel calls it. */
  label: string;
  /** Where to click, in the labeler's words. */
  where: string;
}

// The order is click order AND storage order, and it must equal the server's
// labels_db.POINT_NAMES (the taxonomy route serves that list; a test compares).
// Head to tail along the back first: those three are the points most often
// visible, so a labeler who can see little still gives the back line.
export const POINT_STEPS: readonly PointStep[] = [
  { name: "nose", label: "Nose", where: "The tip of the muzzle." },
  {
    name: "shoulders",
    label: "Top of the shoulders",
    where: "The highest point of the back, above the front legs.",
  },
  { name: "tail_base", label: "Tail base", where: "Where the tail joins the body." },
  {
    name: "front_hoof",
    label: "Front hoof",
    where: "The front hoof you can see best, where it touches the ground.",
  },
  {
    name: "hind_hoof",
    label: "Hind hoof",
    where: "The hind hoof you can see best, where it touches the ground.",
  },
];

export const POINT_COUNT = POINT_STEPS.length;

export type PointDraft = readonly BodyPoint[];

/** The index of the point to ask for next, or null when all five are given. */
export function nextIndex(d: PointDraft): number | null {
  return d.length < POINT_COUNT ? d.length : null;
}

export function isComplete(d: PointDraft): boolean {
  return d.length >= POINT_COUNT;
}

export function placedCount(d: PointDraft): number {
  return d.filter((p) => p !== null).length;
}

/** Place the next point at `xy`. Refused (same draft back) once all five are given. */
export function place(d: PointDraft, xy: [number, number]): PointDraft {
  if (isComplete(d)) return d;
  return [...d, [xy[0], xy[1]]];
}

/** Whether "can't see" is allowed for the next point.
 *
 * Refused for the LAST point when none of the other four was placed: five
 * "can't see" answers is not a set of body points, and the route rejects it.
 * The labeler has two honest ways out instead — S says the cow is too small,
 * F flags it as occluded or otherwise unanswerable. */
export function canHide(d: PointDraft): boolean {
  if (isComplete(d)) return false;
  return !(d.length === POINT_COUNT - 1 && placedCount(d) === 0);
}

/** Mark the next point "can't see". Refused when canHide() is false. */
export function hide(d: PointDraft): PointDraft {
  return canHide(d) ? [...d, null] : d;
}

/** Take back the last point given. Refused (same draft back) when there is none. */
export function undo(d: PointDraft): PointDraft {
  return d.length === 0 ? d : d.slice(0, -1);
}

/** Whether two drafts say the same thing — what decides that a revisit which
    ends where it started writes nothing. */
export function sameDraft(a: PointDraft, b: PointDraft): boolean {
  if (a.length !== b.length) return false;
  return a.every((p, i) => {
    const q = b[i];
    if (p === null || q === null) return p === q;
    return p[0] === q[0] && p[1] === q[1];
  });
}

/** Whether `xy` (full-frame px) is a point the route will accept.
 *
 * `bounds` is the item's `point_bounds`, the route's own check, so this is the
 * same rule and not a second opinion. Inside the frame too, when its size is
 * known: a hoof is never in the grey padding past the edge of the photo. */
export function insideBounds(
  xy: [number, number],
  bounds: readonly [number, number, number, number] | undefined,
  frameW?: number | null,
  frameH?: number | null,
): boolean {
  const [x, y] = xy;
  if (!Number.isFinite(x) || !Number.isFinite(y)) return false;
  if (bounds !== undefined) {
    const [x0, y0, x1, y1] = bounds;
    if (x < x0 || x > x1 || y < y0 || y > y1) return false;
  }
  if (frameW && (x < 0 || x > frameW)) return false;
  if (frameH && (y < 0 || y > frameH)) return false;
  return true;
}
