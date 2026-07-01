import { useEffect, useState } from "react";
import type { Site } from "../lib/types";
import { getSite } from "../lib/api";
import { SectionLabel } from "../components/ui";
import { Heatmap } from "../components/Heatmap";
import KpiPanel from "../components/KpiPanel";
import CameraSegStack from "../components/CameraSegStack";

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

  return (
    <div className="grid grid-cols-1 lg:grid-cols-[264px_minmax(0,1fr)_320px] gap-6 items-start">
      {/* Hero — occupancy heatmap (leads on mobile) */}
      <section className="lg:col-start-2 lg:row-start-1">
        <SectionLabel>OCCUPANCY</SectionLabel>
        <h2 className="font-display text-2xl sm:text-3xl font-light text-near-black leading-tight mt-1 mb-4">
          Where the herd spends its day
        </h2>
        <Heatmap />
      </section>

      {/* Right side panel — aggregated KPIs */}
      <div className="lg:col-start-3 lg:row-start-1">
        <KpiPanel kpis={site.kpis} camera={camera} trunc="hour" />
      </div>

      {/* Left side panel — per-camera instance segmentation */}
      <div className="lg:col-start-1 lg:row-start-1">
        <CameraSegStack cameras={site.cameras} active={camera} onSelect={setCamera} />
      </div>
    </div>
  );
}
