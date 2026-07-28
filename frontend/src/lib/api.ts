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
  DatasetRow,
  UploadJob,
  Crosstab,
  FeatureInfo,
  LocalizeStatus,
  CameraHealth,
  CameraCoverage,
  User,
  Role,
} from "./types";

// The selected data-package (day). Held module-level so every /api call carries
// it without threading a param through each function; the DatasetProvider sets it
// (and remounts the tree) when the user picks a different day. null -> the backend
// resolves the latest package.
let currentDataset: string | null = null;
export function setDatasetParam(id: string | null): void {
  currentDataset = id;
}
function withDs(url: string): string {
  if (currentDataset == null || !url.startsWith("/api/")) return url;
  const sep = url.includes("?") ? "&" : "?";
  return `${url}${sep}dataset=${encodeURIComponent(currentDataset)}`;
}

// A single place to learn a session went away: any /api call that comes back 401
// (session expired, server restarted, logged out in another tab) fires this, and
// the AuthProvider registers a handler that drops back to the login screen.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(fn: (() => void) | null): void {
  onUnauthorized = fn;
}

async function j<T>(url: string, init?: RequestInit): Promise<T> {
  // credentials:"include" so the session cookie rides along even when the dev
  // Vite server and the API sit on different origins.
  const res = await fetch(withDs(url), { credentials: "include", ...init });
  if (res.status === 401) {
    onUnauthorized?.();
    throw new Error("session expired — please sign in again");
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText} — ${url}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<T>;
}

// ------------------------------------------------------------------------ auth
// Current user, or throws (401 -> handled by the gate). `getMe` is the SPA's
// "am I logged in?" probe on load.
export function getMe(): Promise<User> {
  return j<User>("/api/me");
}

export async function login(username: string, password: string): Promise<User> {
  const res = await fetch("/api/login", {
    method: "POST",
    credentials: "include",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password }),
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json() as Promise<User>;
}

export async function logout(): Promise<void> {
  await fetch("/api/logout", { method: "POST", credentials: "include" });
}

// --------------------------------------------------------------- admin: users
export function listUsers(): Promise<User[]> {
  return j<User[]>("/api/admin/users");
}

export function createUser(username: string, password: string, role: Role): Promise<{ ok: boolean; users: User[] }> {
  return j<{ ok: boolean; users: User[] }>("/api/admin/users", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password, role }),
  });
}

// Change a user's password and/or role — send only the field(s) that change.
export function updateUser(
  username: string,
  patch: { password?: string; role?: Role },
): Promise<{ ok: boolean; users: User[] }> {
  return j<{ ok: boolean; users: User[] }>(`/api/admin/users/${encodeURIComponent(username)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export function deleteUser(username: string): Promise<{ ok: boolean; users: User[] }> {
  return j<{ ok: boolean; users: User[] }>(`/api/admin/users/${encodeURIComponent(username)}`, {
    method: "DELETE",
  });
}

export function getDatasets(): Promise<DatasetRow[]> {
  return j<DatasetRow[]>("/api/datasets");
}

// Thrown when the backend can't read the capture day from the video metadata and
// needs the user to specify one (re-submit with a `day` in the form). The panel
// catches this to reveal a date picker.
export class CaptureDayRequiredError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "CaptureDayRequiredError";
  }
}

// Upload one video per camera as a new day. `form` carries repeated `videos`
// files + matching `cameras` names (same order), plus an optional `label`. The
// capture day is not sent — the backend reads it from each video file's own
// metadata (the container creation_time). Only if that can't be read does the
// user set a `day`, re-submitted here as an explicit override.
// Returns the queued job (202) to poll with getUploadJob. Not routed through j()
// because it's dataset-independent and must not append the ?dataset param.
export async function uploadVideos(form: FormData): Promise<UploadJob> {
  const res = await fetch("/api/uploads", { method: "POST", body: form });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let code: string | undefined;
    try {
      const body = await res.json();
      if (body?.detail && typeof body.detail === "object") {
        detail = body.detail.message || detail;
        code = body.detail.code;
      } else if (body?.detail) {
        detail = body.detail;
      }
    } catch {
      /* non-JSON error body */
    }
    if (code === "capture_day_required") throw new CaptureDayRequiredError(detail);
    throw new Error(detail);
  }
  return res.json() as Promise<UploadJob>;
}

export function getUploadJob(jobId: string): Promise<UploadJob> {
  return j<UploadJob>(`/api/uploads/${jobId}`);
}

// Every known upload job, newest first (active first). Used on mount to reconnect
// the progress bar to an upload still processing on the server — the job store is
// process-wide, so it works after a refresh, in another tab, or for another user.
// (The endpoint ignores the ?dataset param j() appends.)
export function listUploadJobs(): Promise<UploadJob[]> {
  return j<UploadJob[]>("/api/uploads");
}

// Delete a day from the dashboard. The backend does NOT destroy it — it moves the
// day's rows into an archive DB, so it vanishes from every view but is preserved.
// `confirm` must equal the capture day as ddmmyy (typed by the user); the server
// re-checks it. Not routed through j() — dataset-independent, no ?dataset param.
export async function deleteDataset(id: string, confirm: string): Promise<void> {
  const res = await fetch(
    `/api/datasets/${encodeURIComponent(id)}?confirm=${encodeURIComponent(confirm)}`,
    { method: "DELETE" },
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
}

// ---------------------------------------------------- per-camera management
// Per-camera data-quality verdict for one day (brightness, span, detections, and
// any 'dark'/'truncated'/'no_detections' issues). The id rides in the path; the
// ?dataset that j() appends is harmless — the endpoint reads the path id.
export function getCameraHealth(dataset: string): Promise<CameraHealth[]> {
  return j<CameraHealth[]>(`/api/dataset/${encodeURIComponent(dataset)}/camera-health`);
}

// Permanently drop ONE camera's stream from a day (frames, detections, images,
// areas) — every other camera is untouched — and re-localize what remains. Not
// routed through j() because it's path-scoped; both segments are encoded.
export async function deleteCameraStream(
  dataset: string,
  camera: string,
): Promise<{
  ok: boolean;
  dataset_id: string;
  camera: string;
  frames_removed: number;
  detections_removed: number;
  localize: LocalizeStatus;
}> {
  const res = await fetch(
    `/api/dataset/${encodeURIComponent(dataset)}/camera/${encodeURIComponent(camera)}`,
    { method: "DELETE", credentials: "include" },
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

// Add (or replace) ONE camera stream in an existing day, then auto-process just
// that stream (ingest -> segment -> re-localize) server-side. `form` carries a
// `video` file + `camera` name (no `start` — the backend reads the recording time
// from the video). Returns the queued job (202) to poll with getUploadJob. Like
// uploadVideos, a plain fetch (no j()) so it isn't scoped by the ?dataset param.
export async function addCameraStream(dataset: string, form: FormData): Promise<UploadJob> {
  const res = await fetch(`/api/dataset/${encodeURIComponent(dataset)}/camera`, {
    method: "POST",
    credentials: "include",
    body: form,
  });
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    let code: string | undefined;
    try {
      const body = await res.json();
      if (body?.detail && typeof body.detail === "object") {
        detail = body.detail.message || detail;
        code = body.detail.code;
      } else if (body?.detail) {
        detail = body.detail;
      }
    } catch {
      /* non-JSON error body */
    }
    if (code === "capture_day_required") throw new CaptureDayRequiredError(detail);
    throw new Error(detail);
  }
  return res.json() as Promise<UploadJob>;
}

// Trim ONE camera's stream to a time window [start, end] (ISO timestamps),
// permanently dropping frames + detections outside it, so an over-long camera
// lines up with the others. Poweruser-gated; re-localizes the day server-side.
export async function clipCameraStream(
  dataset: string,
  camera: string,
  start: string,
  end: string,
): Promise<{ ok: boolean; dataset_id: string; camera: string; frames_removed: number; frames_kept: number }> {
  const res = await fetch(
    `/api/dataset/${encodeURIComponent(dataset)}/camera/${encodeURIComponent(camera)}/clip`,
    {
      method: "POST",
      credentials: "include",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start, end }),
    },
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
}

// Undo a clip: restore ONE camera's staged (clipped-out) frames + detections back
// to its full pre-clip extent. Poweruser-gated; re-localizes the day server-side.
export async function restoreCameraStream(
  dataset: string,
  camera: string,
): Promise<{ ok: boolean; dataset_id: string; camera: string; frames_restored: number }> {
  const res = await fetch(
    `/api/dataset/${encodeURIComponent(dataset)}/camera/${encodeURIComponent(camera)}/restore`,
    { method: "POST", credentials: "include" },
  );
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      if (body?.detail) detail = body.detail;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(detail);
  }
  return res.json();
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

// Per-camera frame coverage over the day (which camera records in which ranges) +
// an `uneven` flag, for the dashboard coverage strip. Scoped to the selected day.
export function getCameraCoverage(): Promise<CameraCoverage> {
  return j<CameraCoverage>("/api/camera-coverage");
}

export function getDaySeries(): Promise<DaySeries> {
  return j<DaySeries>("/api/day-series");
}

export function frameImg(camera: string, frameIdx: number, kind: "overlay" | "raw" | "pose" = "overlay"): string {
  return withDs(`/api/img/frame/${camera}/${frameIdx}?kind=${kind}`);
}

// The per-camera frame_idx to show at one instant (timestamp bucket). Cameras
// with no footage in that bucket are omitted (not yet online / already offline).
export function getFrameMap(instant: number): Promise<Record<string, number>> {
  return j<Record<string, number>>(`/api/frame-map?frame=${instant}`);
}

export function orthoImg(): string {
  return "/api/img/orthophoto"; // dataset-independent (one site backdrop)
}

export function refImg(camera: string): string {
  return withDs(`/api/img/reference/${camera}`);
}

export function runLocalize(): Promise<{ updated: number }> {
  return j<{ updated: number }>("/api/localize", { method: "POST" });
}

// Background-localize progress for the "the box is working" spinner. This is
// GLOBAL worker status — not scoped to a dataset. The endpoint ignores the
// ?dataset param j() appends (same as listUploadJobs), so a plain j() GET is fine.
export function getLocalizeStatus(): Promise<LocalizeStatus> {
  return j<LocalizeStatus>("/api/localize/status");
}

// CSV export (one row per detection). Returns a URL for an <a href download> —
// the browser streams the file directly from the API. No arg = whole database;
// pass a dataset id to scope the export to a single day.
export function exportCsvUrl(dataset?: string): string {
  return dataset
    ? `/api/export.csv?dataset=${encodeURIComponent(dataset)}`
    : "/api/export.csv";
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

// Areas for an EXPLICIT dataset (a different day than the selected one) — used to
// import a camera's shapes from another day so they don't have to be redrawn. A
// plain fetch, NOT j(), so the current-day ?dataset param isn't also appended.
export async function getAreasFor(dataset: string): Promise<Areas> {
  const res = await fetch(`/api/areas?dataset=${encodeURIComponent(dataset)}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<Areas>;
}

export async function getPanelAreasFor(dataset: string): Promise<Areas> {
  const res = await fetch(`/api/panel-areas?dataset=${encodeURIComponent(dataset)}`, {
    credentials: "include",
  });
  if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
  return res.json() as Promise<Areas>;
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

// Cross-filter table: counts of `primary` (over its domain), optionally split by
// `breakdown`, and optionally scoped to one camera and/or a single frame.
export function getCrosstab(
  primary: string,
  breakdown?: string,
  opts?: { camera?: string; frame?: number },
): Promise<Crosstab> {
  const q = new URLSearchParams({ primary });
  if (breakdown) q.set("breakdown", breakdown);
  if (opts?.camera) q.set("camera", opts.camera);
  if (opts?.frame !== undefined) q.set("frame", String(opts.frame));
  return j<Crosstab>(`/api/crosstab?${q}`);
}

// The features the backend can cross-filter on, plus per-feature availability.
export function getFeatures(): Promise<FeatureInfo[]> {
  return j<FeatureInfo[]>("/api/features");
}

