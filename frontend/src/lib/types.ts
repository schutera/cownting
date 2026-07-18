export interface Kpis { frames:number; valid_frames:number; detections:number; standing:number; lying:number; sheltering:number; cows_per_frame:number; pct_lying:number; pct_sheltering:number; }
// Whole-day totals for one count area: split by posture + how many were under a panel.
export interface AreaSummaryRow { region_id:string; total:number; standing:number; lying:number; sheltering:number; }
export interface ImgMeta { url:string; width:number; height:number; }
// A count area: a named region whose camera_polygon (image px) does the counting,
// while ortho_polygon (ortho px) is only used to place it on the map for display.
// A PANEL area has the exact same shape — a cow whose ground point falls inside a
// panel-area camera_polygon counts as 'under a panel' (shelter). Both are edited
// on the camera page and stored per camera; only the semantic differs.
export type CountArea = { id: string; name: string; camera_polygon: number[][]; ortho_polygon: number[][] };
// Areas keyed by camera id — used for both count areas and panel (shelter) areas.
export type Areas = Record<string, CountArea[]>;

// A dashboard login account. `auth_disabled` is true only when the server was
// booted with auth turned off (tests / trusted-LAN demo) — the SPA then skips
// the login gate and treats the session as an admin.
export type Role = "admin" | "poweruser" | "user";
export interface User { username:string; role:Role; auth_disabled?:boolean; }

export interface Site { cameras:string[]; kpis:Kpis; orthophoto:ImgMeta|null; references:Record<string,ImgMeta>; posture_enabled:boolean; pose_enabled:boolean; dataset:string|null; }
// One data-package (a day's multi-camera shoot) for the day picker.
export interface DatasetRow { dataset_id:string; day:string|null; label:string|null; status:string; n_frames:number; n_detections:number; n_cameras:number; }
// An upload+auto-process job, polled while a newly-uploaded day is processing.
export interface UploadJob {
  job_id:string; dataset_id:string; label:string;
  status:"queued"|"running"|"done"|"failed";
  stage:"queued"|"ingesting"|"segmenting"|"localizing"|"done";
  progress:number; message:string; error:string|null;
  frames:number; detections:number;
}
export interface CountRow { t:string; frames:number; detections:number; cows_per_frame:number|null; }
export type PostureRow = { t:string } & Record<string, number|string>;
// Per-area posture composition (reused mask-elongation proxy; NULL -> unknown).
export interface PostureBreakdown { standing:number; lying:number; unknown:number; }
export interface FrameRow { frame_idx:number; ts:string; }
// `frames` is the shared *instant* axis (timestamp buckets; cameras linked by
// time, not frame_idx); `times` is each instant's wall-clock ISO for labelling.
export interface TimelineData { frames:number[]; times:string[]; counts:number[]; min_frame:number; max_frame:number; }
// Per-frame metric arrays (summed across cameras) for the time-of-day bar strips.
export interface DaySeries { frames:number[]; times:string[]; total:number[]; standing:number[]; lying:number[]; sheltering:number[]; open:number[]; }

// One cell of a cross-filter table: how many detections fall in a given
// primary-feature bucket, optionally split by a secondary breakdown feature.
export interface CrosstabCell { primary: string | number; breakdown: string | number | null; n: number; }
// A cross-filter result: counts of the `primary` feature over its domain,
// optionally split by `breakdown`. `primary_totals` gives the per-primary sum
// across all breakdown buckets; `total` is the grand total.
export interface Crosstab {
  primary: string; breakdown: string | null;
  primary_domain: (string|number)[]; breakdown_domain: (string|number)[];
  cells: CrosstabCell[]; primary_totals: Record<string, number>; total: number;
}
// A feature the backend can cross-filter on, plus whether it has data available.
export interface FeatureInfo { key: string; kind: string; available: boolean; }
