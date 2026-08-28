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

// Background localize ("the box is working") progress. This is GLOBAL worker
// state — NOT scoped to the selected dataset — returned by GET
// /api/localize/status and embedded as `.localize` in the save-areas responses.
// `pending` lists dataset_ids still queued; `busy` is true while any pass is
// queued or running; `updated` is the detections reassigned in the last
// completed pass and `dataset` is which day that pass was for.
export interface LocalizeStatus {
  status: "idle" | "pending" | "running" | "done" | "failed";
  dataset: string | null;
  updated: number;
  error: string | null;
  at: number | null;
  pending: string[];
  busy: boolean;
}

// A dashboard login account. `auth_disabled` is true only when the server was
// booted with auth turned off (tests / trusted-LAN demo) — the SPA then skips
// the login gate and treats the session as an admin.
export type Role = "admin" | "poweruser" | "user";
// `role` is the EFFECTIVE role (an admin may act as a lower role via /api/act-as
// to preview the app); `real_role` is the account's actual role — they differ
// only while an admin is previewing.
export interface User { username:string; role:Role; real_role?:Role; auth_disabled?:boolean; }

export interface Site { cameras:string[]; kpis:Kpis; orthophoto:ImgMeta|null; references:Record<string,ImgMeta>; posture_enabled:boolean; pose_enabled:boolean; dataset:string|null; }
// One data-package (a day's multi-camera shoot) for the day picker.
export interface DatasetRow { dataset_id:string; day:string|null; label:string|null; status:string; n_frames:number; n_detections:number; n_cameras:number; }
// An upload+auto-process job, polled while a newly-uploaded day is processing.
export interface UploadJob {
  job_id:string; dataset_id:string; label:string;
  status:"queued"|"running"|"done"|"failed";
  stage:"queued"|"ingesting"|"segmenting"|"localizing"|"done";
  progress:number; message:string; error:string|null;
  frames:number; detections:number; created_at:number;
  // Advisory per-camera quality warnings, present on completed jobs (e.g.
  // ["camera_02 (obscured (dark), no cows detected)"]). Never blocks the job.
  warnings?:string[];
}
// The three per-camera issue codes quality.camera_health can flag on a camera.
export type CameraIssue = "dark" | "truncated" | "no_detections";
// Per-camera data-quality verdict for one day (quality.camera_health). ADVISORY:
// `ok` is true when `issues` is empty; a flagged camera is obscured ('dark'),
// stopped early ('truncated'), or produced nothing ('no_detections').
export interface CameraHealth {
  camera_id:string;
  n_frames:number;
  first_ts:string|null;
  last_ts:string|null;
  span_seconds:number;
  n_detections:number;
  brightness_p90:number|null;
  issues:CameraIssue[];
  ok:boolean;
  // Frames staged by a prior clip that Undo can restore (0 when not clipped).
  restorable?:number;
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

// One camera's frame coverage over the day: contiguous [startInstant, endInstant]
// runs on the shared instant axis (gaps split them), plus its count + first/last ts.
export interface CoverageCamera {
  camera_id: string;
  n_frames: number;
  first_ts: string;
  last_ts: string;
  segments: [number, number][];
}
// Per-camera coverage for a day. `uneven` is true when the shortest-covered camera
// spans under half the longest — the dashboard warns on it. Instants share the
// timeline/scrubber axis; min/max_ts give the wall-clock extent for labels.
export interface CameraCoverage {
  cameras: CoverageCamera[];
  min_instant: number;
  max_instant: number;
  min_ts: string | null;
  max_ts: string | null;
  bin_seconds: number;
  uneven: boolean;
}

// ------------------------------------------------------------ labeling (Label)
// The in-app annotation tool (docs/roadmap/M3_labeling.md). Nothing here is
// related to the Stage-1b CVAT round-trip that `label-select` / `label-export`
// drive — one word apart, two unrelated features. Naming, once: a GROUP is a
// question ("Sun exposure"), a CLASS is an answer inside it ("Shaded").

// The fixed icon vocabulary. Icons are stored as a NAME, never as markup: a
// poweruser adding a class at runtime must not be able to inject SVG, and a
// strict CSP forbids external assets anyway. This union is the CLIENT side of the
// contract — what the taxonomy editor may offer — while `LabelClass.icon` stays a
// bare string, because the server is free to hand back a name from a vocabulary
// this build has not heard of and <ClassIcon> must fall back to the neutral dot
// rather than have the type lie about what arrived.
export type LabelIconName =
  | "shade"
  | "sun"
  | "eye-off"
  | "question"
  | "grass"
  | "lying"
  | "standing"
  | "probe"
  | "pixels"
  | "dot";

// One answer option. `description` is required server-side and is what the (i)
// disclosure reveals: an option with no written definition is the single largest
// source of annotator disagreement.
// `icon` is a LabelIconName the option renders beside its text so the row is
// spottable at a glance instead of read word by word. It cannot be hardcoded per
// class_key anywhere in the frontend, because powerusers add classes at runtime;
// it therefore travels with the class. Empty or unrecognised -> the neutral dot.
// `active` is a soft archive — nothing in this feature is ever deleted, so an
// answer recorded against an archived class still resolves and still counts.
export interface LabelClass {
  class_key: string;     // '<group_key>.<slug>' — globally unique and immutable
  group_key: string;
  name: string;
  description: string;
  icon: string;          // a LabelIconName; unknown/'' renders the neutral dot
  sort_order: number;
  is_escape: boolean;    // the 'Cannot tell' hatch: a forced guess is noise
  active: boolean;
}
// One question. Position (not the literal `sort_order` value) decides the ORDER
// the groups are answered in — sun exposure first, behaviour second — and only
// the active group listens to the number keys, so reordering groups changes the
// sequence an annotator works through rather than which keys exist.
export interface LabelGroup {
  group_key: string;
  name: string;
  description: string | null;   // long form, shown behind the group's (i) icon
  sort_order: number;
  multi_select: boolean;
  required: boolean;
  active: boolean;
  classes: LabelClass[];
}
// The whole question stack plus the revision it was served at. Every submit
// echoes `revision` back; a skew is a 409 `taxonomy_stale` (not a 400), because a
// poweruser adding a required group mid-session would otherwise permanently 400
// every answer already on screen with no way forward but a reload.
export interface Taxonomy {
  revision: number;
  groups: LabelGroup[];
}

// FROZEN queue-item contract, mirrored field for field. `crop_url` is built
// SERVER-side and dropped straight into <img src>: a client-built URL would go
// through withDs() and 404 every item from a day other than the selected one.
// `ring` is crop-local px and is drawn as SVG over the image rather than baked
// into it, which is what makes hold-to-hide-the-ring cost no network. The caption
// shows day + camera and never the clock time — the burnt-in timestamp banner is
// masked server-side for the same reason, since time of day hands the annotator
// the sun/shade answer. `n_annotators` is how many already labeled this instance,
// never WHAT they said. `frame_url` is the whole banner-masked frame behind this
// crop, shown on hold-Space; there is still no frame_w/frame_h, because nothing
// measures it.
export interface LabelItem {
  instance_key: string;
  dataset_id: string | null;
  day: string | null;                       // ISO date; NOT the clock time
  camera_id: string;
  frame_file: string;
  bbox: [number, number, number, number];   // full-frame px, echoed on submit
  ordinal: number;
  score: number | null;
  frame_sig: string | null;
  crop_url: string;
  frame_url: string;
  crop_w: number;
  crop_h: number;
  ring: [number, number, number, number];   // crop-local px
  n_annotators: number;
  target: number;
  overlap: boolean;
  serve_event_id: number;
  // M4a outline fix-up (docs/roadmap/M4a_instance_mask_fixup.md §4.1). The
  // model's instance mask in CROP-LOCAL px — the same space as `ring`, for the
  // same reason: the client draws it with zero math. All three are optional
  // end-to-end because a pre-M4a server simply never sends them; the page then
  // seeds the editor from the ring rectangle (`mask_seed` "bbox").
  mask?: [number, number][] | null;
  mask_rev?: string | null;                 // hash of the stored polygon, echoed on submit
  mask_seed?: MaskSeed;
}

// Where the editable outline came from: the stored model mask, or (when none is
// stored yet) a rectangle at the ring the annotator sculpts into one.
export type MaskSeed = "mask" | "bbox";
// The two-way branch the outline tool offers (plan §3): correct the shape, or
// declare there is nothing to outline. Frozen in code like the flag reasons —
// each kind has export semantics and its own UI path.
export type MaskFixKind = "polygon" | "false_positive";
// POST /api/label/mask-fix. The anchor is echoed verbatim like every other
// label write; `polygon` is CROP-LOCAL px (required iff kind 'polygon' — the
// server converts to full-frame px for storage) and `mask_rev` is what makes a
// mid-session remask a 409 `mask_stale` instead of a silent re-attachment.
export interface LabelMaskFixReq {
  instance_key: string;
  anchor: InstanceAnchor;
  kind: MaskFixKind;
  polygon: [number, number][] | null;
  mask_rev: string | null;
  seeded_from: MaskSeed;
  serve_event_id?: number | null;
  session_id?: string | null;
  client_elapsed_ms?: number | null;
}

// Which instances the annotator wants: 'todo' hides the ones they personally have
// a current answer on, 'all' does not. Deliberately NOT a coverage filter — other
// annotators' answers never hide an instance until it reaches its target.
export type LabelQueueMine = "todo" | "all";
// 'fresh' drains the newest day first, so a new upload observably appears at the
// head of the queue; 'spread' drops the day term for a stratified sample.
export type LabelQueueOrder = "fresh" | "spread";
// The sampling knobs the server applied, echoed so the page can explain itself
// (why an instance is still being served, what redundancy is being targeted).
export interface LabelQueuePolicy {
  targets_per_instance: number;
  overlap_fraction: number;
  overlap_targets: number;
  skip_retire: number;
  batch_size: number;
  max_batch_size: number;
}
// The scope the server actually applied. This is the defence against api.ts's
// withDs() quietly stamping the selected day onto a queue that is cross-day by
// design: `dataset` null here means the whole DB was scanned, as intended.
export interface LabelQueueFilters {
  dataset: string | null;
  camera: string | null;
  day: string | null;
  mine: LabelQueueMine;
  order: LabelQueueOrder;
}
// `matching` is the exact number of instances the filters select — count(*) OVER ()
// from the same scan, free. There is no cursor and no offset on purpose: other
// annotators change an instance's coverage while you work, so any positional
// cursor would skip or repeat items. The queue is self-consuming instead —
// whatever you answer or flag is anti-joined away, so re-fetching always
// advances. Going BACK along the tape does not re-fetch: the page keeps the
// items it has already been served, which is what lets a revisited instance
// reappear with the answers already given.
export interface LabelQueue {
  items: LabelItem[];
  matching: number;
  policy: LabelQueuePolicy;
  filters: LabelQueueFilters;
}

// GET /api/label/progress. `pool_total === 0` is its own terminal state — no
// footage has been processed at all, which needs a link to /data, not a
// "you're caught up" celebration. `auth_disabled` is true when the server runs
// with auth off: every row is then written by annotator 'local', so agreement is
// undefined by construction and the panel says so instead of printing a number.
export interface LabelStats {
  pool_total: number;      // instances the queue could ever serve
  pool_labeled: number;    // ...with at least one labeled annotation
  pool_covered: number;    // ...at or over their coverage target
  remaining: number;       // ...still servable to ME
  my_labeled: number;
  my_skipped: number;
  my_median_ms: number | null;
  annotators: number;
  auth_disabled: boolean;
  filters: { dataset: string | null; camera: string | null };
}

// Why an instance could not be answered. There is no silent skip: an instance
// the annotator cannot judge is FLAGGED, and a flag carries one of these reasons
// AND a written explanation. `multiple_cows` in particular is a direct signal
// that the crop padding or the detector merged two animals. The name still says
// "skip" because this mirrors the backend's unchanged `labels_db.SKIP_REASONS`
// and the unchanged stored `skip_reason` column — only the word the annotator
// sees changed, and stored rows are deliberately not migrated.
export type LabelSkipReason =
  | "bad_crop"
  | "no_cow"
  | "multiple_cows"
  | "occluded"
  | "low_resolution"
  | "other";
// Storage terms, not user-facing ones. 'skipped' is the stored outcome of a FLAG
// — it means "not answered" and predates the rename, and stored rows are not
// migrated. 'undone' only ever appears on rows written before ArrowLeft replaced
// the undo action; nothing in the app produces it any more.
export type LabelOutcome = "labeled" | "skipped" | "undone";
// Recorded per annotation so a report can ask whether keyboard-first annotators
// disagree differently from mouse users.
export type LabelInputMode = "key" | "mouse";
// Effort telemetry, describing what is STORED rather than what the client may
// post. 'served' is written by the queue itself and is the non-forgeable
// time-on-task clock; 'submitted'/'skipped' are minted inside the write
// transactions; 'undo' survives only on historical rows. The client posts
// 'session_start', 'session_end', 'info_opened' and 'relabel' — the last when a
// revisited instance is re-answered after moving back along the tape.
export type LabelEventKind =
  | "session_start"
  | "served"
  | "submitted"
  | "skipped"
  | "undo"
  | "relabel"
  | "info_opened"
  | "session_end";

// The anchor the queue served, echoed back verbatim on submit. The server
// re-hashes it and 400s a mismatch, so the stored key and the stored anchor can
// never disagree — and the write path never has to open the main DB to check.
// `ordinal` cannot be recomputed by the client (it is a window function over the
// whole frame), which is exactly why the item carries it.
export interface InstanceAnchor {
  dataset_id: string | null;
  camera_id: string;
  frame_file: string;                       // basename, "00000450.jpg"
  bbox: [number, number, number, number];   // full-frame px
  ordinal: number;
  ts: string | null;
  frame_sig: string | null;
}
// `answers` is group_key -> class_key for a single-select group, or a list for a
// multi-select one. `taxonomy_revision` is the revision the annotator was SERVED,
// not the current one — sending the current one would defeat the 409.
export interface LabelSubmitReq {
  instance_key: string;
  anchor: InstanceAnchor;
  answers: Record<string, string | string[]>;
  taxonomy_revision: number;
  serve_event_id?: number | null;
  session_id?: string | null;
  client_elapsed_ms?: number | null;   // client ACTIVE time; the tab-away detector
  input_mode?: LabelInputMode | null;
  note?: string | null;
}
// Flagging an instance the annotator cannot answer. `explanation` is REQUIRED
// and must not be whitespace — that requirement is the entire point of replacing
// skip with flag: an escape hatch nobody has to justify gets pulled whenever the
// work gets hard, and the resulting rows are indistinguishable from genuinely
// unjudgeable crops. The dialog keeps Submit disabled until both fields are
// present and the server 400s a blank one, so neither side is load-bearing alone.
// A flag is still an annotation with the same provenance and uniqueness rule as
// an answer — not a 400 — and it is counted separately from coverage, so an
// instance one annotator could not judge is still served to the next.
export interface LabelFlagReq {
  instance_key: string;
  anchor: InstanceAnchor;
  reason: LabelSkipReason;
  explanation: string;                 // non-empty, non-whitespace; server-checked
  serve_event_id?: number | null;
  session_id?: string | null;
  client_elapsed_ms?: number | null;
}
export interface LabelEventReq {
  session_id: string;
  kind: LabelEventKind;
  instance_key?: string | null;
  class_key?: string | null;   // info_opened: WHICH description was read
}
// Submits append version n+1; they never overwrite, so a second annotator (or the
// same annotator changing their mind) is a new row, not a lost one. This is what
// makes moving back along the tape safe: correcting a revisited instance re-posts
// it and yields version 2, superseding the earlier row without losing it.
export interface LabelWriteResult {
  ok: boolean;
  annotation_id: number;
  version: number;
}

// GET /api/label/mine — the annotator's own recent submissions, newest first.
// `choices` is empty for a flag, which is why `outcome` and `skip_reason` are
// both on the row.
export interface LabelChoice {
  group_key: string;
  class_key: string;
  class_name: string | null;   // the display name AT LABEL TIME (survives a rename)
}
export interface LabelMineRow {
  annotation_id: number;
  instance_key: string;
  version: number;
  outcome: LabelOutcome;
  skip_reason: string | null;
  submitted_at: string;
  dataset_id: string | null;
  camera_id: string;
  frame_file: string;
  crop_url: string;
  choices: LabelChoice[];
}
// `next_before` is the `before` to pass for the following page, or null at the
// end. A timestamp cursor, not an offset — the list only ever grows at the head.
export interface LabelMinePage {
  items: LabelMineRow[];
  next_before: string | null;
}

// ---- taxonomy editing (poweruser)
// There are no DELETE routes anywhere in this feature: archiving is
// PATCH {"active": false} and restoring is {"active": true} — one field, one
// polarity, server and client in agreement. A hard delete would orphan every
// stored answer and silently change what the historical data means.
export interface LabelGroupReq {
  group_key: string;
  name: string;
  description?: string | null;
  multi_select?: boolean;
  required?: boolean;
}
export interface LabelGroupPatchReq {
  name?: string;
  description?: string | null;
  multi_select?: boolean;
  required?: boolean;
  active?: boolean;
}
// `icon` is typed to the vocabulary rather than to string on the WRITE side: the
// editor picks from a fixed list and free text is rejected, so a typo cannot
// reach the database and leave a class with no icon anyone can explain.
export interface LabelClassReq {
  class_key?: string;    // omitted -> '<group_key>.<slug of name>'
  name: string;
  description: string;   // required: the editor disables Add until it is written
  icon?: LabelIconName;  // omitted -> the server's default ('dot')
  is_escape?: boolean;
}
export interface LabelClassPatchReq {
  name?: string;
  description?: string;
  icon?: LabelIconName;
  is_escape?: boolean;
  active?: boolean;
}
// Reordering is up/down, not drag-and-drop: keyboard-reachable, screen-reader
// announceable, and it keeps the app dependency-free.
export type LabelMoveDir = "up" | "down";
