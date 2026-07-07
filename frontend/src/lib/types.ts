export interface Kpis { frames:number; valid_frames:number; detections:number; standing:number; lying:number; sheltering:number; cows_per_frame:number; pct_lying:number; pct_sheltering:number; }
// Whole-day totals for one count area, split by posture (static per-area KPI list).
export interface AreaSummaryRow { region_id:string; total:number; standing:number; lying:number; }
export interface ImgMeta { url:string; width:number; height:number; }
// A count area: a named region whose camera_polygon (image px) does the counting,
// while ortho_polygon (ortho px) is only used to place it on the map for display.
export type CountArea = { id: string; name: string; camera_polygon: number[][]; ortho_polygon: number[][] };
// All count areas, keyed by camera id.
export type Areas = Record<string, CountArea[]>;

// A solar-panel shelter primitive. `centerline` is an OPEN polyline of [x,y]
// vertices (px, ≥2 pts) tracing the panel's centre line. `width` (camera panels
// only) is the full shelter-band width in image px; the band is ±width/2 around
// the line, and a cow's ground-contact point inside it counts as sheltering.
export interface Panel { id: string; centerline: number[][]; width?: number }
// The site's panels: `ortho` = site-wide centre lines on the orthophoto (map
// overlay + naming), no width; `cameras[cam]` = that camera's view of each
// panel's centre line + band width, in camera image px (image-space shelter
// test). `id` links a camera panel to its ortho panel (same physical panel).
export interface PanelSet { ortho: Panel[]; cameras: Record<string, Panel[]> }

export interface Site { cameras:string[]; kpis:Kpis; orthophoto:ImgMeta|null; references:Record<string,ImgMeta>; posture_enabled:boolean; panels?:PanelSet|null; }
export interface CountRow { t:string; frames:number; detections:number; cows_per_frame:number|null; }
// One time-bucket of the shelter-over-time series: how many detections fell
// under a panel footprint vs. total, at bucket `t`.
export interface ShelterRow { t:string; sheltering:number; detections:number; }
export type PostureRow = { t:string } & Record<string, number|string>;
// Per-area posture composition (reused mask-elongation proxy; NULL -> unknown).
export interface PostureBreakdown { standing:number; lying:number; unknown:number; }
export interface FrameRow { frame_idx:number; ts:string; }
export interface TimelineData { frames:number[]; counts:number[]; min_frame:number; max_frame:number; }
