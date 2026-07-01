import type { Site, CountRow, PostureRow, FrameRow, HeatmapData } from "./types";

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

export function getHeatmap(): Promise<HeatmapData> {
  return j<HeatmapData>("/api/heatmap");
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

export function saveCalibration(
  camera: string,
  camPoints: number[][],
  orthoPoints: number[][],
): Promise<{ camera: string; reproj_error: number; n_points: number }> {
  return j<{ camera: string; reproj_error: number; n_points: number }>("/api/calibration", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ camera, cam_points: camPoints, ortho_points: orthoPoints }),
  });
}

export function runLocalize(): Promise<{ updated: number }> {
  return j<{ updated: number }>("/api/localize", { method: "POST" });
}
