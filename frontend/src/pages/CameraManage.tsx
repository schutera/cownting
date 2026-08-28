import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useAuth, canManageData } from "../lib/auth";
import type { CameraHealth, CameraIssue, DatasetRow, UploadJob } from "../lib/types";
import {
  getCameraHealth,
  getDatasets,
  deleteCameraStream,
  clipCameraStream,
  restoreCameraStream,
  addCameraStream,
  getUploadJob,
  CaptureDayRequiredError,
} from "../lib/api";
import { Button, SectionLabel } from "../components/ui";

/**
 * Per-camera manager for one day. A field camera can silently produce unusable
 * footage — an obscured lens (near-black frames), a clip that stopped hours early,
 * or a view with nothing to detect — and the pipeline ingests it fine, so it only
 * surfaces here as an empty/black camera (as happened to camera_02 on 2025-10-15).
 *
 * This page reads each camera's health (advisory, from the backend) and, for
 * powerusers, lets one bad stream be dropped and a replacement re-uploaded into
 * the SAME day without touching the other cameras. View-only users see the health.
 */

// Same rule the upload panel enforces on camera names.
const CAMERA_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

// Human labels for each health issue code. Warm, plain-language — an obscured or
// failed camera should read as "needs a look", not a stack trace.
const ISSUE_LABELS: Record<CameraIssue, string> = {
  dark: "Obscured",
  truncated: "Stopped early",
  no_detections: "No cows",
};

const STAGE_LABEL: Record<UploadJob["stage"], string> = {
  queued: "Queued",
  ingesting: "Sampling frames",
  segmenting: "Detecting cows",
  localizing: "Placing in areas",
  remasking: "Tracing outlines",
  done: "Done",
};

const mb = (bytes: number) => `${(bytes / 1_048_576).toFixed(1)} MB`;

// HH:MM straight off the ISO string (no timezone shift) so the window reads as the
// footage was recorded. Null when the timestamp is missing.
function hhmm(iso: string | null): string | null {
  if (!iso) return null;
  const m = iso.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : null;
}
function fmtWindow(first: string | null, last: string | null): string {
  const a = hhmm(first);
  const b = hhmm(last);
  if (!a && !b) return "—";
  return `${a ?? "??:??"}–${b ?? "??:??"}`;
}

/** Grab a frame from a video File as a JPEG data URL for the tile preview. */
function videoThumb(file: File): Promise<string | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    v.muted = true;
    v.src = url;
    const done = (out: string | null) => {
      URL.revokeObjectURL(url);
      resolve(out);
    };
    v.onloadedmetadata = () => {
      v.currentTime = Math.min(1, (v.duration || 2) / 2);
    };
    v.onseeked = () => {
      try {
        const w = 400;
        const scale = w / (v.videoWidth || w);
        const canvas = document.createElement("canvas");
        canvas.width = w;
        canvas.height = Math.round((v.videoHeight || 225) * scale);
        const ctx = canvas.getContext("2d");
        if (!ctx) return done(null);
        ctx.drawImage(v, 0, 0, canvas.width, canvas.height);
        done(canvas.toDataURL("image/jpeg", 0.7));
      } catch {
        done(null);
      }
    };
    v.onerror = () => done(null);
  });
}

export default function CameraManage() {
  const { dataset = "" } = useParams();
  const { user } = useAuth();
  const canManage = canManageData(user);

  const [health, setHealth] = useState<CameraHealth[] | null>(null);
  const [row, setRow] = useState<DatasetRow | null>(null);
  const [loadErr, setLoadErr] = useState<string | null>(null);

  // Re-read just the health list (after a delete, or once a replacement finishes).
  const refreshHealth = useCallback(async () => {
    const h = await getCameraHealth(dataset);
    setHealth(h);
  }, [dataset]);

  useEffect(() => {
    let alive = true;
    setLoadErr(null);
    Promise.all([getCameraHealth(dataset), getDatasets()])
      .then(([h, rows]) => {
        if (!alive) return;
        setHealth(h);
        setRow(rows.find((r) => r.dataset_id === dataset) ?? null);
      })
      .catch((e: unknown) => {
        if (!alive) return;
        setLoadErr(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [dataset]);

  const title = row?.label ?? row?.day?.slice(0, 10) ?? dataset;
  const showDate = !!(row?.day && row?.label);

  return (
    <div className="flex flex-col gap-10 animate-fade-slide-in">
      <header>
        <Link
          to="/data"
          className="font-mono text-[11px] uppercase tracking-[0.16em] text-gray-tertiary hover:text-accent transition-colors"
        >
          ← Data
        </Link>
        <SectionLabel className="block mt-4">CAMERAS</SectionLabel>
        <h1 className="font-display text-3xl sm:text-4xl font-light text-near-black leading-tight mt-1">
          {title}
        </h1>
        <p className="text-gray-mid text-sm mt-2 max-w-xl">
          {showDate ? (
            <span className="font-mono text-[12px] text-gray-tertiary mr-2">
              {row!.day!.slice(0, 10)}
            </span>
          ) : null}
          {canManage
            ? "Check each camera, drop a stream that came back unusable, and re-upload a replacement into this same day."
            : "Per-camera health for this day."}
        </p>
      </header>

      {loadErr ? (
        <p className="text-sm text-accent-deep bg-accent-soft border border-accent/30 rounded-xl px-3.5 py-2.5">
          {loadErr}
        </p>
      ) : health === null ? (
        <p className="text-gray-tertiary font-mono text-sm">Loading…</p>
      ) : health.length === 0 ? (
        <p className="text-gray-tertiary text-sm max-w-xl">
          No cameras on this day.
        </p>
      ) : (
        <section className="flex flex-col gap-3">
          {health.map((cam) => (
            <CameraCard
              key={cam.camera_id}
              dataset={dataset}
              cam={cam}
              others={health.filter((h) => h.camera_id !== cam.camera_id)}
              canManage={canManage}
              onChanged={refreshHealth}
            />
          ))}
        </section>
      )}

      {canManage && health !== null ? (
        <>
          <div className="h-px bg-border" />
          <AddCamera dataset={dataset} onAdded={refreshHealth} />
        </>
      ) : null}
    </div>
  );
}

/**
 * One camera's row: id, frame count, recording window, detections, and a status —
 * a calm "Healthy" badge, or one warm chip per issue. Two-step controls (a reveal
 * then a Confirm) so a stream is never dropped or trimmed on a stray click:
 *   • Clip — trim to a time window (default = the window the OTHER cameras share),
 *     to line an over-long camera up with the rest.
 *   • Delete — drop the whole stream.
 */
function CameraCard({
  dataset,
  cam,
  others,
  canManage,
  onChanged,
}: {
  dataset: string;
  cam: CameraHealth;
  others: CameraHealth[];
  canManage: boolean;
  onChanged: () => Promise<void>;
}) {
  const [mode, setMode] = useState<"idle" | "delete" | "clip">("idle");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const flagged = !cam.ok;
  const dayStr = cam.first_ts?.slice(0, 10) ?? "";

  // Default clip window = the span the OTHER cameras share (latest start → earliest
  // end), so "clip to sync" trims this one to line up; fall back to this camera's
  // own range when the others don't overlap.
  const startList = others.map((o) => o.first_ts).filter((t): t is string => !!t).sort();
  const endList = others.map((o) => o.last_ts).filter((t): t is string => !!t).sort();
  const syncStart: string | null = startList.length ? startList[startList.length - 1] ?? null : null;
  const syncEnd: string | null = endList.length ? endList[0] ?? null : null;
  const useSync = !!(syncStart && syncEnd && syncStart < syncEnd);
  const [clipStart, setClipStart] = useState(hhmm(useSync ? syncStart : cam.first_ts) ?? "");
  const [clipEnd, setClipEnd] = useState(hhmm(useSync ? syncEnd : cam.last_ts) ?? "");
  const clipValid = !!(clipStart && clipEnd && clipStart < clipEnd && dayStr);

  function close() {
    setMode("idle");
    setErr(null);
  }

  async function confirmDelete() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await deleteCameraStream(dataset, cam.camera_id);
      await onChanged(); // re-reads the health list — this row drops out
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  async function confirmClip() {
    if (busy || !clipValid) return;
    setBusy(true);
    setErr(null);
    try {
      await clipCameraStream(
        dataset, cam.camera_id, `${dayStr}T${clipStart}:00`, `${dayStr}T${clipEnd}:59`,
      );
      await onChanged(); // reloads health — this card refreshes with the trimmed range
      setMode("idle");
      setBusy(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  // Undo is the safe direction (it restores data), so it's a single click.
  const restorable = cam.restorable ?? 0;
  async function restore() {
    if (busy) return;
    setBusy(true);
    setErr(null);
    try {
      await restoreCameraStream(dataset, cam.camera_id);
      await onChanged(); // reloads health — the full range comes back
      setBusy(false);
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div
      className={
        "bg-surface border rounded-2xl p-4 sm:p-5 " +
        (flagged ? "border-warn/50" : "border-border")
      }
    >
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div className="min-w-0">
          <div className="font-mono text-[15px] text-near-black truncate">{cam.camera_id}</div>
          <div className="mt-2 flex items-center gap-2 text-[12px] text-gray-mid tabular-nums">
            <Metric value={cam.n_frames.toLocaleString()} label="frames" />
            <Dot />
            <span className="font-mono text-gray-mid">{fmtWindow(cam.first_ts, cam.last_ts)}</span>
            <Dot />
            <Metric value={cam.n_detections.toLocaleString()} label="cows" />
          </div>
        </div>
        <div className="flex items-center gap-1.5 flex-wrap justify-end">
          {cam.ok ? (
            <span className="inline-flex items-center gap-1.5 text-[12px] px-2.5 py-1 rounded-full bg-accent-soft text-accent-deep">
              <span className="w-1.5 h-1.5 rounded-full bg-accent" />
              Healthy
            </span>
          ) : (
            cam.issues.map((code) => (
              <span
                key={code}
                className="text-[12px] px-2.5 py-1 rounded-full bg-warn/10 border border-warn/40 text-warn"
              >
                {ISSUE_LABELS[code] ?? code}
              </span>
            ))
          )}
        </div>
      </div>

      {canManage ? (
        <div className="mt-4 pt-3 border-t border-border">
          {mode === "delete" ? (
            <div className="flex flex-col gap-2.5">
              <p className="text-[13px] text-gray-mid">
                This <span className="text-near-black">permanently removes this camera stream</span> —
                its frames and detections — from this day. Other cameras are untouched. This cannot be
                undone.
              </p>
              <div className="flex items-center gap-3">
                <button
                  onClick={confirmDelete}
                  disabled={busy}
                  className={
                    "bg-warn text-white text-sm font-medium px-4 py-2 rounded-full hover:opacity-90 active:scale-95 transition-all duration-150" +
                    (busy ? " opacity-50 pointer-events-none" : "")
                  }
                >
                  {busy ? "Removing…" : "Confirm delete"}
                </button>
                <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
              </div>
            </div>
          ) : mode === "clip" ? (
            <div className="flex flex-col gap-2.5">
              <p className="text-[13px] text-gray-mid">
                Keep only frames recorded between these times — everything outside is{" "}
                <span className="text-near-black">permanently removed</span>. Lines this camera up with
                the others; the default is the window they share.
              </p>
              <div className="flex items-center gap-2 flex-wrap text-[13px]">
                <span className="text-gray-tertiary">Keep</span>
                <input
                  type="time"
                  value={clipStart}
                  disabled={busy}
                  onChange={(e) => setClipStart(e.target.value)}
                  aria-label="clip start time"
                  className="bg-surface-sunk border border-border rounded-lg px-2.5 py-1.5 font-mono text-[13px] text-near-black outline-none focus:border-accent"
                />
                <span className="text-gray-tertiary">to</span>
                <input
                  type="time"
                  value={clipEnd}
                  disabled={busy}
                  onChange={(e) => setClipEnd(e.target.value)}
                  aria-label="clip end time"
                  className="bg-surface-sunk border border-border rounded-lg px-2.5 py-1.5 font-mono text-[13px] text-near-black outline-none focus:border-accent"
                />
              </div>
              <div className="flex items-center gap-3">
                <button
                  onClick={confirmClip}
                  disabled={busy || !clipValid}
                  className={
                    "bg-warn text-white text-sm font-medium px-4 py-2 rounded-full hover:opacity-90 active:scale-95 transition-all duration-150" +
                    (busy || !clipValid ? " opacity-50 pointer-events-none" : "")
                  }
                >
                  {busy ? "Clipping…" : "Confirm clip"}
                </button>
                <Button variant="ghost" onClick={close} disabled={busy}>Cancel</Button>
              </div>
            </div>
          ) : (
            <div className="flex items-center gap-2 flex-wrap">
              {restorable > 0 ? (
                <button
                  onClick={restore}
                  disabled={busy}
                  title={`Undo clipping — restore ${restorable.toLocaleString()} frames removed by a previous clip`}
                  className="inline-flex items-center gap-1.5 text-[13px] text-accent-deep border border-accent/40 rounded-full px-3.5 py-1.5 hover:bg-accent-soft transition-colors disabled:opacity-50 disabled:pointer-events-none"
                >
                  ↩ Undo clip · {restorable.toLocaleString()}
                </button>
              ) : null}
              <button
                onClick={() => setMode("clip")}
                title="Trim this camera to a time window so it lines up with the others — removes frames outside it"
                className="inline-flex items-center gap-1.5 text-[13px] text-text border border-border rounded-full px-3.5 py-1.5 hover:border-accent hover:text-accent-deep transition-colors"
              >
                ✂ Clip this stream
              </button>
              <button
                onClick={() => setMode("delete")}
                className="inline-flex items-center gap-1.5 text-[13px] text-warn border border-warn/40 rounded-full px-3.5 py-1.5 hover:bg-warn/10 transition-colors"
              >
                🗑 Delete this camera
              </button>
            </div>
          )}
          {err ? (
            <p className="mt-3 text-sm text-accent-deep bg-accent-soft border border-accent/30 rounded-xl px-3.5 py-2.5">
              {err}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Add or replace one camera stream in this day. Picking a name that already exists
 * replaces that camera; a new name adds one — either way the OTHER cameras are
 * left alone. The clip is uploaded, then the backend auto-processes just this
 * stream (sample → detect → place); we poll the job for a live progress bar.
 */
function AddCamera({ dataset, onAdded }: { dataset: string; onAdded: () => Promise<void> }) {
  const [file, setFile] = useState<File | null>(null);
  const [thumb, setThumb] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [dragging, setDragging] = useState(false);
  const [job, setJob] = useState<UploadJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [warnings, setWarnings] = useState<string[] | null>(null);
  // The backend couldn't infer the capture day and the day has no date on record.
  const [needDay, setNeedDay] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const timer = useRef<number | null>(null);
  const alive = useRef(true);

  // Cancel the poll and block any late setState after the page unmounts.
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      if (timer.current) window.clearTimeout(timer.current);
    };
  }, []);

  const busy =
    submitting || (job !== null && job.status !== "done" && job.status !== "failed");
  const nameValid = CAMERA_RE.test(name.trim());
  const canUpload = !!file && nameValid && !busy;

  function attach(f: File | null) {
    setFile(f);
    setThumb(null);
    if (f) {
      videoThumb(f).then((t) => {
        if (alive.current) setThumb(t);
      });
    }
  }

  function poll(id: string) {
    const tick = async () => {
      try {
        const jb = await getUploadJob(id);
        if (!alive.current) return;
        setJob(jb);
        if (jb.status === "done") {
          setWarnings(jb.warnings ?? []);
          await onAdded();
          return;
        }
        if (jb.status === "failed") {
          setErr(jb.error || jb.message);
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (!alive.current) return;
      timer.current = window.setTimeout(tick, 1500);
    };
    tick();
  }

  async function submit() {
    setErr(null);
    setNeedDay(false);
    setWarnings(null);
    const form = new FormData();
    form.append("video", file as File);
    form.append("camera", name.trim());
    setSubmitting(true);
    try {
      const started = await addCameraStream(dataset, form);
      if (!alive.current) return;
      setJob(started);
      poll(started.job_id);
    } catch (e) {
      if (!alive.current) return;
      if (e instanceof CaptureDayRequiredError) {
        setNeedDay(true);
        setErr(e.message);
        return;
      }
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      if (alive.current) setSubmitting(false);
    }
  }

  function reset() {
    if (timer.current) window.clearTimeout(timer.current);
    setFile(null);
    setThumb(null);
    setName("");
    setJob(null);
    setSubmitting(false);
    setWarnings(null);
    setNeedDay(false);
    setErr(null);
  }

  // ---- completion state -----------------------------------------------------
  if (job && job.status === "done") {
    return (
      <section>
        <SectionLabel>ADD OR REPLACE A CAMERA</SectionLabel>
        <div className="mt-4 flex items-start gap-3">
          <span className="grid place-items-center w-10 h-10 rounded-full bg-accent-soft text-accent-deep text-lg shrink-0">
            ✓
          </span>
          <div>
            <h2 className="font-display text-xl text-near-black leading-tight">
              Camera processed
            </h2>
            <p className="text-gray-mid text-sm mt-1">
              {job.frames.toLocaleString()} frames · {job.detections.toLocaleString()} cows detected —
              added to this day.
            </p>
          </div>
        </div>
        {warnings && warnings.length ? (
          <div className="mt-4 bg-warn/10 border border-warn/40 rounded-xl px-3.5 py-2.5">
            <p className="text-[13px] text-near-black">Heads up — this upload still looks off:</p>
            <ul className="mt-1.5 list-disc pl-5 text-[13px] text-warn">
              {warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          </div>
        ) : null}
        <div className="mt-5">
          <Button variant="ghost" onClick={reset}>
            Add another camera
          </Button>
        </div>
      </section>
    );
  }

  return (
    <section>
      <SectionLabel>ADD OR REPLACE A CAMERA</SectionLabel>
      <h2 className="font-display text-2xl font-light text-near-black leading-tight mt-1">
        Add or replace a camera
      </h2>
      <p className="text-gray-mid text-sm mt-1.5 max-w-md">
        Upload one clip. Use an existing camera name to replace that stream, or a new
        name to add one — either way the other cameras in this day are left untouched.
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-[minmax(0,18rem)_1fr] gap-5 mt-6 items-start">
        {/* drop tile */}
        <div
          onDragOver={busy ? undefined : (e) => { e.preventDefault(); setDragging(true); }}
          onDragLeave={busy ? undefined : () => setDragging(false)}
          onDrop={
            busy
              ? undefined
              : (e) => {
                  e.preventDefault();
                  setDragging(false);
                  const f = e.dataTransfer.files?.[0];
                  if (f) attach(f);
                }
          }
          className={
            "group relative rounded-2xl aspect-[3/2] flex flex-col overflow-hidden transition-colors " +
            (dragging
              ? "border-2 border-accent bg-accent-soft"
              : thumb
                ? "border border-accent/40"
                : file
                  ? "border border-accent/40 bg-accent-soft/60"
                  : "border border-dashed border-border bg-surface hover:border-accent hover:bg-accent-soft/30")
          }
        >
          {thumb ? (
            <>
              <img src={thumb} alt="" className="absolute inset-0 w-full h-full object-cover" />
              <div className="absolute inset-0 bg-gradient-to-b from-black/45 via-black/5 to-black/60" />
            </>
          ) : null}
          <label
            className={
              "relative z-10 flex-1 flex flex-col items-center justify-center px-3 text-center " +
              (busy ? "cursor-default" : "cursor-pointer")
            }
          >
            <input
              type="file"
              accept="video/*"
              className="hidden"
              disabled={busy}
              onChange={(e) => attach(e.target.files?.[0] ?? null)}
            />
            {file ? (
              <div className="w-full">
                <div className={"text-[12px] truncate " + (thumb ? "text-white" : "text-near-black")}>
                  {file.name}
                </div>
                <div className={"text-[11px] mt-0.5 " + (thumb ? "text-white/70" : "text-gray-tertiary")}>
                  {mb(file.size)}
                  {busy ? "" : " · replace"}
                </div>
              </div>
            ) : (
              <div className="flex flex-col items-center">
                <span className="text-2xl text-gray-tertiary/70 group-hover:text-accent transition-colors">↑</span>
                <span className="text-[12px] text-gray-mid mt-2 leading-snug">
                  Drop video
                  <br />
                  or click
                </span>
              </div>
            )}
          </label>
        </div>

        {/* name + action */}
        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] text-gray-tertiary">Camera name</span>
            <input
              value={name}
              disabled={busy}
              onChange={(e) => setName(e.target.value)}
              placeholder="camera_02"
              aria-label="camera name"
              className={
                "w-full bg-surface-sunk border rounded-xl px-3.5 py-2.5 font-mono text-[13px] outline-none transition-colors " +
                (name.length === 0
                  ? "border-border focus:border-accent text-near-black"
                  : nameValid
                    ? "border-accent text-near-black"
                    : "border-warn text-warn")
              }
            />
            <span className="text-[11px] text-gray-tertiary">
              Letters, digits, _ or - · matching an existing camera replaces it.
            </span>
          </label>

          {job && busy ? (
            <Progress job={job} />
          ) : submitting ? (
            <p className="text-[13px] text-gray-mid">Sending your footage to the server…</p>
          ) : (
            <div className="flex items-center gap-4 flex-wrap">
              <Button onClick={submit} disabled={!canUpload}>
                Upload camera
              </Button>
              {!file ? (
                <span className="text-[13px] text-gray-tertiary">Attach a clip to upload.</span>
              ) : !nameValid ? (
                <span className="text-[13px] text-gray-tertiary">Name uses letters, digits, _ or - only.</span>
              ) : null}
            </div>
          )}
        </div>
      </div>

      {needDay ? (
        <p className="mt-4 text-sm text-near-black bg-warn/10 border border-warn/40 rounded-xl px-3.5 py-2.5">
          We couldn't read a recording time from this clip and the day has no date on record, so we
          can't place it. Try a clip whose camera wrote a timestamp.
        </p>
      ) : err && !needDay ? (
        <p className="mt-4 text-sm text-accent-deep bg-accent-soft border border-accent/30 rounded-xl px-3.5 py-2.5">
          {err}
        </p>
      ) : null}
    </section>
  );
}

function Progress({ job }: { job: UploadJob }) {
  const pct = Math.round(job.progress * 100);
  return (
    <div className="mt-1">
      <div className="flex items-baseline justify-between">
        <span className="font-display text-3xl text-near-black tabular-nums leading-none">{pct}%</span>
        <span className="text-[13px] text-gray-mid">{STAGE_LABEL[job.stage]}</span>
      </div>
      <div className="h-2 w-full rounded-full bg-surface-sunk overflow-hidden mt-3">
        <div
          className="h-full bg-accent rounded-full transition-[width] duration-500 ease-out"
          style={{ width: `${Math.max(4, job.progress * 100)}%` }}
        />
      </div>
      <p className="text-[13px] text-gray-mid mt-2">{job.message}</p>
      <p className="text-[12px] text-gray-tertiary mt-1">
        Processing runs on the server — you can leave this page; it keeps going.
      </p>
    </div>
  );
}

function Metric({ value, label }: { value: string; label: string }) {
  return (
    <span>
      <span className="text-near-black font-medium">{value}</span> {label}
    </span>
  );
}

function Dot() {
  return <span className="text-border">·</span>;
}
