import { useEffect, useState } from "react";
import type { DatasetRow, Site, UploadJob } from "../lib/types";
import { getSite, getFrameMap } from "../lib/api";
import { useTimeline } from "../lib/timeline";
import { useDataset } from "../lib/dataset";
import { isReady, statusExplainer, statusLabel, useUploadJobs } from "../lib/processing";
import { SectionLabel } from "../components/ui";
import { AreaMap } from "../components/AreaMap";
import { DatasetPicker } from "../components/DatasetPicker";
import { CrossFilter } from "../components/CrossFilter";
import { CrossFilterProvider } from "../lib/crossfilter";
import KpiPanel from "../components/KpiPanel";
import CameraSegStack from "../components/CameraSegStack";
import CameraDetail from "../components/CameraDetail";

// Homepage layout: heatmap hero in the centre, aggregated KPIs on the right,
// per-camera segmentation on the left. Side panels stack under the hero on
// mobile (hero leads).

/** Full-page state for a day with nothing to show yet: uploaded, still queued or
 *  mid-pipeline. Carries the same wording as the slim strip so the two read as
 *  one idea at two sizes. */
function ProcessingNotice({ row, job }: { row: DatasetRow; job?: UploadJob }) {
  const failed = job?.status === "failed";
  const pct = job ? Math.round(job.progress * 100) : 0;
  return (
    <div className="bg-surface border border-border rounded-2xl p-8 flex flex-col items-center text-center animate-fade-slide-in">
      <span
        className={
          "grid place-items-center w-14 h-14 rounded-full text-xl " +
          (failed ? "bg-[#e76f51]/10 text-[#e76f51]" : "bg-accent-soft text-accent-deep")
        }
      >
        {failed ? "!" : "↑"}
      </span>
      <h2 className="font-display text-2xl font-light text-near-black mt-4">
        {failed ? "Processing failed" : "Uploaded — not processed yet"}
      </h2>
      <p className="text-gray-mid text-sm mt-2 max-w-md">
        {failed
          ? job?.error || job?.message
          : statusExplainer(row.status) +
            " Counts, the heatmap and the camera views appear here as it finishes."}
      </p>
      {job && !failed ? (
        <div className="w-full max-w-sm mt-6">
          <div className="h-1.5 w-full rounded-full bg-surface-sunk overflow-hidden">
            <div
              className="h-full bg-accent rounded-full transition-[width] duration-500 ease-out"
              style={{ width: `${Math.max(4, pct)}%` }}
            />
          </div>
          <p className="text-[12px] text-gray-tertiary mt-2 tabular-nums">{job.message}</p>
        </div>
      ) : null}
    </div>
  );
}

/** Slim companion to ProcessingNotice for a day that already has SOME data on
 *  screen — a caveat over the charts, not a takeover. */
function ProcessingStrip({ row, job }: { row: DatasetRow; job?: UploadJob }) {
  const failed = job?.status === "failed";
  const pct = job ? Math.round(job.progress * 100) : 0;
  return (
    <div
      className={
        "flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border px-3.5 py-2.5 " +
        (failed
          ? "border-[#e76f51]/30 bg-[#e76f51]/5"
          : "border-warn/30 bg-warn/5")
      }
    >
      <span className={"w-2 h-2 rounded-full shrink-0 " + (failed ? "bg-[#e76f51]" : "bg-warn")} />
      <span className="text-[13px] text-near-black">
        {failed ? "Processing failed" : `Still processing — ${statusLabel(row.status)}`}
      </span>
      <span className="text-[12px] text-gray-mid">
        {failed
          ? job?.error || job?.message
          : "These numbers are incomplete and will keep climbing."}
      </span>
      {job && !failed ? (
        <span className="ml-auto font-mono text-[11px] text-gray-tertiary tabular-nums shrink-0">
          {pct}%
        </span>
      ) : null}
    </div>
  );
}

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
  // Processing state for the day on screen. `row.status` (durable) says whether
  // the numbers can be trusted yet; `job` (live) supplies the percentage.
  const { datasets, dataset, refresh, loaded: daysLoaded } = useDataset();
  const jobs = useUploadJobs();
  const row = datasets.find((d) => d.dataset_id === dataset) ?? null;
  const job = dataset ? jobs[dataset] : undefined;
  const processing = row ? !isReady(row.status) : false;
  // Changes each time the pipeline moves — the cue to re-read the day, so a
  // dashboard opened mid-processing fills itself in rather than needing a reload.
  const stageKey = job ? `${job.status}:${job.stage}` : "";
  // Day-scrubber state is shared with the header scrubber via context.
  const { frame } = useTimeline();
  // `frame` is an instant (timestamp bucket); resolve each camera's own frame_idx
  // for it so the expanded CameraDetail shows that camera's actual frame.
  const [frameMap, setFrameMap] = useState<Record<string, number>>({});
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
        // Only seed the selection — a re-read mid-processing must not yank the
        // user off the camera they picked.
        setCamera((c) => c || (s.cameras[0] ?? ""));
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
    // Re-runs on every pipeline stage change (see stageKey); effectively [] once
    // the day has settled and no job is reporting.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stageKey]);

  // Keep datasets.status fresh alongside it, so the notice clears by itself the
  // moment the day finishes localizing.
  useEffect(() => {
    if (!stageKey) return;
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [stageKey]);

  useEffect(() => {
    if (frame == null) {
      setFrameMap({});
      return;
    }
    let alive = true;
    getFrameMap(frame)
      .then((m) => alive && setFrameMap(m))
      .catch(() => alive && setFrameMap({}));
    return () => {
      alive = false;
    };
  }, [frame]);

  if (error) {
    return (
      <p className="text-gray-tertiary font-mono text-sm">
        Couldn't load the dashboard — {error}
      </p>
    );
  }

  if (!site) {
    return <Shimmer />;
  }

  // No cameras on this day. Until now this fell through to a skeleton that never
  // resolved — the day HAS no frames yet, so nothing was ever coming. Say which
  // of the two reasons it is instead.
  if (!camera) {
    // Wait for the day list before deciding WHICH empty state — without it a
    // still-processing day flashes "no footage" for a beat on every load.
    if (!daysLoaded) return <Shimmer />;
    return (
      <div className="flex flex-col gap-6">
        <DatasetPicker />
        {processing && row ? (
          <ProcessingNotice row={row} job={job} />
        ) : (
          <p className="text-gray-tertiary font-mono text-sm">
            This day has no camera footage.
          </p>
        )}
      </div>
    );
  }

  return (
    <CrossFilterProvider>
      <div className="flex flex-col gap-6">
        {/* Day / data-package selector — dashboard-specific, so it rides here
            rather than in the global header. */}
        <DatasetPicker />
        {/* Partly-processed day: the charts below are real but incomplete, so
            say so rather than letting a half-filled dashboard read as the truth. */}
        {processing && row ? <ProcessingStrip row={row} job={job} /> : null}
        {/* Day scrubber lives in the sticky header (see App.tsx) so it can be
            dragged from anywhere; it drives `frame` via context. The cross-filter
            selection is shared here so the centre strips and the right-rail KPI
            mirror read one pivot. */}
        <div className="grid grid-cols-1 lg:grid-cols-[264px_minmax(0,1fr)_320px] gap-6 items-start">
        {/* Hero — occupancy heatmap, or a camera enlarged in its place (leads on mobile) */}
        <section className="lg:col-start-2 lg:row-start-1">
          <SectionLabel>{focusCam ? "CAMERA" : "OCCUPANCY"}</SectionLabel>
          <h2 className="font-display text-2xl sm:text-3xl font-light text-near-black leading-tight mt-1 mb-4">
            {focusCam
              ? `${focusCam} · segmentation`
              : frame === null
                ? "Where the herd is (latest frame)"
                : "Where the herd is at this time"}
          </h2>
          {focusCam ? (
            <CameraDetail
              camera={focusCam}
              frameIdx={frame != null ? (frameMap[focusCam] ?? null) : null}
              meta={site.references[focusCam]}
              poseEnabled={site.pose_enabled}
              onClose={() => setFocusCam(null)}
            />
          ) : null}
          {/* Kept mounted (hidden) so returning to the map doesn't refetch/redraw. */}
          <div className={focusCam ? "hidden" : ""}>
            <AreaMap frame={frame} cameras={site.cameras} hidden={hidden} />
          </div>

          {/* Interactive cross-filter: conditional ratios of any stored feature
              (posture, under-panels, shade, area, camera) grouped by another or
              by time of day — subsumes the old static time-of-day strips. */}
          <CrossFilter cameras={site.cameras} />
        </section>

        {/* Right side panel — whole-day aggregates + a live mirror of the
            cross-filter pivot (see KpiPanel) */}
        <div className="lg:col-start-3 lg:row-start-1">
          <KpiPanel kpis={site.kpis} cameras={site.cameras} />
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
            frameMap={frameMap}
            hidden={hidden}
            onToggleHidden={toggleHidden}
          />
        </div>
        </div>
      </div>
    </CrossFilterProvider>
  );
}
