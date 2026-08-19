import { useEffect, useRef, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useDataset } from "../lib/dataset";
import { useTimeline } from "../lib/timeline";
import type { Areas, CountArea as Area, DatasetRow, FrameRow, LocalizeStatus, Site } from "../lib/types";
import {
  frameImg,
  getAreas,
  getAreasFor,
  getDatasets,
  getFrameMap,
  getFrames,
  getLocalizeStatus,
  getPanelAreas,
  getPanelAreasFor,
  getSite,
  orthoImg,
  refImg,
  runLocalize,
  saveAreas,
  savePanelAreas,
} from "../lib/api";
import { ImageClicker } from "../components/ImageClicker";
import { Button, Card, SectionLabel, Working } from "../components/ui";
import { SHELTER_COLOR } from "../lib/palette";

// HH:MM straight off a frame's ISO ts (no timezone shift), for the frame picker.
function hhmm(iso: string | null | undefined): string {
  const m = iso?.match(/T(\d{2}:\d{2})/);
  return m ? m[1] : "";
}

type SaveState = "idle" | "saving" | "saved" | "error";
type Mode = "count" | "panel";

// Localize-after-save watch tuning: how often we poll the box, how long the
// "Updated" confirmation lingers, and a hard cap so the poll always terminates.
const LOCALIZE_POLL_MS = 700;
const LOCALIZE_DONE_LINGER_MS = 4000;
const LOCALIZE_MAX_WATCH_MS = 120_000;
type LocalizePhase = "idle" | "working" | "done" | "failed";

// Canvas height cap. Subtracting the editor's fixed chrome (sticky toolbar, hint,
// chip row, card padding, frame slider, poly controls) keeps BOTH canvases and
// their controls on one laptop screen; the 20rem floor stops a short window from
// squeezing the drawing area down to nothing, and 68vh remains the ceiling on a
// tall monitor. Overflow past the cap is reachable by scrolling as before.
const CANVAS_MAX_H = "min(68vh, max(20rem, calc(100vh - 24rem)))";

/**
 * Per-camera count-area editor. A count area is a named region drawn twice: its
 * `camera_polygon` (image px, on the camera reference frame) is what actually
 * DOES the counting — a detection whose ground point falls inside it is tallied
 * to `"{camera}::{id}"`. Its `ortho_polygon` (ortho px, on the orthophoto) is
 * only used to place the region on the map for display.
 *
 * Two side-by-side closed-polygon editors edit the SAME selected area: LEFT the
 * camera frame (camera_polygon), RIGHT the orthophoto (ortho_polygon). A chip
 * list adds / selects / renames / deletes this camera's areas. Save merges this
 * camera's list back into the full site-wide areas map and persists it.
 */
export default function CountArea() {
  const { dataset: routeDataset = "", camera = "" } = useParams();
  const { dataset: currentDataset, setDataset } = useDataset();
  // The dashboard's currently-selected instant (a timestamp bucket), carried in
  // via the app-wide TimelineProvider — so the editor can open on the exact frame
  // the user was looking at on the dashboard.
  const { frame: dashInstant } = useTimeline();

  const [site, setSite] = useState<Site | null>(null);
  // Which camera frame is used as the drawing backdrop. `frameIdx` null = the
  // mid-day reference frame; a number = that specific frame (raw image). Polygons
  // are in fixed camera-pixel space, so the backdrop never moves existing points.
  const [frames, setFrames] = useState<FrameRow[]>([]);
  const [frameIdx, setFrameIdx] = useState<number | null>(null);
  // A source camera's areas, overlaid (dashed) as a preview before import, so you
  // can see which one fits the current view without committing. Driven by the
  // import panel; cleared when it closes or the camera changes.
  const [previewAreas, setPreviewAreas] = useState<Area[]>([]);
  // Two independent per-camera polygon sets: count areas (tally cows) and panel
  // areas (a cow inside one is 'under a panel'). `mode` picks which is edited.
  const [countMap, setCountMap] = useState<Areas>({});
  const [panelMap, setPanelMap] = useState<Areas>({});
  const [mode, setMode] = useState<Mode>("count");
  const [areas, setAreas] = useState<Area[]>([]);
  const [active, setActive] = useState(0);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveErr, setSaveErr] = useState<string | null>(null);

  // "The box is working" — background localize kicked off by a save. We poll the
  // (global) worker and treat it as working *for this editor* only while
  // routeDataset is queued or is the dataset currently running.
  const [localize, setLocalize] = useState<{
    phase: LocalizePhase;
    updated: number | null;
    error: string | null;
  }>({ phase: "idle", updated: null, error: null });
  // Timers + a generation counter so a new save (or unmount) cancels any poll
  // still in flight and no async tick ever setState()s a dead/stale watch.
  const pollTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const doneTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const pollGen = useRef(0);

  const isPanel = mode === "panel";

  // Deep-link / refresh safe: sync the app's selected day to the one in the URL
  // BEFORE fetching, so getAreas()/getPanelAreas() (which append ?dataset) scope
  // to the dataset this editor is for — not whatever day happened to be selected.
  useEffect(() => {
    if (routeDataset && routeDataset !== currentDataset) setDataset(routeDataset);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [routeDataset, currentDataset]);

  useEffect(() => {
    // Wait until the app's dataset matches the URL so the fetch is scoped right.
    if (!routeDataset || currentDataset !== routeDataset) return;
    let alive = true;
    setLoadErr(null);
    Promise.all([getSite(), getAreas(), getPanelAreas()])
      .then(([s, cnt, pnl]) => {
        if (!alive) return;
        setSite(s);
        setCountMap(cnt);
        setPanelMap(pnl);
        setAreas((mode === "count" ? cnt : pnl)[camera] ?? []);
        setActive(0);
      })
      .catch((e) => alive && setLoadErr(String(e)));
    return () => {
      alive = false;
    };
    // Reload on camera or dataset change; mode switches are local.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera, routeDataset, currentDataset]);

  // Frame picker: load this camera's frame list, and default the drawing backdrop
  // to the frame at the dashboard's selected instant (so the editor opens on what
  // the user was just looking at). Falls back to the reference frame when the
  // camera has no footage at that instant, or nothing was selected. Runs once per
  // camera/day; the dashboard scrubber isn't shown here so `dashInstant` is stable.
  useEffect(() => {
    if (!routeDataset || currentDataset !== routeDataset || !camera) return;
    let alive = true;
    getFrames(camera)
      .then((fr) => {
        if (!alive) return;
        setFrames(fr);
        const mid = fr[Math.floor(fr.length / 2)]?.frame_idx ?? null;
        // Default the backdrop to the dashboard's selected instant, else the middle
        // frame — always a real frame so the slider has a position to sit at.
        if (dashInstant != null) {
          getFrameMap(dashInstant)
            .then((fm) => {
              if (alive) setFrameIdx(fm[camera] ?? mid);
            })
            .catch(() => {
              if (alive) setFrameIdx(mid);
            });
        } else {
          setFrameIdx(mid);
        }
      })
      .catch(() => {
        if (alive) setFrames([]);
      });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camera, routeDataset, currentDataset]);

  const activeMap = isPanel ? panelMap : countMap;
  const setActiveMap = isPanel ? setPanelMap : setCountMap;

  // Swap edit target, committing the current mode's in-memory edits first so a
  // toggle never drops unsaved work.
  function switchMode(m: Mode) {
    if (m === mode) return;
    setActiveMap((prev) => ({ ...prev, [camera]: areas }));
    const nextMap = m === "panel" ? panelMap : countMap;
    setMode(m);
    setAreas(nextMap[camera] ?? []);
    setActive(0);
    setSaveState("idle");
  }

  const ref = site?.references?.[camera] ?? null;
  const ortho = site?.orthophoto ?? null;

  const activeIdx = areas.length ? Math.min(active, areas.length - 1) : -1;
  const activeArea = activeIdx >= 0 ? areas[activeIdx] : null;
  const camPoly = activeArea?.camera_polygon ?? [];
  const orthoPoly = activeArea?.ortho_polygon ?? [];

  // Other areas render as read-only guide rings on each canvas.
  const camGuides = areas
    .filter((_, i) => i !== activeIdx)
    .map((a) => a.camera_polygon)
    .filter((p) => p.length >= 3);
  const orthoGuides = areas
    .filter((_, i) => i !== activeIdx)
    .map((a) => a.ortho_polygon)
    .filter((p) => p.length >= 3);

  // Import preview overlays (dashed), one polygon per source area, per canvas.
  const camPreview = previewAreas.map((a) => a.camera_polygon).filter((p) => p.length >= 3);
  const orthoPreview = previewAreas.map((a) => a.ortho_polygon).filter((p) => p.length >= 3);

  function mutateActive(patch: Partial<Area>) {
    if (activeIdx < 0) return;
    setAreas((prev) => prev.map((a, i) => (i === activeIdx ? { ...a, ...patch } : a)));
    setSaveState("idle");
  }

  function setCamPoly(poly: number[][]) {
    mutateActive({ camera_polygon: poly });
  }
  function setOrthoPoly(poly: number[][]) {
    mutateActive({ ortho_polygon: poly });
  }

  // Fine-adjust: move / delete a single vertex of the active area's polygons.
  const moveCamPoint = (i: number, pt: [number, number]) =>
    setCamPoly(camPoly.map((p, idx) => (idx === i ? pt : p)));
  const deleteCamPoint = (i: number) => setCamPoly(camPoly.filter((_, idx) => idx !== i));
  const moveOrthoPoint = (i: number, pt: [number, number]) =>
    setOrthoPoly(orthoPoly.map((p, idx) => (idx === i ? pt : p)));
  const deleteOrthoPoint = (i: number) => setOrthoPoly(orthoPoly.filter((_, idx) => idx !== i));

  // Frame picker: the backdrop image for the LEFT (camera) canvas, chosen by a
  // slider over this camera's frames.
  const frameSrc = frameIdx != null ? frameImg(camera, frameIdx, "raw") : refImg(camera);
  const framePos = frameIdx != null ? frames.findIndex((f) => f.frame_idx === frameIdx) : -1;
  const sliderPos = framePos >= 0 ? framePos : Math.floor(frames.length / 2);
  const frameLabel =
    framePos >= 0 ? `${hhmm(frames[framePos]?.ts)} · ${framePos + 1}/${frames.length}` : "";

  // Import: append another day/camera's areas into this camera's list (deep-copied,
  // re-named/-ided to stay unique) so they can be tweaked with the point handles
  // rather than redrawn. Coordinates carry over as-is — a moved camera just needs
  // the corners nudged.
  function importAreas(src: Area[]) {
    if (!src.length) return;
    const base = areas.length;
    setAreas((prev) => {
      const out = [...prev];
      for (const a of src) {
        const name = defaultName(out, a.name || camera);
        const id = uniqueSlug(name, out);
        out.push({
          id,
          name,
          camera_polygon: a.camera_polygon.map((p) => [...p]),
          ortho_polygon: a.ortho_polygon.map((p) => [...p]),
        });
      }
      return out;
    });
    setActive(base); // focus the first imported area
    setSaveState("idle");
  }

  function addArea() {
    const name = defaultName(areas, isPanel ? `${camera} panel` : camera);
    const id = uniqueSlug(name, areas);
    setAreas((prev) => [...prev, { id, name, camera_polygon: [], ortho_polygon: [] }]);
    setActive(areas.length);
    setSaveState("idle");
  }

  function deleteArea(i: number) {
    setAreas((prev) => prev.filter((_, idx) => idx !== i));
    setActive((a) => (a > i ? a - 1 : a));
    setSaveState("idle");
  }

  function renameArea(i: number, name: string) {
    setAreas((prev) => prev.map((a, idx) => (idx === i ? { ...a, name } : a)));
    setSaveState("idle");
  }

  function clearLocalizeTimers() {
    if (pollTimer.current) clearTimeout(pollTimer.current);
    if (doneTimer.current) clearTimeout(doneTimer.current);
    pollTimer.current = null;
    doneTimer.current = null;
  }

  // Cancel any in-flight localize watch when this editor unmounts (a camera or
  // dataset change remounts it) so the poll always terminates and no async tick
  // ever touches a component that's gone.
  useEffect(() => {
    return () => {
      pollGen.current++;
      clearLocalizeTimers();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Poll GET /api/localize/status (~700 ms) after a save until this dataset's
  // pass leaves the queue, then briefly confirm. A hard deadline guarantees the
  // loop stops even if the box never reports done. Never blocks the UI.
  function watchLocalize(ds: string) {
    clearLocalizeTimers();
    const gen = ++pollGen.current; // supersede any earlier watch
    const deadline = Date.now() + LOCALIZE_MAX_WATCH_MS;
    setLocalize({ phase: "working", updated: null, error: null });

    const tick = async () => {
      if (gen !== pollGen.current) return; // superseded / unmounted
      let s: LocalizeStatus | null = null;
      try {
        s = await getLocalizeStatus();
      } catch {
        /* transient — let the deadline / next tick decide */
      }
      if (gen !== pollGen.current) return; // changed during the await

      if (s) {
        if (s.status === "failed" && s.dataset === ds) {
          setLocalize({ phase: "failed", updated: null, error: s.error ?? "localize failed" });
          return; // terminal
        }
        const mine = s.pending.includes(ds) || (s.status === "running" && s.dataset === ds);
        if (!mine) {
          // Our pass is done. Report its count only if the last pass was ours.
          setLocalize({
            phase: "done",
            updated: s.dataset === ds ? s.updated : null,
            error: null,
          });
          doneTimer.current = setTimeout(() => {
            if (gen === pollGen.current) {
              setLocalize((p) =>
                p.phase === "done" ? { phase: "idle", updated: null, error: null } : p,
              );
            }
          }, LOCALIZE_DONE_LINGER_MS);
          return; // terminal
        }
      }

      if (Date.now() >= deadline) {
        // Timeout guard: stop watching; the box may still finish in the background.
        setLocalize({ phase: "idle", updated: null, error: null });
        return;
      }
      pollTimer.current = setTimeout(tick, LOCALIZE_POLL_MS);
    };

    // First poll after one interval — the save already enqueued the pass.
    pollTimer.current = setTimeout(tick, LOCALIZE_POLL_MS);
  }

  async function save() {
    // A fresh save supersedes any localize watch still running from a prior one.
    pollGen.current++;
    clearLocalizeTimers();
    setLocalize({ phase: "idle", updated: null, error: null });
    setSaveState("saving");
    setSaveErr(null);
    // Fill in / normalize any id that drifted (empty name etc.) and keep them
    // unique within this camera before persisting.
    const normalized = withUniqueIds(areas);
    const fullMap: Areas = { ...activeMap, [camera]: normalized };
    try {
      await (isPanel ? savePanelAreas : saveAreas)(fullMap);
      setActiveMap(fullMap);
      setAreas(normalized);
      setSaveState("saved");
      // The save POST returned instantly; the box now re-localizes off-thread.
      // Watch it in the background without ever blocking the UI.
      if (routeDataset) watchLocalize(routeDataset);
    } catch (e) {
      setSaveErr(e instanceof Error ? e.message : String(e));
      setSaveState("error");
    }
  }

  // Re-enqueue this day's localize pass after a failed one — the only direct
  // entry to /api/localize (saves and camera ops trigger it implicitly). Rides
  // the same ?dataset the editor's saves use.
  async function retryLocalize() {
    if (!routeDataset) return;
    try {
      await runLocalize();
      watchLocalize(routeDataset);
    } catch (e) {
      setLocalize({
        phase: "failed",
        updated: null,
        error: e instanceof Error ? e.message : String(e),
      });
    }
  }

  const totalVerts = areas.reduce(
    (n, a) => n + a.camera_polygon.length + a.ortho_polygon.length,
    0,
  );

  return (
    <div className="animate-fade-slide-in">
      {/* Editor toolbar. Pulled up into <main>'s top padding and stuck just under
          the app header, so Save — and whether the last one landed — is in view
          from the moment the page opens and stays there while you draw. On a
          laptop the old bottom-of-page Save sat well below the fold. */}
      <div
        className={
          "sticky top-[var(--app-header-h)] z-40 -mx-6 sm:-mx-10 -mt-10 sm:-mt-12 mb-3 " +
          "px-6 sm:px-10 pt-3.5 pb-3 bg-bg border-b border-border"
        }
      >
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          <Link
            to="/"
            className="font-mono text-[11px] text-gray-tertiary hover:text-accent shrink-0"
          >
            ← Dashboard
          </Link>
          <h1 className="font-sans text-xl text-near-black leading-none">
            {isPanel ? "Panel areas" : "Count areas"} ·{" "}
            <span className="text-accent">{camera}</span>
          </h1>
          <ModeToggle mode={mode} onMode={switchMode} />
          <span className="font-mono text-[11px] text-gray-tertiary tabular-nums">
            {areas.length} area{areas.length === 1 ? "" : "s"} · {totalVerts} point
            {totalVerts === 1 ? "" : "s"}
          </span>

          {/* Save + what happened to it, pinned right. */}
          <div className="ml-auto flex items-center gap-3">
            {saveState === "saved" && localize.phase === "idle" ? (
              <span className="font-mono text-[11px] text-accent">✓ Saved</span>
            ) : null}
            {localize.phase === "working" ? (
              <Working label="Assigning cows to areas…" className="font-mono text-[11px]" />
            ) : null}
            {localize.phase === "done" ? (
              <Working
                done
                className="font-mono text-[11px]"
                label={
                  localize.updated != null
                    ? `Updated · ${localize.updated} reassigned`
                    : "Updated"
                }
              />
            ) : null}
            {localize.phase === "failed" ? (
              <>
                <span
                  className="font-mono text-[11px] text-[#e76f51] truncate max-w-[14rem]"
                  title={localize.error ?? undefined}
                >
                  Localize failed — {localize.error}
                </span>
                <Button variant="ghost" onClick={retryLocalize}>
                  Retry
                </Button>
              </>
            ) : null}
            {saveState === "error" ? (
              <span
                className="font-mono text-[11px] text-[#e76f51] truncate max-w-[14rem]"
                title={saveErr ?? undefined}
              >
                Save failed — {saveErr}
              </span>
            ) : null}
            <Button onClick={save} disabled={saveState === "saving"}>
              {saveState === "saving"
                ? "Saving…"
                : isPanel
                  ? "Save panel areas"
                  : "Save count areas"}
            </Button>
          </div>
        </div>
      </div>

      <p className="text-[12.5px] text-text mb-3 max-w-4xl">
        {isPanel ? (
          <>
            Draw the <strong>shade under a panel</strong> on the{" "}
            <strong>camera frame</strong> (left) — a cow whose ground point falls inside
            it counts as <strong>under a panel</strong>; draw the matching shape on the{" "}
            <strong>orthophoto</strong> (right) for the map.
          </>
        ) : (
          <>
            Draw a region on the <strong>camera frame</strong> (left) — its polygon is
            what counts cows; draw the matching region on the{" "}
            <strong>orthophoto</strong> (right) to place it on the map.
          </>
        )}
      </p>

      {loadErr ? (
        <Card className="p-5">
          <span className="font-mono text-[12px] text-[#e76f51]">Failed to load — {loadErr}</span>
        </Card>
      ) : null}

      {/* Area chips: add / select / rename / delete. */}
      <div className="mb-3 flex flex-wrap gap-2 items-center">
        {areas.map((a, i) => {
          const nCam = a.camera_polygon.length;
          const nOrtho = a.ortho_polygon.length;
          return (
            <div
              key={i}
              className={
                "inline-flex items-center gap-2 px-2 py-1 border font-mono text-[11px] " +
                (i === activeIdx
                  ? "border-accent text-accent bg-accent/5"
                  : "border-border text-gray-tertiary")
              }
            >
              <button
                onClick={() => {
                  setActive(i);
                  setSaveState("idle");
                }}
                className="font-mono text-[11px]"
                title="Edit this area"
              >
                ▤
              </button>
              <input
                value={a.name}
                onChange={(e) => renameArea(i, e.target.value)}
                className="w-24 bg-transparent border-b border-border font-mono text-[11px] text-near-black outline-none focus:border-accent"
                title="Area name (its id is a slug of this)"
              />
              <span className={nCam >= 3 ? "text-accent" : "text-[#e76f51]"}>
                cam {nCam}
              </span>
              <span className={nOrtho >= 3 ? "text-accent" : "text-gray-tertiary"}>
                map {nOrtho}
              </span>
              <button
                onClick={() => deleteArea(i)}
                className="text-gray-tertiary hover:text-[#e76f51]"
                title="Delete this area"
              >
                ✕
              </button>
            </div>
          );
        })}
        <Button variant="ghost" onClick={addArea}>
          + Add {isPanel ? "panel" : "area"}
        </Button>
        <ImportAreas
          key={camera}
          currentDataset={routeDataset}
          mode={mode}
          onImport={importAreas}
          onPreview={setPreviewAreas}
        />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        {/* LEFT — camera reference frame: edits camera_polygon (does the counting). */}
        <Card className="p-3.5">
          {ref ? (
            <>
              {/* Frame picker sits here (not spanning both cards) because it only
                  changes THIS camera backdrop — never the orthophoto. */}
              {frames.length ? (
                <div className="mb-2 flex items-center gap-2.5 flex-wrap text-[12px]">
                  <span className="text-gray-tertiary shrink-0">Frame</span>
                  <input
                    type="range"
                    min={0}
                    max={frames.length - 1}
                    value={sliderPos}
                    onChange={(e) => {
                      const f = frames[Number(e.target.value)];
                      if (f) setFrameIdx(f.frame_idx);
                    }}
                    className="flex-1 min-w-[8rem] accent-accent cursor-pointer"
                    aria-label="drawing frame"
                    title="Pick which frame to draw on — a backdrop only; it doesn’t move your areas"
                  />
                  <span className="font-mono text-near-black tabular-nums whitespace-nowrap shrink-0">
                    {frameLabel}
                  </span>
                </div>
              ) : null}
              <ImageClicker
                title={
                  activeArea
                    ? `Camera frame — ${activeArea.name} (${isPanel ? "under panel" : "counts here"})`
                    : `Camera frame — add ${isPanel ? "a panel area" : "an area"} to begin`
                }
                src={frameSrc}
                naturalWidth={ref.width}
                naturalHeight={ref.height}
                mode="polyline"
                closed
                points={camPoly}
                lines={camGuides}
                interactive={activeIdx >= 0}
                onPlace={(pt) => setCamPoly([...camPoly, pt])}
                onMovePoint={moveCamPoint}
                onDeletePoint={deleteCamPoint}
                preview={camPreview}
                maxHeight={CANVAS_MAX_H}
              />
              <PolyControls
                label={activeArea ? `${activeArea.name} · camera` : "camera"}
                poly={camPoly}
                onUndo={() => setCamPoly(camPoly.slice(0, -1))}
                onClear={() => setCamPoly([])}
              />
            </>
          ) : (
            <span className="font-mono text-[12px] text-gray-tertiary">
              No reference frame for {camera}.
            </span>
          )}
        </Card>

        {/* RIGHT — orthophoto: edits ortho_polygon (display placement only). */}
        <Card className="p-3.5">
          {ortho ? (
            <>
              <ImageClicker
                title={
                  activeArea
                    ? `Orthophoto — ${activeArea.name} (map placement)`
                    : "Orthophoto — add an area to begin"
                }
                src={orthoImg()}
                naturalWidth={ortho.width}
                naturalHeight={ortho.height}
                mode="polyline"
                closed
                points={orthoPoly}
                lines={orthoGuides}
                interactive={activeIdx >= 0}
                onPlace={(pt) => setOrthoPoly([...orthoPoly, pt])}
                onMovePoint={moveOrthoPoint}
                onDeletePoint={deleteOrthoPoint}
                preview={orthoPreview}
                maxHeight={CANVAS_MAX_H}
              />
              <PolyControls
                label={activeArea ? `${activeArea.name} · map` : "map"}
                poly={orthoPoly}
                onUndo={() => setOrthoPoly(orthoPoly.slice(0, -1))}
                onClear={() => setOrthoPoly([])}
              />
            </>
          ) : (
            <span className="font-mono text-[12px] text-gray-tertiary">
              No orthophoto configured.
            </span>
          )}
        </Card>
      </div>

    </div>
  );
}

/** Segmented toggle: edit count areas (tally cows) vs panel areas (shelter).
 *  Exported so the manual can render the real control (code-as-documentation). */
export function ModeToggle({ mode, onMode }: { mode: Mode; onMode: (m: Mode) => void }) {
  return (
    <div className="inline-flex border border-border rounded overflow-hidden shrink-0">
      <button
        onClick={() => onMode("count")}
        className={
          "px-3 py-1.5 text-[12px] font-mono transition-colors " +
          (mode === "count" ? "bg-accent text-white" : "text-gray-tertiary hover:text-accent")
        }
      >
        count areas
      </button>
      <button
        onClick={() => onMode("panel")}
        className={
          "px-3 py-1.5 text-[12px] font-mono transition-colors " +
          (mode === "panel" ? "text-white" : "text-gray-tertiary hover:text-gray-mid")
        }
        style={mode === "panel" ? { background: SHELTER_COLOR } : undefined}
      >
        panel areas
      </button>
    </div>
  );
}

/** Undo / clear controls + vertex readout for one polygon editor. */
function PolyControls({
  label,
  poly,
  onUndo,
  onClear,
}: {
  label: string;
  poly: number[][];
  onUndo: () => void;
  onClear: () => void;
}) {
  const ready = poly.length >= 3;
  return (
    <div className="mt-2.5 flex flex-wrap items-center gap-3">
      <Button variant="ghost" disabled={!poly.length} onClick={onUndo}>
        Undo point
      </Button>
      <Button variant="ghost" disabled={!poly.length} onClick={onClear}>
        Clear
      </Button>
      <SectionLabel>
        <span className={ready ? "text-accent" : "text-gray-tertiary"}>
          {label}: {poly.length} pt{poly.length === 1 ? "" : "s"}
          {poly.length > 0 && !ready ? " — need ≥3" : ready ? " ✓" : ""}
        </span>
      </SectionLabel>
    </div>
  );
}

/**
 * A fully styled dropdown (custom listbox): a pill button that opens OUR OWN option
 * list. A native <select> can't style its opened option list (that menu is drawn by
 * the OS), so the day/camera pickers use this instead — matching the app's look both
 * closed and open. Closes on outside-click or after a choice.
 */
function Dropdown({
  value,
  onChange,
  options,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: { value: string; label: string }[];
  placeholder: string;
  ariaLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);
  const selected = options.find((o) => o.value === value);
  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-label={ariaLabel}
        aria-haspopup="listbox"
        aria-expanded={open}
        className={
          "inline-flex items-center gap-2 text-sm border rounded-full pl-4 pr-3 py-2.5 bg-surface " +
          "transition-colors duration-150 outline-none cursor-pointer hover:border-accent " +
          (open ? "border-accent " : "border-border ") +
          (selected ? "text-text" : "text-gray-tertiary")
        }
      >
        <span className="truncate max-w-[12rem]">{selected ? selected.label : placeholder}</span>
        <svg
          className="w-3 h-3 text-gray-tertiary shrink-0"
          viewBox="0 0 20 20"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.25"
          aria-hidden="true"
        >
          <path d="M5 7.5L10 12.5L15 7.5" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
      </button>
      {open ? (
        <div
          role="listbox"
          className="absolute left-0 z-30 mt-1.5 min-w-full max-h-64 overflow-auto rounded-2xl border border-border bg-surface shadow-[0_10px_28px_-10px_rgba(43,42,38,0.25)] py-1"
        >
          {options.length === 0 ? (
            <div className="px-4 py-2 text-[13px] text-gray-tertiary whitespace-nowrap">No options</div>
          ) : (
            options.map((o) => (
              <button
                key={o.value}
                type="button"
                role="option"
                aria-selected={o.value === value}
                onClick={() => {
                  onChange(o.value);
                  setOpen(false);
                }}
                className={
                  "w-full text-left px-4 py-2 text-sm whitespace-nowrap transition-colors hover:bg-accent-soft/60 " +
                  (o.value === value ? "text-accent-deep bg-accent-soft/40" : "text-text")
                }
              >
                {o.label}
              </button>
            ))
          )}
        </div>
      ) : null}
    </div>
  );
}

/**
 * Import another day/camera's areas into the current camera — so a repeat site
 * doesn't have to be redrawn every upload. Pick a day, then a camera on that day;
 * its shapes are appended to the current list (coordinates carry over as-is, to be
 * nudged into place with the point handles). Reads the source day's areas directly
 * (getAreasFor / getPanelAreasFor), independent of the currently-selected day.
 */
function ImportAreas({
  currentDataset,
  mode,
  onImport,
  onPreview,
}: {
  currentDataset: string;
  mode: Mode;
  onImport: (src: Area[]) => void;
  onPreview: (areas: Area[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [days, setDays] = useState<DatasetRow[]>([]);
  const [ds, setDs] = useState("");
  const [srcAreas, setSrcAreas] = useState<Areas | null>(null);
  const [cam, setCam] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  // Overlay the chosen source camera's areas (dashed) on the canvases while the
  // panel is open, and clear it when closed — so you can flip through cameras and
  // see which one fits, without importing and undoing. Cleanup on unmount clears it.
  useEffect(() => {
    onPreview(open && srcAreas && cam ? srcAreas[cam] ?? [] : []);
    return () => onPreview([]);
  }, [open, cam, srcAreas, onPreview]);

  function toggle() {
    const next = !open;
    setOpen(next);
    if (next && days.length === 0) {
      getDatasets()
        .then((rows) => setDays(rows.filter((r) => r.dataset_id !== currentDataset)))
        .catch((e) => setErr(String(e)));
    }
  }

  async function pickDay(id: string) {
    setDs(id);
    setSrcAreas(null);
    setCam("");
    setErr(null);
    if (!id) return;
    setBusy(true);
    try {
      const a = await (mode === "count" ? getAreasFor : getPanelAreasFor)(id);
      setSrcAreas(a);
      // Land on the first camera that has areas, so the preview shows at once and
      // the user just steps from there.
      const first = Object.keys(a).find((c) => (a[c]?.length ?? 0) > 0);
      if (first) setCam(first);
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  }

  const cams = srcAreas ? Object.keys(srcAreas).filter((c) => (srcAreas[c]?.length ?? 0) > 0) : [];
  const picked = srcAreas && cam ? srcAreas[cam] ?? [] : [];

  // Step through the source cameras (wrap around), so each one's preview can be
  // eyeballed in turn without a dropdown.
  function stepCam(dir: 1 | -1) {
    if (!cams.length) return;
    const i = cams.indexOf(cam);
    const from = i >= 0 ? i : 0;
    const next = cams[(from + dir + cams.length) % cams.length];
    if (next) setCam(next);
  }

  function doImport() {
    onImport(picked);
    setOpen(false);
    setDs("");
    setSrcAreas(null);
    setCam("");
  }

  if (!open) {
    return (
      <Button variant="ghost" onClick={toggle}>
        Import areas…
      </Button>
    );
  }

  return (
    <div className="w-full mt-2 border border-border rounded-xl p-3.5 flex flex-col gap-3 bg-surface">
      <div className="flex items-center justify-between">
        <SectionLabel>Import {mode} areas from another day</SectionLabel>
        <button onClick={toggle} className="text-gray-tertiary hover:text-near-black text-sm" aria-label="close import">
          ✕
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-2.5 text-[13px]">
        <Dropdown
          value={ds}
          onChange={pickDay}
          ariaLabel="source day"
          placeholder="Choose a day…"
          options={days.map((d) => ({
            value: d.dataset_id,
            label: d.label ?? d.day?.slice(0, 10) ?? d.dataset_id,
          }))}
        />
        {busy ? <span className="text-gray-tertiary">Loading…</span> : null}
        {srcAreas && cams.length ? (
          <div className="inline-flex items-center gap-1.5" role="group" aria-label="source camera">
            <button
              type="button"
              onClick={() => stepCam(-1)}
              disabled={cams.length <= 1}
              title="Previous camera"
              aria-label="previous camera"
              className="w-8 h-8 grid place-items-center border border-border rounded-full text-gray-mid hover:border-accent hover:text-accent transition-colors disabled:opacity-40 disabled:pointer-events-none"
            >
              ‹
            </button>
            <span className="font-mono text-[13px] text-near-black text-center min-w-[9rem]">
              {cam ? `${cam} · ${srcAreas[cam]?.length ?? 0} area${(srcAreas[cam]?.length ?? 0) === 1 ? "" : "s"}` : "—"}
            </span>
            <button
              type="button"
              onClick={() => stepCam(1)}
              disabled={cams.length <= 1}
              title="Next camera"
              aria-label="next camera"
              className="w-8 h-8 grid place-items-center border border-border rounded-full text-gray-mid hover:border-accent hover:text-accent transition-colors disabled:opacity-40 disabled:pointer-events-none"
            >
              ›
            </button>
          </div>
        ) : null}
        {cam && picked.length ? (
          <Button onClick={doImport}>
            Import {picked.length} area{picked.length === 1 ? "" : "s"}
          </Button>
        ) : null}
      </div>
      {srcAreas && cams.length === 0 ? (
        <span className="text-[12px] text-gray-tertiary">That day has no {mode} areas to import.</span>
      ) : null}
      {err ? <span className="text-[12px] text-[#e76f51]">{err}</span> : null}
      <span className="text-[11px] text-gray-tertiary">
        The chosen camera’s areas preview on the canvases in{" "}
        <span className="text-[#3b82f6] font-medium">dashed blue</span> — Import adds them
        to this camera, then drag the corners to fit and Save.
      </span>
    </div>
  );
}

/** kebab-case slug of a name; falls back to "area" when empty. */
function slugify(name: string): string {
  const s = name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "");
  return s || "area";
}

/** A slug of `name` made unique within `areas` (excluding self at `skip`). */
function uniqueSlug(name: string, areas: Area[], skip = -1): string {
  const base = slugify(name);
  const used = new Set(areas.filter((_, i) => i !== skip).map((a) => a.id));
  if (!used.has(base)) return base;
  let k = 2;
  while (used.has(`${base}-${k}`)) k++;
  return `${base}-${k}`;
}

/** Default name after the camera; numbered only when a camera has several. */
function defaultName(areas: Area[], camera: string): string {
  const base = camera || "area";
  const used = new Set(areas.map((a) => a.name));
  if (!used.has(base)) return base;
  let k = 2;
  while (used.has(`${base} ${k}`)) k++;
  return `${base} ${k}`;
}

/** Re-derive unique ids from names right before persisting. */
function withUniqueIds(areas: Area[]): Area[] {
  const out: Area[] = [];
  for (const a of areas) {
    const id = uniqueSlug(a.name, out);
    out.push({ ...a, id });
  }
  return out;
}
