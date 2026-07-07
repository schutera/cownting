import type {
  Site,
  CountRow,
  PostureRow,
  FrameRow,
  TimelineData,
  Areas,
  PostureBreakdown,
  AreaSummaryRow,
  DaySeries,
} from "./types";

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init);
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${url}`);
  return res.json() as Promise<T>;
}

export function getSite(): Promise<Site> {
  return j<Site>("/api/site");
}

export function getCounts(camera: string, trunc: string): Promise<CountRow[]> {
  return j<CountRow[]>(`/api/counts?camera=${camera}&trunc=${trunc}`);
}

export function getPosture(camera: string, trunc: string): Promise<PostureRow[]> {
  return j<PostureRow[]>(`/api/posture?camera=${camera}&trunc=${trunc}`);
}

export function getFrames(camera: string): Promise<FrameRow[]> {
  return j<FrameRow[]>(`/api/frames?camera=${camera}`);
}

export function getTimeline(): Promise<TimelineData> {
  return j<TimelineData>("/api/timeline");
}

export function getDaySeries(): Promise<DaySeries> {
  return j<DaySeries>("/api/day-series");
}

export function frameImg(camera: string, frameIdx: number, kind: "overlay" | "raw" = "overlay"): string {
  return `/api/img/frame/${camera}/${frameIdx}?kind=${kind}`;
}

export function orthoImg(): string {
  return "/api/img/orthophoto";
}

export function refImg(camera: string): string {
  return `/api/img/reference/${camera}`;
}

export function runLocalize(): Promise<{ updated: number }> {
  return j<{ updated: number }>("/api/localize", { method: "POST" });
}

export function getAreas(): Promise<Areas> {
  return j<Areas>("/api/areas");
}

export function saveAreas(areas: Areas): Promise<{ ok: boolean }> {
  return j<{ ok: boolean }>("/api/areas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ areas }),
  });
}

// Panel (shelter) areas — same polygon shape as count areas, separate storage.
export function getPanelAreas(): Promise<Areas> {
  return j<Areas>("/api/panel-areas");
}

export function savePanelAreas(areas: Areas): Promise<{ ok: boolean }> {
  return j<{ ok: boolean }>("/api/panel-areas", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ areas }),
  });
}

export interface AreaCounts {
  counts: Record<string, number>;
  postures: Record<string, PostureBreakdown>;
  sheltering: Record<string, number>; // per-region cows under a panel (unit-block indicator)
  frame: number | null;
}

export function getAreaCounts(frame?: number): Promise<AreaCounts> {
  const q = frame === undefined || frame === null ? "" : `?frame=${frame}`;
  return j<AreaCounts>(`/api/area-counts${q}`);
}

export function getAreaSummary(): Promise<AreaSummaryRow[]> {
  return j<AreaSummaryRow[]>("/api/area-summary");
}

export function getAreaCountsOverTime(
  camera?: string,
  trunc = "hour",
): Promise<{ series: { t: string; region_id: string; cows: number }[] }> {
  const q = camera ? `?camera=${camera}&trunc=${trunc}` : `?trunc=${trunc}`;
  return j<{ series: { t: string; region_id: string; cows: number }[] }>(
    `/api/area-counts/over-time${q}`,
  );
}

