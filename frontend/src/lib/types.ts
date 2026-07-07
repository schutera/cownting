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

export interface Site { cameras:string[]; kpis:Kpis; orthophoto:ImgMeta|null; references:Record<string,ImgMeta>; posture_enabled:boolean; }
export interface CountRow { t:string; frames:number; detections:number; cows_per_frame:number|null; }
export type PostureRow = { t:string } & Record<string, number|string>;
// Per-area posture composition (reused mask-elongation proxy; NULL -> unknown).
export interface PostureBreakdown { standing:number; lying:number; unknown:number; }
export interface FrameRow { frame_idx:number; ts:string; }
export interface TimelineData { frames:number[]; counts:number[]; min_frame:number; max_frame:number; }
// Per-frame metric arrays (summed across cameras) for the time-of-day bar strips.
export interface DaySeries { frames:number[]; total:number[]; standing:number[]; lying:number[]; sheltering:number[]; open:number[]; }
