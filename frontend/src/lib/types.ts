export interface Kpis { frames:number; valid_frames:number; detections:number; cows_per_frame:number; pct_lying:number; pct_localized:number; pct_sheltering:number; }
export interface ImgMeta { url:string; width:number; height:number; }
// A camera↔ortho correspondence: [[cam x,y], [ortho x,y]] in natural/full-res px.
export type PointPair = [[number, number], [number, number]];
// A fisheye "should-be-straight" line: a list of [x,y] vertices in camera px.
export type Line = [number, number][];
// A fence-segment correspondence: [camera polyline, ortho polyline] tracing the
// same ground-level fence span; vertex i on one side matches vertex i on the other.
export type FenceLink = [Line, Line];
// A ground-line correspondence: [camera polyline, ortho polyline] tracing the SAME
// straight ground feature — but the endpoints/length/position do NOT correspond.
// Used as a length-agnostic point-on-line constraint (perpendicular distance only).
export type GroundLine = [Line, Line];
// One camera's sighting of a shared ground feature, in that camera's natural px.
export interface TieObs { camera: string; pt: [number, number]; }
// A cross-camera tie point: the SAME physical ground point seen by ≥2 cameras.
// A free bundle-adjustment landmark that couples those cameras' calibrations.
export type TiePoint = TieObs[];

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

export interface PerPointError { center?:number[]; ground?:number[]; fence?:number[]; ground_lines?:number[]; }

export interface CalibEntry {
  // --- new 3-stage (center_pillar / ground_poly) fields (all optional) ---
  method?:string;
  model?:unknown;
  reproj_error?:number;
  max_residual?:number;
  line_residual?:number;
  per_point_error?:PerPointError;
  h_center?:number|null;
  lines?:Line[];
  center_pairs?:PointPair[];
  ground_pairs?:PointPair[];
  n_center?:number;
  n_ground?:number;
  n_lines?:number;
  // --- legacy single-homography fields (still emitted by older entries) ---
  n_points?:number;
  cam_points?:number[][];
  ortho_points?:number[][];
  H?:number[][];
  orthophoto?:string|null;
  saved_at?:string;
}
export interface Site { cameras:string[]; kpis:Kpis; orthophoto:ImgMeta|null; references:Record<string,ImgMeta>; calibration:Record<string,CalibEntry>; posture_enabled:boolean; fence?:number[][]|null; tiepoints?:TiePoint[]; panels?:PanelSet|null; coverage?:Record<string,{ detections:number; localized:number }>; }
export interface CountRow { t:string; frames:number; detections:number; cows_per_frame:number|null; }
// One time-bucket of the shelter-over-time series: how many detections fell
// under a panel footprint vs. total, at bucket `t`.
export interface ShelterRow { t:string; sheltering:number; detections:number; }
export type PostureRow = { t:string } & Record<string, number|string>;
export interface FrameRow { frame_idx:number; ts:string; }
export interface HeatmapData { points:[number,number][]; cams?:string[]; frames?:number[]; orthophoto:{width:number;height:number}|null; frame?:number|null; window?:number; }
export interface TimelineData { frames:number[]; counts:number[]; min_frame:number; max_frame:number; }
