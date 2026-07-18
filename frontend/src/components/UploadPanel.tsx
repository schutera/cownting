import { useEffect, useRef, useState } from "react";
import { useDataset } from "../lib/dataset";
import { uploadVideos, getUploadJob, listUploadJobs, CaptureDayRequiredError } from "../lib/api";
import type { UploadJob } from "../lib/types";
import { Button, SectionLabel } from "./ui";

/**
 * Non-technical upload surface. One drop tile per camera (default 4, add/remove
 * freely) laid out as a single filmstrip row — the tiles are the only surfaces,
 * no nested cards. Drop or click to attach a clip; a frame is grabbed from the
 * video for a thumbnail preview so a filled tile reads at a glance. Upload sends
 * every clip as one day and the backend auto-processes it (ingest -> segment ->
 * localize); the job is polled for a live progress bar (tiles stay on screen with
 * their thumbnails), and on completion the day list refreshes.
 */
const CAMERA_RE = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;
const DEFAULT_ZONES = 4;
const pad2 = (n: number) => String(n).padStart(2, "0");
const todayISO = () => new Date().toISOString().slice(0, 10);
const mb = (bytes: number) => `${(bytes / 1_048_576).toFixed(1)} MB`;

const STAGE_LABEL: Record<UploadJob["stage"], string> = {
  queued: "Queued",
  ingesting: "Sampling frames",
  segmenting: "Detecting cows",
  localizing: "Placing in areas",
  done: "Done",
};

type Zone = { key: number; camera: string; file: File | null; thumb: string | null };

function makeZones(n: number): Zone[] {
  return Array.from({ length: n }, (_, i) => ({
    key: i,
    camera: `camera_${pad2(i + 1)}`,
    file: null,
    thumb: null,
  }));
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

export function UploadPanel() {
  const { setDataset, refresh } = useDataset();
  const [zones, setZones] = useState<Zone[]>(() => makeZones(DEFAULT_ZONES));
  const [job, setJob] = useState<UploadJob | null>(null);
  // True while the POST is in flight (files streaming up), before the job exists —
  // so the surface shows continuous feedback instead of a dead wait on a big file.
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dragKey, setDragKey] = useState<number | null>(null);
  // Fallback path: the backend couldn't read the capture day from the video
  // metadata, so we reveal a picker and re-submit with `day` set.
  const [needDay, setNeedDay] = useState(false);
  const [day, setDay] = useState<string>(todayISO());
  const nextKey = useRef(DEFAULT_ZONES);
  const timer = useRef<number | null>(null);

  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
  }, []);

  // Reconnect on mount: processing runs server-side and the job store is
  // process-wide, so if a day is still being detected (after a refresh, in a new
  // tab, or started by someone else) pick it up and resume the progress bar.
  useEffect(() => {
    let cancelled = false;
    listUploadJobs()
      .then((jobs) => {
        if (cancelled) return;
        const active = jobs.find((jb) => jb.status === "queued" || jb.status === "running");
        if (active) {
          setJob(active);
          poll(active.job_id);
        }
      })
      .catch(() => {/* no jobs / transient — just show the empty upload form */});
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const busy =
    submitting || (job !== null && job.status !== "done" && job.status !== "failed");

  function setName(key: number, camera: string) {
    setZones((zs) => zs.map((z) => (z.key === key ? { ...z, camera } : z)));
  }
  function setFile(key: number, file: File | null) {
    setZones((zs) => zs.map((z) => (z.key === key ? { ...z, file, thumb: null } : z)));
    if (file) {
      videoThumb(file).then((thumb) =>
        setZones((zs) => zs.map((z) => (z.key === key && z.file === file ? { ...z, thumb } : z))),
      );
    }
  }
  function addZone() {
    const k = nextKey.current++;
    setZones((zs) => [...zs, { key: k, camera: `camera_${pad2(zs.length + 1)}`, file: null, thumb: null }]);
  }
  function removeZone(key: number) {
    setZones((zs) => (zs.length > 1 ? zs.filter((z) => z.key !== key) : zs));
  }

  const names = zones.map((z) => z.camera.trim());
  const allFilled = zones.length > 0 && zones.every((z) => z.file);
  const allNamed = names.every((n) => CAMERA_RE.test(n));
  const unique = new Set(names).size === names.length;
  const canUpload = allFilled && allNamed && unique && !busy && (!needDay || !!day);

  const hint = !allFilled
    ? "Attach a clip to every camera to upload."
    : !allNamed
      ? "Camera names use letters, digits, _ or - only."
      : !unique
        ? "Camera names must be unique."
        : needDay && !day
          ? "Pick the capture day to continue."
          : null;

  async function submit() {
    setError(null);
    const form = new FormData();
    for (const z of zones) {
      form.append("cameras", z.camera.trim());
      form.append("videos", z.file as File);
    }
    // Only sent on the fallback path, once the picker is showing.
    if (needDay && day) form.append("day", day);
    setSubmitting(true);
    try {
      const started = await uploadVideos(form);
      setJob(started);
      poll(started.job_id);
    } catch (e) {
      if (e instanceof CaptureDayRequiredError) {
        setNeedDay(true);
        setError(`${e.message} We couldn't read it automatically — set it below and upload again.`);
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSubmitting(false);
    }
  }

  function poll(id: string) {
    const tick = async () => {
      try {
        const j = await getUploadJob(id);
        setJob(j);
        if (j.status === "done") {
          await refresh();
          setDataset(j.dataset_id);
          return;
        }
        if (j.status === "failed") {
          setError(j.error || j.message);
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      timer.current = window.setTimeout(tick, 1500);
    };
    tick();
  }

  function reset() {
    if (timer.current) window.clearTimeout(timer.current);
    setJob(null);
    setError(null);
    setNeedDay(false);
    setZones((zs) => zs.map((z) => ({ ...z, file: null, thumb: null })));
  }

  // ---- completion banner (flat, centred) ---------------------------------
  if (job && job.status === "done") {
    return (
      <section className="flex flex-col items-center text-center py-12">
        <span className="grid place-items-center w-16 h-16 rounded-full bg-accent-soft text-accent-deep text-2xl">
          ✓
        </span>
        <h2 className="font-display text-3xl font-light text-near-black mt-5">Upload complete</h2>
        <p className="text-gray-mid text-sm mt-2">
          <span className="text-near-black">{job.label}</span> · {job.frames.toLocaleString()} frames ·{" "}
          {job.detections.toLocaleString()} cows detected — now your active day.
        </p>
        <div className="mt-6">
          <Button variant="ghost" onClick={reset}>Upload another day</Button>
        </div>
      </section>
    );
  }

  return (
    <section>
      {/* header */}
      <div>
        <SectionLabel>NEW UPLOAD</SectionLabel>
        <h2 className="font-display text-2xl sm:text-3xl font-light text-near-black leading-tight mt-1">
          Add a day of footage
        </h2>
        <p className="text-gray-mid text-sm mt-1.5 max-w-md">
          Drop one clip per camera, then upload — every cow is sampled, detected and
          placed automatically.
        </p>
      </div>

      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-5 gap-4 mt-7">
        {zones.map((z) => (
          <DropTile
            key={z.key}
            zone={z}
            busy={busy}
            dragging={dragKey === z.key}
            nameValid={CAMERA_RE.test(z.camera.trim())}
            removable={zones.length > 1}
            onName={(name) => setName(z.key, name)}
            onFile={(file) => setFile(z.key, file)}
            onRemove={() => removeZone(z.key)}
            onDragState={(on) => setDragKey(on ? z.key : null)}
          />
        ))}
        {!busy ? <AddTile onClick={addZone} /> : null}
      </div>

      {/* Fallback: shown only when the capture day couldn't be read from the
          video metadata — the user sets it here and re-uploads. */}
      {needDay && !busy ? (
        <label className="flex items-center gap-2.5 mt-6">
          <span className="uppercase tracking-[0.14em] text-[11px] text-gray-tertiary">Capture day</span>
          <input
            type="date"
            value={day}
            onChange={(e) => setDay(e.target.value)}
            className="font-mono text-[13px] text-near-black bg-transparent border-b border-border focus:border-accent outline-none py-1"
          />
        </label>
      ) : null}

      {job && busy ? (
        <Progress job={job} />
      ) : submitting ? (
        <Uploading />
      ) : (
        <div className="flex items-center gap-4 mt-7 flex-wrap">
          <Button onClick={submit} disabled={!canUpload}>
            Upload {zones.length} {zones.length === 1 ? "camera" : "cameras"}
          </Button>
          {hint ? <span className="text-[13px] text-gray-tertiary">{hint}</span> : null}
        </div>
      )}

      {error ? (
        <p className="mt-4 text-sm text-accent-deep bg-accent-soft border border-accent/30 rounded-xl px-3.5 py-2.5">
          {error}
        </p>
      ) : null}
    </section>
  );
}

/** Pre-job feedback: the clips are streaming to the server. Once the server has
 *  them it returns the job and <Progress> takes over (ingest → detect → place). */
function Uploading() {
  return (
    <div className="mt-8 max-w-xl">
      <div className="flex items-baseline justify-between">
        <span className="font-display text-5xl text-near-black tabular-nums leading-none">···</span>
        <span className="text-[13px] text-gray-mid">Uploading</span>
      </div>
      <div className="h-2 w-full rounded-full bg-surface-sunk overflow-hidden mt-4">
        <div className="h-full w-1/3 bg-accent/60 rounded-full animate-pulse" />
      </div>
      <p className="text-[13px] text-gray-mid mt-3">Sending your footage to the server…</p>
      <p className="text-[12px] text-gray-tertiary mt-1">
        Large clips take a moment — detection starts automatically once they land.
      </p>
    </div>
  );
}

function Progress({ job }: { job: UploadJob }) {
  const pct = Math.round(job.progress * 100);
  return (
    <div className="mt-8 max-w-xl">
      <div className="flex items-baseline justify-between">
        <span className="font-display text-5xl text-near-black tabular-nums leading-none">{pct}%</span>
        <span className="text-[13px] text-gray-mid">{STAGE_LABEL[job.stage]}</span>
      </div>
      <div className="h-2 w-full rounded-full bg-surface-sunk overflow-hidden mt-4">
        <div
          className="h-full bg-accent rounded-full transition-[width] duration-500 ease-out"
          style={{ width: `${Math.max(4, job.progress * 100)}%` }}
        />
      </div>
      <p className="text-[13px] text-gray-mid mt-3">{job.message}</p>
      <p className="text-[12px] text-gray-tertiary mt-1">
        Processing runs on the server — you can leave this page; it keeps going.
      </p>
    </div>
  );
}

/** A single camera slot — one surface, no card-in-card. Empty: dashed drop
 *  target. Filled: the grabbed video frame fills the tile with the name + file
 *  over a legibility gradient. */
function DropTile({
  zone,
  busy,
  dragging,
  nameValid,
  removable,
  onName,
  onFile,
  onRemove,
  onDragState,
}: {
  zone: Zone;
  busy: boolean;
  dragging: boolean;
  nameValid: boolean;
  removable: boolean;
  onName: (name: string) => void;
  onFile: (file: File | null) => void;
  onRemove: () => void;
  onDragState: (on: boolean) => void;
}) {
  const filled = !!zone.file;
  const thumbed = filled && !!zone.thumb;
  const shell = dragging
    ? "border-2 border-accent bg-accent-soft"
    : thumbed
      ? "border border-accent/40"
      : filled
        ? "border border-accent/40 bg-accent-soft/60"
        : "border border-dashed border-border bg-surface hover:border-accent hover:bg-accent-soft/30";

  return (
    <div
      onDragOver={busy ? undefined : (e) => { e.preventDefault(); onDragState(true); }}
      onDragLeave={busy ? undefined : () => onDragState(false)}
      onDrop={
        busy
          ? undefined
          : (e) => {
              e.preventDefault();
              onDragState(false);
              const f = e.dataTransfer.files?.[0];
              if (f) onFile(f);
            }
      }
      className={"group relative rounded-2xl aspect-[3/2] flex flex-col overflow-hidden transition-colors " + shell}
    >
      {thumbed ? (
        <>
          <img src={zone.thumb!} alt="" className="absolute inset-0 w-full h-full object-cover" />
          <div className="absolute inset-0 bg-gradient-to-b from-black/45 via-black/5 to-black/60" />
        </>
      ) : null}

      {removable && !busy ? (
        <button
          onClick={onRemove}
          className={
            "absolute top-2 right-2 z-20 w-6 h-6 grid place-items-center rounded-full transition " +
            (thumbed
              ? "bg-black/40 text-white hover:bg-black/60"
              : "text-gray-tertiary opacity-0 group-hover:opacity-100 hover:bg-black/5 hover:text-accent-deep")
          }
          aria-label={`remove ${zone.camera}`}
        >
          ×
        </button>
      ) : null}

      <input
        value={zone.camera}
        disabled={busy}
        onChange={(e) => onName(e.target.value)}
        aria-label="camera name"
        className={
          "relative z-10 mt-3 mx-2 bg-transparent text-center font-mono text-[12px] tracking-tight outline-none disabled:opacity-100 " +
          (thumbed
            ? "text-white/90 focus:text-white"
            : nameValid
              ? "text-gray-mid focus:text-near-black"
              : "text-accent-deep")
        }
      />

      <label
        className={
          "relative z-10 flex-1 flex flex-col px-3 text-center " +
          (thumbed ? "justify-end pb-2.5" : "justify-center pb-4") +
          (busy ? " cursor-default" : " cursor-pointer")
        }
      >
        <input
          type="file"
          accept="video/*"
          className="hidden"
          disabled={busy}
          onChange={(e) => onFile(e.target.files?.[0] ?? null)}
        />
        {thumbed ? (
          <div className="w-full">
            <div className="text-[12px] text-white truncate">{zone.file!.name}</div>
            <div className="text-[10px] text-white/70 mt-0.5">
              {mb(zone.file!.size)}
              {busy ? "" : " · replace"}
            </div>
          </div>
        ) : filled ? (
          <div className="flex flex-col items-center">
            <span className="text-accent-deep text-xl">🎞</span>
            <span className="text-[12px] text-near-black mt-1.5 leading-snug line-clamp-2 break-all">
              {zone.file!.name}
            </span>
            <span className="text-[11px] text-gray-tertiary mt-0.5">
              {mb(zone.file!.size)}
              {busy ? "" : " · replace"}
            </span>
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
  );
}

function AddTile({ onClick }: { onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="rounded-2xl aspect-[3/2] flex flex-col items-center justify-center gap-2 border border-dashed border-border text-gray-tertiary hover:border-accent hover:text-accent-deep hover:bg-accent-soft/30 transition-colors"
    >
      <span className="text-2xl leading-none">+</span>
      <span className="text-[12px]">Add camera</span>
    </button>
  );
}
