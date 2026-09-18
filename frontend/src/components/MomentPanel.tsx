import { useEffect, useState } from "react";
import type { Kpis } from "../lib/types";
import { getMoment } from "../lib/api";
import { useTimeline } from "../lib/timeline";
import { AttrStrips } from "./KpiPanel";
import { SectionLabel, Stat } from "./ui";

/**
 * The right rail's figures, for the moment on screen.
 *
 * Deliberately the SAME blocks in the SAME order as KpiPanel — cows in view,
 * the per-camera average, then the crop model's behaviour and light strips —
 * reading from the same `kpi_summary` on the server with one extra clause.
 * That is what makes the two panels comparable: "cows in view" has to count the
 * same thing in both or setting them side by side means nothing, and two
 * queries that merely look similar drift the first time one is edited.
 * `AttrStrips` is imported from KpiPanel rather than reimplemented for the same
 * reason.
 *
 * It shows no whole-day numbers of its own. The rail is four hundred pixels
 * away and already says them; repeating them here is the redundancy this panel
 * was built to replace.
 */
export default function MomentPanel() {
  const { frame, timeline } = useTimeline();
  const [kpis, setKpis] = useState<Kpis | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    getMoment(frame ?? undefined)
      .then((k) => alive && setKpis(k))
      .catch(() => alive && setKpis(null))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [frame]);

  // The wall clock for the scrubbed instant, so the scope is stated in the
  // reader's terms rather than as a bucket key.
  const when = (() => {
    if (frame == null || !timeline) return null;
    const i = timeline.frames.indexOf(frame);
    const iso = i >= 0 ? timeline.times[i] : null;
    return iso ? iso.slice(11, 16) : null;
  })();

  const cams = kpis?.valid_frames ?? 0;

  return (
    <div className="bg-surface border border-border px-4 py-3 mt-6 animate-fade-slide-in">
      <div className="flex items-baseline justify-between gap-3 flex-wrap">
        <SectionLabel>AT THIS MOMENT</SectionLabel>
        <span className="font-mono text-[11px] text-gray-tertiary">
          {when ? `${when} · ${cams} camera${cams === 1 ? "" : "s"} looking` : "whole day"}
        </span>
      </div>

      {kpis === null ? (
        <p className="text-gray-tertiary font-mono text-xs py-4">
          {loading ? "Loading…" : "No data for this moment."}
        </p>
      ) : (
        <div className="mt-3 grid grid-cols-1 sm:grid-cols-[minmax(0,180px)_minmax(0,1fr)] gap-x-8 gap-y-4 items-start">
          <div>
            <Stat value={kpis.detections.toLocaleString()} label="cows in view" size="hero" />
            {cams > 0 ? (
              <div className="mt-3">
                <Stat
                  value={kpis.cows_per_frame.toFixed(1)}
                  label="per camera"
                  accent="var(--color-accent-deep)"
                />
              </div>
            ) : null}
          </div>

          {/* Same component the rail draws, so a colour or an ordering can
              never mean two different things across the two panels. */}
          <div>
            {kpis.attrs && kpis.detections > 0 ? (
              <AttrStrips attrs={kpis.attrs} />
            ) : (
              <p className="text-[12px] text-gray-tertiary py-2">
                {kpis.detections === 0
                  ? "No cows in view at this moment."
                  : "The crop model has not scored these detections yet."}
              </p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
