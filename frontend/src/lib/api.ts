import type {
  Site,
  CountRow,
  PostureRow,
  FrameRow,
  HeatmapData,
  TimelineData,
  Line,
  FenceLink,
  GroundLine,
  TiePoint,
  PanelSet,
  ShelterRow,
  PerPointError,
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

export function getHeatmap(frame?: number | null, window = 30): Promise<HeatmapData> {
  const q =
    frame === undefined || frame === null
      ? ""
      : `?frame=${frame}&window=${window}`;
  return j<HeatmapData>(`/api/heatmap${q}`);
}

export function getTimeline(): Promise<TimelineData> {
  return j<TimelineData>("/api/timeline");
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

export interface CalibrationPayload {
  camera: string;
  method: "center_pillar";
  image_size: [number, number]; // camera reference image natural [w, h]
  lines: Line[];
  ground_lines: GroundLine[];
  // Panel footprint correspondences: each entry is [cam_ring, ortho_ring] tracing
  // one panel's height-0 ground footprint (matched vertex order) — ground anchors.
  panel_lines: FenceLink[];
}

export interface CalibrationResult {
  camera: string;
  method: string;
  reproj_error: number;
  max_residual: number;
  line_residual: number;
  per_point_error: PerPointError;
  n_ground_lines: number;
  n_panel: number;
  n_lines: number;
}

export function saveCalibration(payload: CalibrationPayload): Promise<CalibrationResult> {
  return j<CalibrationResult>("/api/calibration", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function runLocalize(): Promise<{ updated: number }> {
  return j<{ updated: number }>("/api/localize", { method: "POST" });
}

export interface JointCameraDiag {
  reproj_error: number; max_residual: number; line_residual: number; n_shared: number; method: string;
}
export interface JointResult {
  cameras: string[];
  global: { cross_camera_px: number; max_cross_camera_px: number; n_shared_corners: number; n_pairs: number };
  per_camera: Record<string, JointCameraDiag>;
  updated: number;
}
export function runJointCalibration(): Promise<JointResult> {
  return j<JointResult>("/api/calibration/joint", { method: "POST" });
}

export function saveFence(
  polygon: number[][],
): Promise<{ n_vertices: number; updated: number }> {
  return j<{ n_vertices: number; updated: number }>("/api/fence", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ polygon }),
  });
}

export function saveTiePoints(tiepoints: TiePoint[]): Promise<{ n: number }> {
  return j<{ n: number }>("/api/tiepoints", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ tiepoints }),
  });
}

export function savePanels(
  set: PanelSet,
): Promise<{ n_ortho: number; n_cameras: number; updated: number }> {
  return j<{ n_ortho: number; n_cameras: number; updated: number }>("/api/panels", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ ortho: set.ortho, cameras: set.cameras }),
  });
}

export function getShelter(camera: string, trunc: string): Promise<ShelterRow[]> {
  return j<ShelterRow[]>(`/api/shelter?camera=${camera}&trunc=${trunc}`);
}
