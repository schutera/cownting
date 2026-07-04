/**
 * CoveragePanel — per-camera "map coverage" diagnostic for calibration.
 *
 * Each camera detects cows, but a detection only lands on the orthophoto heatmap
 * if this camera's calibration maps it *inside the valid region*; otherwise it is
 * dropped. "Coverage" is that survival rate: `localized / detections` per camera.
 *
 * Low coverage means the calibration is throwing away cows. The usual cause is
 * under-anchored calibration — too few ground anchors, or anchors all clustered in
 * one small patch — so the homography extrapolates/clips outside a tiny anchored
 * region and drops any detection that falls beyond it. The fix is to add ground
 * anchors spanning the full area where cows actually roam in that camera's view.
 *
 * This panel surfaces the worst offenders (sorted worst-first) so an
 * under-anchored camera is immediately obvious.
 */
import { Card, SectionLabel } from "./ui";

type Cov = { detections: number; localized: number };

export function CoveragePanel({
  coverage,
  cameras,
}: {
  coverage?: Record<string, Cov>;
  cameras: string[];
}) {
  if (!coverage || Object.keys(coverage).length === 0) return null;

  // Only cameras present in `coverage`; keep camera order stable via `cameras`
  // for tie-breaks, then sort by pct ascending so problems bubble to the top.
  const rows = cameras
    .filter((c) => c in coverage)
    .map((c) => {
      const { detections, localized } = coverage[c];
      const pct = detections > 0 ? Math.round((100 * localized) / detections) : 0;
      return { camera: c, detections, localized, pct };
    })
    .sort((a, b) => a.pct - b.pct);

  return (
    <Card className="mt-4 p-5">
      <SectionLabel>Map coverage</SectionLabel>
      <p className="text-[13px] text-text mt-1 max-w-3xl">
        Each camera's share of detections that actually land on the orthophoto
        heatmap; low coverage means the calibration is dropping cows — usually
        because its ground anchors are too few or too clustered, so it
        extrapolates/clips outside a small region. Fix by adding ground anchors
        spanning where cows roam in that camera's view.
      </p>

      <div className="mt-3 flex flex-col gap-1.5">
        {rows.map((r) => {
          const color =
            r.pct >= 80 ? undefined : r.pct >= 50 ? "#e58a3c" : "#e76f51";
          const bad = r.pct < 50;
          return (
            <div key={r.camera}>
              <div
                className={
                  "flex items-center justify-between font-mono text-[12px] tabular-nums" +
                  (r.pct >= 80 ? " text-accent" : "")
                }
                style={color ? { color } : undefined}
              >
                <span>{r.camera}</span>
                <span>
                  {r.localized} / {r.detections} &middot; {r.pct}%
                </span>
              </div>
              {bad ? (
                <div className="font-mono text-[10px]" style={{ color: "#e76f51" }}>
                  few detections reach the map — add ground anchors spanning the
                  cow area
                </div>
              ) : null}
            </div>
          );
        })}
      </div>
    </Card>
  );
}
