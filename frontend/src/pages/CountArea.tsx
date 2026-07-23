import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { useDataset } from "../lib/dataset";
import type { Areas, CountArea as Area, Site } from "../lib/types";
import {
  getAreas,
  getPanelAreas,
  getSite,
  orthoImg,
  refImg,
  saveAreas,
  savePanelAreas,
} from "../lib/api";
import { ImageClicker } from "../components/ImageClicker";
import { Button, Card, SectionLabel } from "../components/ui";
import { SHELTER_COLOR } from "../lib/palette";

type SaveState = "idle" | "saving" | "saved" | "error";
type Mode = "count" | "panel";

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

  const [site, setSite] = useState<Site | null>(null);
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

  async function save() {
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
    } catch (e) {
      setSaveErr(String(e));
      setSaveState("error");
    }
  }

  const totalVerts = areas.reduce(
    (n, a) => n + a.camera_polygon.length + a.ortho_polygon.length,
    0,
  );

  return (
    <div className="animate-fade-slide-in">
      <div className="mb-6 flex items-start justify-between gap-4">
        <div>
          <Link to="/" className="font-mono text-[11px] text-gray-tertiary hover:text-accent">
            ← Dashboard
          </Link>
          <h1 className="font-sans text-2xl text-near-black mt-2">
            {isPanel ? "Panel areas" : "Count areas"} ·{" "}
            <span className="text-accent">{camera}</span>
          </h1>
          <p className="text-[13px] text-text mt-1 max-w-3xl">
            {isPanel ? (
              <>
                Draw the <strong>shade under a panel</strong> on the{" "}
                <strong>camera frame</strong> (left) — a cow whose ground point falls
                inside it counts as <strong>under a panel</strong>. Draw the matching shape
                on the <strong>orthophoto</strong> (right) for the map.
              </>
            ) : (
              <>
                Draw a region on the <strong>camera frame</strong> (left) — its polygon is
                what counts cows. Draw the matching region on the{" "}
                <strong>orthophoto</strong> (right) to place it on the map.
              </>
            )}
          </p>
        </div>
        <ModeToggle mode={mode} onMode={switchMode} />
      </div>

      {loadErr ? (
        <Card className="p-5">
          <span className="font-mono text-[12px] text-[#e76f51]">Failed to load — {loadErr}</span>
        </Card>
      ) : null}

      {/* Area chips: add / select / rename / delete. */}
      <div className="mb-5 flex flex-wrap gap-2 items-center">
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
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* LEFT — camera reference frame: edits camera_polygon (does the counting). */}
        <Card className="p-4">
          {ref ? (
            <>
              <ImageClicker
                title={
                  activeArea
                    ? `Camera frame — ${activeArea.name} (${isPanel ? "under panel" : "counts here"})`
                    : `Camera frame — add ${isPanel ? "a panel area" : "an area"} to begin`
                }
                src={refImg(camera)}
                naturalWidth={ref.width}
                naturalHeight={ref.height}
                mode="polyline"
                closed
                points={camPoly}
                lines={camGuides}
                interactive={activeIdx >= 0}
                onPlace={(pt) => setCamPoly([...camPoly, pt])}
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
        <Card className="p-4">
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

      <div className="mt-6 flex flex-wrap items-center gap-4">
        <Button onClick={save} disabled={saveState === "saving"}>
          {saveState === "saving"
            ? "Saving…"
            : isPanel
              ? "Save panel areas"
              : "Save count areas"}
        </Button>
        <span className="font-mono text-[11px] text-gray-tertiary">
          {areas.length} area{areas.length === 1 ? "" : "s"} · {totalVerts} point
          {totalVerts === 1 ? "" : "s"} total
        </span>
        {saveState === "saved" ? (
          <span className="font-mono text-[11px] text-accent">✓ Saved · localizing…</span>
        ) : null}
        {saveState === "error" ? (
          <span className="font-mono text-[11px] text-[#e76f51]">Save failed — {saveErr}</span>
        ) : null}
      </div>
    </div>
  );
}

/** Segmented toggle: edit count areas (tally cows) vs panel areas (shelter). */
function ModeToggle({ mode, onMode }: { mode: Mode; onMode: (m: Mode) => void }) {
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
    <div className="mt-3 flex flex-wrap items-center gap-3">
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
