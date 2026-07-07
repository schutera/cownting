import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import type { Areas, CountArea as Area, Site } from "../lib/types";
import { getAreas, getSite, orthoImg, refImg, saveAreas } from "../lib/api";
import { ImageClicker } from "../components/ImageClicker";
import { Button, Card, SectionLabel } from "../components/ui";

type SaveState = "idle" | "saving" | "saved" | "error";

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
  const { camera = "" } = useParams();

  const [site, setSite] = useState<Site | null>(null);
  const [allAreas, setAllAreas] = useState<Areas>({});
  const [areas, setAreas] = useState<Area[]>([]);
  const [active, setActive] = useState(0);
  const [loadErr, setLoadErr] = useState<string | null>(null);
  const [saveState, setSaveState] = useState<SaveState>("idle");
  const [saveErr, setSaveErr] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoadErr(null);
    Promise.all([getSite(), getAreas()])
      .then(([s, a]) => {
        if (!alive) return;
        setSite(s);
        setAllAreas(a);
        setAreas(a[camera] ?? []);
        setActive(0);
      })
      .catch((e) => alive && setLoadErr(String(e)));
    return () => {
      alive = false;
    };
  }, [camera]);

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
    const name = defaultName(areas);
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
    const fullMap: Areas = { ...allAreas, [camera]: normalized };
    try {
      await saveAreas(fullMap);
      setAllAreas(fullMap);
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
      <div className="mb-6 flex items-center justify-between gap-4">
        <div>
          <Link to="/" className="font-mono text-[11px] text-gray-tertiary hover:text-accent">
            ← Dashboard
          </Link>
          <h1 className="font-sans text-2xl text-near-black mt-2">
            Count areas · <span className="text-accent">{camera}</span>
          </h1>
          <p className="text-[13px] text-text mt-1 max-w-3xl">
            Draw a region on the <strong>camera frame</strong> (left) — its polygon is
            what counts cows. Draw the matching region on the <strong>orthophoto</strong>{" "}
            (right) to place it on the map. Both belong to the same selected area.
          </p>
        </div>
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
          + Add area
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
                    ? `Camera frame — ${activeArea.name} (counts here)`
                    : "Camera frame — add an area to begin"
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
          {saveState === "saving" ? "Saving…" : "Save count areas"}
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

/** Default "Area N" name not already taken. */
function defaultName(areas: Area[]): string {
  const used = new Set(areas.map((a) => a.name));
  let k = areas.length + 1;
  while (used.has(`Area ${k}`)) k++;
  return `Area ${k}`;
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
