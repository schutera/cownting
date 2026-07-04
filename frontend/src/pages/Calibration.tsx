import { useEffect, useRef, useState } from "react";
import type { Site, Line, GroundLine, TiePoint, Panel, PanelSet } from "../lib/types";
import {
  getSite,
  refImg,
  orthoImg,
  saveCalibration,
  saveTiePoints,
  savePanels,
  runLocalize,
} from "../lib/api";
import type { CalibrationResult } from "../lib/api";
import { Card, Button, Chip, SectionLabel } from "../components/ui";
import { LineCollector } from "../components/LineCollector";
import { GroundLineCollector } from "../components/GroundLineCollector";
import { FenceEditor } from "../components/FenceEditor";
import { PanelOrthoEditor } from "../components/PanelOrthoEditor";
import { TiePointCollector } from "../components/TiePointCollector";
import { JointCalibratePanel } from "../components/JointCalibratePanel";
import { CoveragePanel } from "../components/CoveragePanel";
import { DeterminationBar } from "../components/DeterminationBar";

const MIN_LINES = 3;
const MIN_PTS_PER_LINE = 5;
const MIN_GROUND_LINES = 2; // ≥2 ground lines in different orientations pin the ground plane

type Step = 1 | 2 | 3;
type Tab = "camera" | "ortho" | "cross"; // per-camera | orthophoto/fence | cross-camera tie points

// Coerce the loosely-typed site.panels payload into a well-formed PanelSet.
// Panels are now centre line + band width (was footprint polygon); tolerate a
// missing/invalid width and skip legacy `polygon`-only entries.
function asPanelList(v: unknown): Panel[] {
  if (!Array.isArray(v)) return [];
  return v
    .filter(
      (p): p is { id: unknown; centerline: unknown; width?: unknown } =>
        !!p &&
        typeof p === "object" &&
        "centerline" in p &&
        Array.isArray((p as { centerline: unknown }).centerline),
    )
    .map((p) => {
      const w = typeof p.width === "number" && p.width > 0 ? p.width : undefined;
      return {
        id: String(p.id ?? ""),
        centerline: p.centerline as number[][],
        ...(w !== undefined ? { width: w } : {}),
      };
    });
}
function asPanelSet(v: unknown): PanelSet {
  const o = (v ?? {}) as { ortho?: unknown; cameras?: unknown };
  const camerasIn = (o.cameras ?? {}) as Record<string, unknown>;
  const cameras: Record<string, Panel[]> = {};
  for (const k of Object.keys(camerasIn)) cameras[k] = asPanelList(camerasIn[k]);
  return { ortho: asPanelList(o.ortho), cameras };
}

// Coerce the loosely-typed site payload (legacy + new fields) into our arrays.
function asLines(v: unknown): Line[] {
  if (!Array.isArray(v)) return [];
  return v.filter((l): l is Line => Array.isArray(l));
}
function asGroundLines(v: unknown): GroundLine[] {
  if (!Array.isArray(v)) return [];
  return v.filter(
    (l): l is GroundLine =>
      Array.isArray(l) && l.length === 2 && Array.isArray(l[0]) && Array.isArray(l[1]),
  );
}

// --- Draft safety net (localStorage) ---------------------------------------
// The debounced autosave only persists a FULLY computable calibration (≥3 fisheye
// lines + ≥2 ground lines). Anything drawn before that threshold — or lost to a
// camera switch / page reload before the ~1s autosave fires — would otherwise
// vanish. We mirror the in-progress [lines, groundLines] to localStorage per
// camera so those inputs survive reloads and camera switches with no backend
// change. The stored value is exactly JSON.stringify([lines, groundLines]), i.e.
// the same shape as `inputsSig`, so signatures compare directly.
const DRAFT_KEY_PREFIX = "cownting.calib.inputs.";
const draftKey = (camera: string) => DRAFT_KEY_PREFIX + camera;

function readDraft(camera: string): { lines: Line[]; groundLines: GroundLine[] } | null {
  try {
    const raw = localStorage.getItem(draftKey(camera));
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed) || parsed.length !== 2) return null;
    // Re-coerce through the same validators the server payload goes through, so a
    // malformed/tampered draft can never inject garbage into state.
    return { lines: asLines(parsed[0]), groundLines: asGroundLines(parsed[1]) };
  } catch {
    return null; // corrupt JSON or storage unavailable → behave as if no draft
  }
}

function writeDraft(camera: string, sig: string): void {
  try {
    localStorage.setItem(draftKey(camera), sig);
  } catch {
    /* quota / storage disabled — the safety net is best-effort, never fatal */
  }
}

function clearDraft(camera: string): void {
  try {
    localStorage.removeItem(draftKey(camera));
  } catch {
    /* ignore */
  }
}

function Counter({ n, min, label }: { n: number; min: number; label: string }) {
  const need = Math.max(0, min - n);
  const ok = need === 0;
  return (
    <span
      className={
        "font-mono text-[11px] px-2 py-1 border " +
        (ok ? "border-accent text-accent" : "border-[#e76f51] text-[#e76f51]")
      }
    >
      {label}: {n} / {min}
      {ok ? " ✓" : ` (need ${need} more)`}
    </span>
  );
}

export default function Calibration() {
  const [site, setSite] = useState<Site | null>(null);
  const [camera, setCamera] = useState<string>("");
  const [tab, setTab] = useState<Tab>("camera");
  const [step, setStep] = useState<Step>(1);

  // Site-wide cross-camera tie points (shared ground features). Loaded once from
  // the site payload; autosaved on change. `tieSavedRef` holds the last-persisted
  // signature so a site refetch neither re-loads over edits nor loops.
  const [tiePoints, setTiePoints] = useState<TiePoint[]>([]);
  const tieSavedRef = useRef<string>("__init__");
  const tieSig = JSON.stringify(tiePoints);

  // Site-wide solar-panel centre lines: `ortho` (shared, edited in the ortho tab)
  // and per-camera centre lines + band widths (edited in the camera tab). Loaded
  // once from the site payload; autosaved to /api/panels on change. Same
  // load-guard/dedupe pattern as the tie points so a post-save refetch neither
  // clobbers nor loops.
  const [panels, setPanels] = useState<PanelSet>({ ortho: [], cameras: {} });
  const panelsSavedRef = useRef<string>("__init__");
  const panelsSig = JSON.stringify(panels);

  const [lines, setLines] = useState<Line[]>([]);
  const [groundLines, setGroundLines] = useState<GroundLine[]>([]);

  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [diag, setDiag] = useState<CalibrationResult | null>(null);
  const [saving, setSaving] = useState(false);

  // Autosave bookkeeping: which camera's inputs are currently loaded, and the
  // signature of the last-persisted inputs — so a post-save site refetch neither
  // clobbers in-progress edits nor loops. `inputsSig` recomputes each render.
  const loadedCamRef = useRef<string | null>(null);
  const savedSigRef = useRef<string>("");
  // Calibration inputs signature. Panel footprints are NOT here — they're shelter
  // zones (autosaved separately via /api/panels), not calibration anchors.
  const inputsSig = JSON.stringify([lines, groundLines]);

  useEffect(() => {
    getSite().then((s) => {
      setSite(s);
      setCamera(s.cameras[0] ?? "");
    });
  }, []);

  // Prefill lines/pairs/height from the loaded entry — ONLY when the selected
  // camera changes. A post-autosave site refetch re-runs this effect with the
  // same camera; the guard makes it a no-op so it never clobbers in-progress
  // edits (residual badges still refresh, since they read from `entry` in render).
  useEffect(() => {
    if (!site || !camera) return;
    if (loadedCamRef.current === camera) return;
    const entry = site.calibration[camera];
    const L = asLines(entry?.lines);
    const GL = asGroundLines((entry as { ground_lines?: unknown })?.ground_lines);
    const serverSig = JSON.stringify([L, GL]);

    // Safety net: if a local draft exists for this camera, holds real work
    // (non-empty), and differs from what the server has, it's unsaved in-progress
    // input — prefer it over the server values. Otherwise fall back to the server.
    const draft = readDraft(camera);
    const draftSig = draft ? JSON.stringify([draft.lines, draft.groundLines]) : null;
    const restore =
      draft &&
      draftSig !== serverSig &&
      (draft.lines.length > 0 || draft.groundLines.length > 0);

    setLines(restore ? draft.lines : L);
    setGroundLines(restore ? draft.groundLines : GL);
    setDiag(null);
    setStep(1);
    loadedCamRef.current = camera;
    // `savedSigRef` always tracks what's PERSISTED (the server), regardless of any
    // restored draft. A restored draft is therefore "dirty" and the autosave effect
    // will persist it the moment it becomes computable; a fresh (server) load reads
    // clean and triggers no spurious save.
    savedSigRef.current = serverSig;
  }, [site, camera]);

  // Debounced autosave (no save button). MUST live above the early return so the
  // hook order is identical every render. Persists ~1s after edits settle, once the
  // fit is computable (≥3 fisheye lines + enough ground/fence anchors); edits that
  // aren't computable yet stay in state and save the moment they become valid.
  useEffect(() => {
    if (!site || !camera) return;
    if (loadedCamRef.current !== camera) return; // this camera's inputs are loaded
    const ref = site.references[camera];
    if (!ref) return;
    const good = lines.filter((l) => l.length >= MIN_PTS_PER_LINE).length;
    // Calibration is anchored by the ground lines (point-on-line); ≥2 in different
    // orientations pin the plane. Panel footprints are shelter zones, not anchors.
    if (good < MIN_LINES || groundLines.length < MIN_GROUND_LINES) return;
    if (inputsSig === savedSigRef.current) return; // nothing changed since save/load
    const sig = inputsSig;
    const timer = setTimeout(async () => {
      setSaving(true);
      setStatus({ ok: true, msg: "Saving…" });
      try {
        const r = await saveCalibration({
          camera,
          method: "center_pillar",
          image_size: [ref.width, ref.height],
          lines,
          ground_lines: groundLines,
          // Panel footprints are shelter zones only — never fed to the fit (the
          // panels sit ~2 m up, so their footprint is an unreliable height-0 anchor).
          panel_lines: [],
        });
        savedSigRef.current = sig; // mark clean BEFORE the refetch so it can't re-trigger
        clearDraft(camera); // these inputs are now persisted server-side — drop the local draft
        const l = await runLocalize();
        setDiag(r);
        setSite(await getSite()); // residual badges refresh; prefill guard prevents clobber
        setStatus({
          ok: true,
          msg: `Auto-saved ✓ reproj ${r.reproj_error.toFixed(1)} px · re-localized ${l.updated} detections.`,
        });
      } catch (e) {
        setStatus({ ok: false, msg: `Autosave failed — ${String(e)}` });
      } finally {
        setSaving(false);
      }
    }, 1000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [inputsSig, camera, site]);

  // Draft safety net: mirror the in-progress inputs to localStorage on every
  // change, so partial work (below the autosave threshold) survives a reload or a
  // camera switch. Only writes once this camera's inputs are actually loaded (so
  // the load transition can't stamp the previous camera's inputs under the new
  // key), and only while the inputs are DIRTY vs. what's persisted — a clean
  // (server-equal) state carries no draft, and the autosave clears the key on a
  // successful save. `savedSigRef.current` is read live (a ref), so it reflects
  // the just-completed load/save without being a dependency.
  useEffect(() => {
    if (!camera) return;
    if (loadedCamRef.current !== camera) return; // inputs belong to this camera
    if (inputsSig === savedSigRef.current) {
      clearDraft(camera); // clean vs. persisted → no draft needed
    } else {
      writeDraft(camera, inputsSig); // unsaved work → keep a local copy
    }
  }, [inputsSig, camera]);

  // Load tie points once from the site payload (site-level, not per-camera).
  useEffect(() => {
    if (!site || tieSavedRef.current !== "__init__") return;
    const tp = (site.tiepoints ?? []) as TiePoint[];
    setTiePoints(tp);
    tieSavedRef.current = JSON.stringify(tp);
  }, [site]);

  // Debounced autosave of the site-wide tie points.
  useEffect(() => {
    if (!site || tieSavedRef.current === "__init__") return; // not loaded yet
    if (tieSig === tieSavedRef.current) return; // unchanged since load/save
    const sig = tieSig;
    const timer = setTimeout(async () => {
      try {
        await saveTiePoints(tiePoints);
        tieSavedRef.current = sig;
        setStatus({ ok: true, msg: `Shared points saved (${tiePoints.length}).` });
      } catch (e) {
        setStatus({ ok: false, msg: `Tie-point save failed — ${String(e)}` });
      }
    }, 800);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tieSig, site]);

  // Load panels once from the site payload (site-level, not per-camera).
  useEffect(() => {
    if (!site || panelsSavedRef.current !== "__init__") return;
    const ps = asPanelSet(site.panels);
    setPanels(ps);
    panelsSavedRef.current = JSON.stringify(ps);
  }, [site]);

  // Debounced autosave of the site-wide panel footprints. Saving re-localizes
  // (recomputes shelter) server-side; refetch the site so the ortho/heatmap
  // overlays and residuals refresh. Guarded like the tie points against loops.
  useEffect(() => {
    if (!site || panelsSavedRef.current === "__init__") return; // not loaded yet
    if (panelsSig === panelsSavedRef.current) return; // unchanged since load/save
    const sig = panelsSig;
    const timer = setTimeout(async () => {
      try {
        const r = await savePanels(panels);
        panelsSavedRef.current = sig; // mark clean BEFORE the refetch so it can't re-trigger
        setSite(await getSite());
        setStatus({
          ok: true,
          msg: `Panels saved (${r.n_ortho} panel${r.n_ortho === 1 ? "" : "s"}, ${r.n_cameras} camera${r.n_cameras === 1 ? "" : "s"}) · re-localized ${r.updated} detections.`,
        });
      } catch (e) {
        setStatus({ ok: false, msg: `Panel save failed — ${String(e)}` });
      }
    }, 1000);
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [panelsSig, site]);

  if (!site) {
    return (
      <div className="p-6">
        <Card>
          <span className="font-mono text-[13px] text-gray-mid">Loading…</span>
        </Card>
      </div>
    );
  }

  const entry = camera ? site.calibration[camera] : undefined;
  const camRef = camera ? site.references[camera] : undefined;
  const ortho = site.orthophoto;

  // --- gating ---
  const goodLines = lines.filter((l) => l.length >= MIN_PTS_PER_LINE).length;
  const nGroundPts = groundLines.reduce((a, gl) => a + gl[0].length, 0); // point-on-line constraints
  const linesOk = goodLines >= MIN_LINES;
  // Calibration is anchored by the ground lines; need ≥2 in different orientations.
  // Panel footprints are shelter zones, not anchors.
  const nGroundLines = groundLines.length;
  const pointsOk = nGroundLines >= MIN_GROUND_LINES;
  const canSave = linesOk && pointsOk && !saving;

  const groundLineResiduals = entry?.per_point_error?.ground_lines;

  const steps: { n: Step; label: string }[] = [
    { n: 1, label: "Fisheye lines" },
    { n: 2, label: "Ground lines" },
    { n: 3, label: "Review & save" },
  ];

  return (
    <div className="p-6 animate-fade-slide-in">
      <h1 className="font-sans text-3xl font-light text-near-black">Calibration</h1>

      <div className="mt-4 flex flex-wrap gap-2">
        {site.cameras.map((c) => (
          <Chip
            key={c}
            active={tab === "camera" && c === camera}
            onClick={() => {
              setCamera(c);
              setTab("camera");
              setStatus(null);
            }}
          >
            {c}
          </Chip>
        ))}
        <Chip active={tab === "ortho"} onClick={() => { setTab("ortho"); setStatus(null); }}>
          orthophoto
        </Chip>
        <Chip active={tab === "cross"} onClick={() => { setTab("cross"); setStatus(null); }}>
          cross-camera
        </Chip>
      </div>

      {tab === "camera" && (
        <div className="font-mono text-[11px] mt-2">
          {entry && entry.saved_at ? (
            <span className="text-accent">
              ✓ {camera} calibrated
              {entry.method ? ` (${entry.method})` : ""}
              {entry.reproj_error !== undefined
                ? ` — reproj ${entry.reproj_error.toFixed(1)} px`
                : ""}
              {entry.line_residual !== undefined
                ? `, lines ${entry.line_residual.toFixed(1)} px`
                : ""}
              , saved {entry.saved_at}
            </span>
          ) : (
            <span className="text-gray-tertiary">
              ○ {camera} not calibrated — trace ≥{MIN_LINES} fisheye lines, then ≥
              {MIN_GROUND_LINES} ground lines
            </span>
          )}
        </div>
      )}

      {!ortho ? (
        <Card className="mt-4">
          <div className="text-gray-mid font-sans">No orthophoto configured.</div>
          <div className="text-[11px] font-mono text-gray-tertiary mt-2">
            Set paths.orthophoto in the site config to enable calibration.
          </div>
        </Card>
      ) : tab === "ortho" ? (
        <div className="mt-4">
          <FenceEditor
            orthoSrc={orthoImg()}
            orthoW={ortho.width}
            orthoH={ortho.height}
            initial={site.fence ?? []}
            onSaved={() => getSite().then(setSite)}
          />
          <PanelOrthoEditor
            orthoSrc={orthoImg()}
            orthoW={ortho.width}
            orthoH={ortho.height}
            panels={panels.ortho}
            onChange={(next) => setPanels((ps) => ({ ...ps, ortho: next }))}
          />
          <JointCalibratePanel cameras={site.cameras} onDone={() => getSite().then(setSite)} />
        </div>
      ) : tab === "cross" ? (
        <div className="mt-4">
          <div className="mb-3">
            <SectionLabel>Cross-camera shared points</SectionLabel>
            <p className="text-[13px] text-text mt-1 max-w-3xl">
              The cameras don't share fence corners, but they do share ground
              features. Click the <strong>same ground point</strong> in every camera
              that sees it (≥2). These become free bundle-adjustment landmarks that
              pull the cameras — especially an off one — into agreement. Saved
              automatically; applied when you run Joint calibrate below.
            </p>
          </div>
          <TiePointCollector
            cameras={site.cameras}
            refs={site.references}
            tiePoints={tiePoints}
            onChange={setTiePoints}
          />
          <JointCalibratePanel cameras={site.cameras} onDone={() => getSite().then(setSite)} />
          <CoveragePanel coverage={site.coverage} cameras={site.cameras} />
        </div>
      ) : !camRef ? (
        <Card className="mt-4">
          <div className="text-gray-mid font-sans">No reference image for this camera.</div>
          <div className="text-[11px] font-mono text-gray-tertiary mt-2">
            A reference frame is needed to place calibration points.
          </div>
        </Card>
      ) : (
        <>
          {/* Stepper */}
          <div className="mt-5 flex flex-wrap gap-2">
            {steps.map((s) => (
              <button
                key={s.n}
                onClick={() => setStep(s.n)}
                className={
                  "font-mono text-[12px] px-3 py-1.5 border transition-colors " +
                  (s.n === step
                    ? "border-accent text-accent bg-accent/5"
                    : "border-border text-gray-tertiary hover:border-accent hover:text-accent-deep")
                }
              >
                {s.n}. {s.label}
              </button>
            ))}
          </div>

          {/* Live counters — always visible so the "need N more" is never lost. */}
          <div className="mt-3 flex flex-wrap gap-2 items-center">
            <Counter n={goodLines} min={MIN_LINES} label="lines" />
            <Counter n={nGroundLines} min={MIN_GROUND_LINES} label="ground lines" />
          </div>

          <div className="mt-5 flex flex-col lg:flex-row lg:items-start gap-5">
            {/* Live determination bar — the per-camera calibration (fisheye lines +
                ground lines). Shown on every step; an exactly-determined fit reads
                amber, not green, despite a perfect self-reproj. */}
            <div className="order-first lg:order-last lg:sticky lg:top-24 border border-border p-3">
              <DeterminationBar
                nCenter={0}
                nGroundEff={nGroundPts}
                nGoodLines={goodLines}
              />
            </div>

            <div className="flex-1 min-w-0">
            {step === 1 ? (
              <div>
                <p className="text-[13px] text-text mb-3 max-w-3xl">
                  Trace <strong>≥{MIN_LINES} polylines</strong> (each ≥{MIN_PTS_PER_LINE} points)
                  along edges that are <strong>straight in reality</strong> but bow in
                  the fisheye image — torque tubes, roof lines, curbs. Spread them
                  across the frame in ≥2 orientations. This fits the undistortion.
                </p>
                <LineCollector
                  src={refImg(camera)}
                  naturalWidth={camRef.width}
                  naturalHeight={camRef.height}
                  lines={lines}
                  onChange={setLines}
                />
              </div>
            ) : null}

            {step === 2 ? (
              <div>
                <div className="mb-2 px-3 py-2 border border-border font-mono text-[11px] text-gray-mid max-w-3xl">
                  The calibration ground anchors, length-agnostic. Trace a{" "}
                  <strong>straight ground feature</strong> — a panel's{" "}
                  <strong>ground centre line</strong> (the row of post bases / torque-tube
                  axis), a curb, a footing, a painted line — in the camera and the same line
                  in the orthophoto; endpoints/length need <strong>not</strong> match. Use{" "}
                  <strong>≥2 orientations</strong> (not all parallel) — e.g. one along the
                  panel rows, one crossing them. Don't trace the panels themselves; they sit
                  ~2 m up.
                </div>
                <GroundLineCollector
                  camSrc={refImg(camera)}
                  camW={camRef.width}
                  camH={camRef.height}
                  orthoSrc={orthoImg()}
                  orthoW={ortho.width}
                  orthoH={ortho.height}
                  lines={groundLines}
                  onChange={setGroundLines}
                  residuals={groundLineResiduals}
                />
              </div>
            ) : null}

            {step === 3 ? (
              <div className="max-w-3xl">
                <div className="border border-border p-4">
                  <SectionLabel>Ready to compute?</SectionLabel>
                  <ul className="mt-2 font-mono text-[12px] flex flex-col gap-1">
                    <li className={linesOk ? "text-accent" : "text-[#e76f51]"}>
                      {linesOk ? "✓" : "✗"} ≥{MIN_LINES} fisheye lines (each ≥{MIN_PTS_PER_LINE} pts):{" "}
                      {goodLines} good
                    </li>
                    <li className={pointsOk ? "text-accent" : "text-[#e76f51]"}>
                      {pointsOk ? "✓" : "✗"} ≥{MIN_GROUND_LINES} ground lines in different
                      orientations: {nGroundLines} traced
                    </li>
                  </ul>
                  <p className="mt-2 font-mono text-[11px] text-gray-tertiary">
                    Calibration = fisheye lines + ground lines. Get reproj to a few px and
                    coverage up, then run Joint calibrate (orthophoto tab). Panel centre
                    lines are image-space shelter zones only.
                  </p>
                </div>
              </div>
            ) : null}
            </div>
          </div>

          {/* Actions */}
          <div className="mt-6 flex flex-wrap gap-3 items-center">
            <Button
              variant="ghost"
              disabled={step === 1}
              onClick={() => setStep((s) => (s > 1 ? ((s - 1) as Step) : s))}
            >
              ← Back
            </Button>
            {step < 3 ? (
              <Button variant="primary" onClick={() => setStep((s) => (s + 1) as Step)}>
                Next →
              </Button>
            ) : null}
            <span className="font-mono text-[11px] text-gray-tertiary">
              {saving
                ? "● Saving…"
                : !canSave
                  ? `○ Auto-saves once ≥${MIN_LINES} lines + ground anchors are set`
                  : inputsSig === savedSigRef.current
                    ? "✓ Auto-saved"
                    : "● Saving shortly…"}
            </span>
          </div>

          {/* Post-save diagnostics banner. */}
          {diag ? (
            <div className="mt-4 border border-accent px-3 py-2 font-mono text-[12px] text-accent">
              Diagnostics — reproj {diag.reproj_error.toFixed(1)} px (max{" "}
              {diag.max_residual.toFixed(1)} px) · line straightness{" "}
              {diag.line_residual.toFixed(1)} px · {diag.n_lines} fisheye lines,{" "}
              {diag.n_ground_lines} ground lines.
              {groundLineResiduals && groundLineResiduals.length
                ? " Red badges flag the worst ground lines — go re-edit them."
                : ""}
            </div>
          ) : null}

          {status ? (
            <div
              className={
                "mt-3 px-3 py-2 border font-mono text-[12px] " +
                (status.ok ? "border-accent text-accent" : "border-[#e76f51] text-[#e76f51]")
              }
            >
              {status.ok ? "✓ " : "✗ "}
              {status.msg}
            </div>
          ) : null}
        </>
      )}
    </div>
  );
}
