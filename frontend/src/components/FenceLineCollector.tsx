import { useState } from "react";
import type { FenceLink, Line } from "../lib/types";
import { Button, SectionLabel } from "./ui";
import { ImageClicker } from "./ImageClicker";

const MIN_VERTS = 2;

/**
 * Fence-line correspondence collector. The user traces a polyline along a fence
 * in the CAMERA frame and the matching polyline along the same fence in the
 * ORTHOPHOTO. Each fence segment becomes a `FenceLink` = `[camLine, orthoLine]`.
 *
 * Contract:
 * - Trace the fence at its BASE (where the posts meet the ground, not the top
 *   rail) — every vertex becomes a ground anchor for the ground-plane fit.
 * - Click the SAME physical points (posts, corners, bends) in the SAME order on
 *   both images: vertex i on the camera side matches vertex i on the ortho side.
 * - Both polylines of a segment MUST have the SAME number of vertices (≥2);
 *   "Add correspondence" stays disabled until the live counts match.
 *
 * Fence segments are optional but strongly improve the ground-plane fit, since
 * a long fence line pins down the plane far better than scattered points.
 */
export function FenceLineCollector({
  camSrc,
  camW,
  camH,
  orthoSrc,
  orthoW,
  orthoH,
  links,
  onChange,
  fence,
  residuals,
}: {
  camSrc: string;
  camW: number;
  camH: number;
  orthoSrc: string;
  orthoW: number;
  orthoH: number;
  links: FenceLink[];
  onChange: (links: FenceLink[]) => void;
  fence?: number[][] | null; // existing ortho fence polygon, shown as a guide overlay on the ortho pane
  residuals?: number[]; // optional per-correspondence mean reprojection error (px), aligned to links
}) {
  // The two in-progress polylines (not yet committed to `links`).
  const [curCam, setCurCam] = useState<Line>([]);
  const [curOrtho, setCurOrtho] = useState<Line>([]);

  // Phase 2 "shared fence anchors": snap ortho clicks onto the site-wide fence
  // polygon so the same physical corner lands on the EXACT same ortho pixel,
  // regardless of which camera traced it. Never applied to the camera pane.
  const [snap, setSnap] = useState(true);
  const [lastSnap, setLastSnap] = useState<"vertex" | "edge" | "none">("none");

  function placeOrtho(pt: [number, number]) {
    if (snap && fence) {
      const r = snapToFence(pt, fence, orthoW, orthoH);
      setLastSnap(r.kind);
      setCurOrtho((c) => [...c, r.pt]);
    } else {
      setLastSnap("none");
      setCurOrtho((c) => [...c, pt]);
    }
  }

  const diff = curCam.length - curOrtho.length;
  const matched = curCam.length === curOrtho.length && curCam.length >= MIN_VERTS;

  function addCorrespondence() {
    if (!matched) return;
    onChange([...links, [curCam, curOrtho]]);
    setCurCam([]);
    setCurOrtho([]);
  }

  function undoCam() {
    setCurCam((c) => c.slice(0, -1));
  }

  function undoOrtho() {
    setCurOrtho((c) => c.slice(0, -1));
  }

  function deleteLink(i: number) {
    onChange(links.filter((_, idx) => idx !== i));
  }

  const worst = residuals && residuals.length ? Math.max(...residuals) : 0;
  const flagThresh = Math.max(worst * 0.6, 8); // px — badges above this go red

  // Live-count banner.
  const shorter = diff > 0 ? "orthophoto" : "camera";
  const snapNote =
    lastSnap === "vertex"
      ? " · snapped to fence corner"
      : lastSnap === "edge"
        ? " · snapped to fence edge"
        : "";
  const banner =
    (curCam.length === 0 && curOrtho.length === 0
      ? "Trace a fence segment in the camera frame, then the SAME span in the orthophoto — click matching posts/bends in the same order."
      : matched
        ? `${curCam.length} vertices each — ready to add this fence segment.`
        : `Camera: ${curCam.length} · ortho: ${curOrtho.length} — click ${Math.abs(diff)} more vertex(es) on the ${shorter} so both match.`) +
    (curOrtho.length > 0 ? snapNote : "");

  const totalAnchors = links.reduce((sum, l) => sum + l[0].length, 0);

  return (
    <div>
      <p className="text-[13px] text-text mb-3 max-w-3xl">
        Trace the fence at its <strong>base</strong> — where it meets the ground,
        not the top rail. Click the same physical points (posts, corners, bends)
        in the same order on both images; both sides need the same vertex count.
        Each vertex becomes a ground anchor for calibration. Fence segments are
        optional but strongly improve the ground-plane fit. Ortho clicks snap to
        the shared fence polygon so the same corner is identical across cameras.
      </p>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <ImageClicker
          title="Camera frame — trace the fence base"
          src={camSrc}
          naturalWidth={camW}
          naturalHeight={camH}
          mode="polyline"
          points={curCam}
          lines={links.map((l) => l[0])}
          onPlace={(pt) => setCurCam((c) => [...c, pt])}
        />
        <ImageClicker
          title="Orthophoto — trace the same fence"
          src={orthoSrc}
          naturalWidth={orthoW}
          naturalHeight={orthoH}
          mode="polyline"
          points={curOrtho}
          lines={[...(fence ? [fence] : []), ...links.map((l) => l[1])]}
          onPlace={placeOrtho}
        />
      </div>

      <div
        className={
          "mt-4 px-3 py-2 border font-mono text-[12px] " +
          (matched ? "border-accent text-accent" : "border-[#e76f51] text-[#e76f51]")
        }
      >
        {banner}
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" disabled={!matched} onClick={addCorrespondence}>
          Add correspondence
        </Button>
        <Button variant="ghost" disabled={curCam.length === 0} onClick={undoCam}>
          Undo camera vertex
        </Button>
        <Button variant="ghost" disabled={curOrtho.length === 0} onClick={undoOrtho}>
          Undo ortho vertex
        </Button>
        {fence ? (
          <label
            className={
              "inline-flex items-center gap-2 px-3 py-1 border font-mono text-[11px] cursor-pointer select-none " +
              (snap ? "border-accent text-accent" : "border-border text-gray-tertiary")
            }
            title="Snap ortho clicks onto the shared fence polygon so the same corner is identical across cameras"
          >
            <input
              type="checkbox"
              checked={snap}
              onChange={(e) => setSnap(e.target.checked)}
              className="accent-accent"
            />
            Snap to fence
          </label>
        ) : null}
      </div>

      {links.length > 0 ? (
        <div className="mt-5">
          <div className="mb-2">
            <SectionLabel>Fence segments</SectionLabel>
          </div>
          <div className="flex flex-col gap-2">
            {links.map((link, i) => {
              const res = residuals?.[i];
              const bad = res !== undefined && res >= flagThresh;
              return (
                <div
                  key={i}
                  className="flex items-center gap-3 bg-surface border border-border px-3 py-2"
                >
                  <span className="font-mono text-[13px] text-near-black w-8 text-center">
                    #{i + 1}
                  </span>
                  <span className="font-mono text-[11px] text-accent">
                    {link[0].length} vtx
                  </span>
                  {res !== undefined ? (
                    <span
                      className={
                        "font-mono text-[10px] px-2 py-0.5 border " +
                        (bad
                          ? "border-[#e76f51] text-[#e76f51]"
                          : "border-accent text-accent")
                      }
                      title="Mean reprojection error (px)"
                    >
                      {res.toFixed(1)} px{bad ? " ⚠" : ""}
                    </span>
                  ) : null}
                  <div className="ml-auto flex gap-2">
                    <button
                      onClick={() => deleteLink(i)}
                      className="font-mono text-[11px] px-2 py-1 border border-border text-gray-tertiary hover:border-[#e76f51] hover:text-[#e76f51]"
                    >
                      delete
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      <div className="mt-4 font-mono text-[11px] text-gray-tertiary">
        {links.length} fence segment{links.length === 1 ? "" : "s"} · {totalAnchors}{" "}
        ground anchor{totalAnchors === 1 ? "" : "s"}
      </div>
    </div>
  );
}

/**
 * Snap an ortho-pane point onto the shared fence polygon so the same physical
 * corner traced from different cameras lands on the EXACT same ortho pixel.
 *
 * Prefers the nearest polygon VERTEX (within VERTEX_R); failing that, the
 * nearest point on any polygon EDGE (within EDGE_R); otherwise returns the
 * point unchanged. Thresholds scale with the ortho diagonal so they behave the
 * same regardless of orthophoto resolution. `poly` is closed (first == last);
 * the duplicated closing vertex is harmless for the vertex search.
 */
export function snapToFence(
  p: [number, number],
  poly: number[][],
  orthoW: number,
  orthoH: number,
): { pt: [number, number]; kind: "vertex" | "edge" | "none" } {
  if (poly.length < 2) return { pt: p, kind: "none" };

  const diag = Math.hypot(orthoW, orthoH);
  const VERTEX_R = 0.02 * diag;
  const EDGE_R = 0.012 * diag;

  // Nearest vertex.
  let bestV: [number, number] | null = null;
  let bestVd = Infinity;
  for (const v of poly) {
    const d = Math.hypot(p[0] - v[0], p[1] - v[1]);
    if (d < bestVd) {
      bestVd = d;
      bestV = [v[0], v[1]];
    }
  }
  if (bestV && bestVd <= VERTEX_R) return { pt: bestV, kind: "vertex" };

  // Nearest point on any edge (project onto each segment, clamp t to [0, 1]).
  let bestE: [number, number] | null = null;
  let bestEd = Infinity;
  for (let i = 0; i < poly.length - 1; i++) {
    const a = poly[i];
    const b = poly[i + 1];
    const dx = b[0] - a[0];
    const dy = b[1] - a[1];
    const len2 = dx * dx + dy * dy;
    let t = len2 > 0 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    const qx = a[0] + t * dx;
    const qy = a[1] + t * dy;
    const d = Math.hypot(p[0] - qx, p[1] - qy);
    if (d < bestEd) {
      bestEd = d;
      bestE = [qx, qy];
    }
  }
  if (bestE && bestEd <= EDGE_R) return { pt: bestE, kind: "edge" };

  return { pt: p, kind: "none" };
}
