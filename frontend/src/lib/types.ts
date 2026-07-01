export interface Kpis { frames:number; blind_frames:number; valid_frames:number; detections:number; cows_per_frame:number; pct_lying:number; pct_localized:number; }
export interface ImgMeta { url:string; width:number; height:number; }
export interface CalibEntry { n_points:number; reproj_error:number; saved_at:string; cam_points:number[][]; ortho_points:number[][]; H:number[][]; orthophoto?:string|null; }
export interface Site { cameras:string[]; kpis:Kpis; orthophoto:ImgMeta|null; references:Record<string,ImgMeta>; calibration:Record<string,CalibEntry>; }
export interface CountRow { t:string; frames:number; detections:number; cows_per_frame:number|null; }
export type PostureRow = { t:string } & Record<string, number|string>;
export interface FrameRow { frame_idx:number; ts:string; }
export interface HeatmapData { points:[number,number][]; orthophoto:{width:number;height:number}|null; }
