import { useEffect, useState } from "react";
import type { Site } from "../lib/types";
import { getSite } from "../lib/api";
import { useTimeline } from "../lib/timeline";
import { SectionLabel } from "../components/ui";
import { AreaMap } from "../components/AreaMap";
import DaySeries from "../components/DaySeries";
import KpiPanel from "../components/KpiPanel";
import CameraSegStack from "../components/CameraSegStack";
import CameraDetail from "../components/CameraDetail";

// Homepage layout: heatmap hero in the centre, aggregated KPIs on the right,
// per-camera segmentation on the left. Side panels stack under the hero on
// mobile (hero leads).

function Shimmer() {
  return (
    <div className="animate-shimmer grid grid-cols-1 lg:grid-cols-[264px_minmax(0,1fr)_320px] gap-6 items-start">
      <div className="h-96 bg-surface border border-border rounded-2xl lg:col-start-1 lg:row-start-1" />
      <div className="h-[28rem] bg-surface border border-border rounded-2xl lg:col-start-2 lg:row-start-1" />
      <div className="h-80 bg-surface border border-border rounded-2xl lg:col-start-3 lg:row-start-1" />
    </div>
  );
}

export default function Dashboard() {
  const [site, setSite] = useState<Site | null>(null);
  const [camera, setCamera] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  // Day-scrubber state is shared with the header scrubber via context.
  const { frame, setFrame, allDay } = useTimeline();
  // Camera enlarged in the hero's centre real estate; null = show the heatmap.
  const [focusCam, setFocusCam] = useState<string | null>(null);
  // Cameras de-selected from the heatmap (toggled via the seg-stack colour bars).
  const [hidden, setHidden] = useState<Set<string>>(new Set());
  const toggleHidden = (cam: string) =>
    setHidden((prev) => {
      const next = new Set(prev);
      if (next.has(cam)) next.delete(cam);
      else next.add(cam);
      return next;
    });

  useEffect(() => {
    let alive = true;
    getSite()
      .then((s) => {
        if (!alive) return;
        setSite(s);
        setCamera(s.cameras[0] ?? "");
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, []);

  if (error) {
    return (
      <p className="text-gray-tertiary font-mono text-sm">
        Couldn't load the dashboard — {error}
      </p>
    );
  }

  if (!site || !camera) {
    return <Shimmer />;
  }

  const heatmapFrame = allDay ? null : frame;

  return (
    <div className="flex flex-col gap-6">
      {/* Day scrubber lives in the sticky header (see App.tsx) so it can be
          dragged from anywhere; it drives `frame`/`allDay` via context. */}
      <div className="grid grid-cols-1 lg:grid-cols-[264px_minmax(0,1fr)_320px] gap-6 items-start">
        {/* Hero — occupancy heatmap, or a camera enlarged in its place (leads on mobile) */}
        <section className="lg:col-start-2 lg:row-start-1">
          <SectionLabel>{focusCam ? "CAMERA" : "OCCUPANCY"}</SectionLabel>
          <h2 className="font-display text-2xl sm:text-3xl font-light text-near-black leading-tight mt-1 mb-4">
            {focusCam
              ? `${focusCam} · segmentation`
              : heatmapFrame === null
                ? "Where the herd is (latest frame)"
                : "Where the herd is at this time"}
          </h2>
          {focusCam ? (
            <CameraDetail
              camera={focusCam}
              frameIdx={frame}
              meta={site.references[focusCam]}
              onClose={() => setFocusCam(null)}
            />
          ) : null}
          {/* Kept mounted (hidden) so returning to the map doesn't refetch/redraw. */}
          <div className={focusCam ? "hidden" : ""}>
            <AreaMap frame={heatmapFrame} cameras={site.cameras} hidden={hidden} />
          </div>

          {/* Time-of-day posture strips — same bar plot as the scrubber, sharing
              the clock axis; click any bar to scrub. */}
          <DaySeries frame={frame} allDay={allDay} onFrame={setFrame} />
        </section>

        {/* Right side panel — static, fully-aggregated KPIs only */}
        <div className="lg:col-start-3 lg:row-start-1">
          <KpiPanel kpis={site.kpis} postureEnabled={site.posture_enabled} />
        </div>

        {/* Left side panel — per-camera instance segmentation at the slider time */}
        <div className="lg:col-start-1 lg:row-start-1">
          <CameraSegStack
            cameras={site.cameras}
            active={camera}
            onSelect={setCamera}
            onExpand={(cam) => setFocusCam((f) => (f === cam ? null : cam))}
            focused={focusCam}
            frame={frame}
            hidden={hidden}
            onToggleHidden={toggleHidden}
          />
        </div>
      </div>
    </div>
  );
}
