import { useEffect, useState } from "react";
import { listUploadJobs } from "./api";
import type { UploadJob } from "./types";

/**
 * "Is this day's data ready, and if not, how far along is it?"
 *
 * Two independent signals answer that, and they are deliberately NOT the same
 * thing (see docs/roadmap/ROADMAP.md — job lifecycle vs data maturity must not be
 * conflated):
 *
 * - `datasets.status` — DATA MATURITY. Durable, in DuckDB, survives restarts:
 *   uploaded -> ingested -> segmented -> localized. This is the source of truth
 *   for "can I trust the numbers on screen yet".
 * - `UploadJob` — LIFECYCLE of one processing run. In-memory (mirrored to JSON),
 *   carries the live percentage and the failure reason. Absent for a day that
 *   finished long ago, or after a restart discarded it.
 *
 * The UI wants both: maturity decides whether to warn, the job supplies the
 * progress bar and the error text.
 */

/** The top rung: every detection has been assigned to its count area. */
export const READY = "localized";

export function isReady(status: string | null | undefined): boolean {
  return status === READY;
}

/** Short human phrasing for a `datasets.status` rung, for a badge. */
export function statusLabel(status: string): string {
  switch (status) {
    case "uploaded":
      return "not processed yet";
    case "ingested":
      return "reading frames";
    case "segmented":
      return "detecting cows";
    case READY:
      return "ready";
    default:
      return status; // forward-compatible: show an unknown rung verbatim
  }
}

/** One sentence explaining what the user can and cannot do at this rung. */
export function statusExplainer(status: string): string {
  switch (status) {
    case "uploaded":
      return "Your footage is saved on the server. Detection hasn't started on it yet.";
    case "ingested":
      return "Frames have been sampled from the video. Cow detection is still to come.";
    case "segmented":
      return "Cows have been detected; they're being assigned to count areas.";
    default:
      return "This day is still being processed.";
  }
}

export function isJobActive(job: UploadJob | undefined | null): boolean {
  return !!job && (job.status === "queued" || job.status === "running");
}

const POLL_ACTIVE_MS = 2000;
const POLL_IDLE_MS = 20000;

/**
 * Upload jobs keyed by dataset_id, kept fresh by polling.
 *
 * Polls briskly while any job is running and slowly otherwise — the slow tick is
 * what lets a tab that was already open notice an upload started somewhere else
 * (the job store is process-wide by design, not tied to the tab that started it).
 * Only the newest job per day is kept: `list_jobs()` returns active-first then
 * newest-first, so the first sighting of a dataset_id is the one that matters.
 */
export function useUploadJobs(): Record<string, UploadJob> {
  const [byDataset, setByDataset] = useState<Record<string, UploadJob>>({});

  useEffect(() => {
    let alive = true;
    let timer: number | undefined;

    const tick = async () => {
      let next = POLL_IDLE_MS;
      try {
        const jobs = await listUploadJobs();
        if (!alive) return;
        const map: Record<string, UploadJob> = {};
        for (const j of jobs) if (!(j.dataset_id in map)) map[j.dataset_id] = j;
        setByDataset(map);
        if (jobs.some(isJobActive)) next = POLL_ACTIVE_MS;
      } catch {
        /* transient — keep the last known state and try again */
      }
      if (alive) timer = window.setTimeout(tick, next);
    };

    tick();
    return () => {
      alive = false;
      if (timer) window.clearTimeout(timer);
    };
  }, []);

  return byDataset;
}
