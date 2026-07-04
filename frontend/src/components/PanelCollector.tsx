import { useEffect, useMemo, useState } from "react";
import type { Panel, Line } from "../lib/types";
import { Button, SectionLabel } from "./ui";
import { ImageClicker } from "./ImageClicker";

const MIN_PTS = 2; // a centre line needs ≥2 points to define a direction
const DEFAULT_WIDTH = 60; // full shelter-band width in image px (band is ±width/2)
const MIN_WIDTH = 4;
const MAX_WIDTH = 400;
const SHELTER = "#e76f51"; // band + current-line accent

/**
 * Per-camera solar-panel CENTRE-LINE collector. The solar panels sit ~2 m in the
 * air, so their ground footprint is barely visible — but the panel's centre line
 * is easy to trace (it follows the visible panel). For each site-wide ortho panel
 * (`orthoPanels`, named by id), the operator traces THIS camera's view of that
 * panel's centre line as an OPEN polyline over the shaded ground strip where cows
 * stand, and sets a band WIDTH (px). Output per panel: `{id, centerline, width}`.
 *
 * The shelter test is image-space: a cow whose ground-contact point falls within
 * ±width/2 of the centre line counts as sheltering. It does NOT feed calibration
 * (calibrate from the ground-lines step); rough tracing is fine.
 *
 * The parent owns the panel list and persists it (`savePanels`); this component
 * only edits it. Centre lines are OPEN polylines (no ring closing).
 */
export function PanelCollector({
  camSrc,
  camW,
  camH,
  orthoPanels,
  camPanels,
  onChange,
}: {
  camSrc: string;
  camW: number;
  camH: number;
  orthoPanels: Panel[]; // site-wide centre lines (from site.panels.ortho), named by id
  camPanels: Panel[]; // this camera's centre lines + widths (edited here)
  onChange: (camPanels: Panel[]) => void;
}) {
  // Which ortho panel we're tracing for this camera. Keyed by id so it survives
  // reordering; default to the first that this camera hasn't traced yet.
  const [activeId, setActiveId] = useState<string>("");
  const active =
    orthoPanels.find((p) => p.id === activeId) ??
    orthoPanels.find(
      (p) => !camPanels.some((c) => c.id === p.id && c.centerline.length >= MIN_PTS),
    ) ??
    orthoPanels[0] ??
    null;
  const activeKey = active?.id ?? "";

  // This camera's committed panel for the active id (if any), shown/edited.
  const existing = active ? camPanels.find((c) => c.id === active.id) : undefined;

  // In-progress centre line + width for the active panel. Seeded from `existing`
  // when the active panel changes so re-tracing starts from what's saved.
  const [curLine, setCurLine] = useState<Line>([]);
  const [width, setWidth] = useState<number>(DEFAULT_WIDTH);

  useEffect(() => {
    const e = camPanels.find((c) => c.id === activeKey);
    // Panel.centerline is number[][]; the clicker state is Line ([x, y] tuples).
    setCurLine(e ? e.centerline.map((p): [number, number] => [p[0], p[1]]) : []);
    setWidth(e && e.width ? e.width : DEFAULT_WIDTH);
    // Only re-seed on active-panel change (not on every camPanels edit), else
    // committing would stomp the in-progress line.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeKey]);

  const ready = curLine.length >= MIN_PTS;

  function saveCenterline() {
    if (!ready || !active) return;
    const others = camPanels.filter((c) => c.id !== active.id);
    onChange([...others, { id: active.id, centerline: curLine, width }]);
  }

  function deletePanel(id: string) {
    onChange(camPanels.filter((c) => c.id !== id));
    if (activeKey === id) {
      setCurLine([]);
      setWidth(DEFAULT_WIDTH);
    }
  }

  function undoVertex() {
    setCurLine((c) => c.slice(0, -1));
  }

  // Guide overlays: every OTHER committed camera centre line, so the operator
  // sees this camera's whole set while tracing one panel.
  const camGuides = useMemo(
    () =>
      camPanels
        .filter((c) => c.id !== activeKey)
        .map((c) => c.centerline)
        .filter((line) => line.length >= 2),
    [camPanels, activeKey],
  );

  // Committed count for the header.
  const tracedCount = camPanels.filter((c) => c.centerline.length >= MIN_PTS).length;

  if (orthoPanels.length === 0) {
    return (
      <div className="px-3 py-2 border border-border font-mono text-[12px] text-gray-mid max-w-3xl">
        No site-wide panel centre lines yet. Add them in the{" "}
        <strong>orthophoto</strong> tab first, then return here to trace each
        panel's centre line from this camera.
      </div>
    );
  }

  const banner = !active
    ? "Select a panel to trace."
    : ready
      ? `Centre line for panel ${active.id}: ${curLine.length} pts — set the band width to cover the shaded strip, then save.`
      : `Trace panel ${active.id}'s centre line: click ≥${MIN_PTS} points along the shaded ground strip. ${curLine.length} so far.`;

  const dirty =
    ready &&
    (!existing ||
      existing.width !== width ||
      JSON.stringify(existing.centerline) !== JSON.stringify(curLine));

  return (
    <div>
      <p className="text-[13px] text-text mb-3 max-w-3xl">
        Trace each panel's <strong>centre line</strong> as this camera sees it —
        the 2 m panel is your guide: place the line over the shaded ground strip
        where cows actually stand under it. Then set the <strong>band width</strong>{" "}
        to cover that strip (the shelter band is ±width/2 around the line). This is
        an image-space shelter test — it does <strong>not</strong> feed
        calibration; calibrate from the ground lines step. Pick a panel, click{" "}
        ≥{MIN_PTS} points along its centre line, adjust the width, then save.
      </p>

      {/* Panel picker — one chip per site-wide panel, marking traced ones. */}
      <div className="mb-3 flex flex-wrap gap-2 items-center">
        {orthoPanels.map((p) => {
          const done = camPanels.some(
            (c) => c.id === p.id && c.centerline.length >= MIN_PTS,
          );
          const isActive = active?.id === p.id;
          return (
            <button
              key={p.id}
              onClick={() => setActiveId(p.id)}
              className={
                "font-mono text-[11px] px-3 py-1 border transition-colors " +
                (isActive
                  ? "border-accent text-accent bg-accent/5"
                  : done
                    ? "border-accent/50 text-accent"
                    : "border-border text-gray-tertiary hover:border-accent hover:text-accent-deep")
              }
              title={done ? "Traced — click to re-trace" : "Not traced yet"}
            >
              {done ? "✓ " : "○ "}
              panel {p.id}
            </button>
          );
        })}
        <span className="font-mono text-[11px] text-gray-tertiary">
          {tracedCount}/{orthoPanels.length} traced for this camera
        </span>
      </div>

      {/* Left: interactive centre-line tracer (zoom/pan). Right: a static band
          preview showing the ±width/2 shelter strip on this camera's frame. */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        <ImageClicker
          title={
            active
              ? `Camera frame — trace panel ${active.id}'s centre line`
              : "Camera frame"
          }
          src={camSrc}
          naturalWidth={camW}
          naturalHeight={camH}
          mode="polyline"
          points={curLine}
          lines={camGuides}
          interactive={!!active}
          onPlace={(pt) => setCurLine((c) => [...c, pt])}
        />
        <BandPreview
          camSrc={camSrc}
          camW={camW}
          camH={camH}
          line={curLine}
          width={width}
        />
      </div>

      <div
        className={
          "mt-4 px-3 py-2 border font-mono text-[12px] " +
          (ready ? "border-accent text-accent" : "border-[#e76f51] text-[#e76f51]")
        }
      >
        {banner}
      </div>

      {/* Width control — slider + number, in image px. Governs the ±width/2 band. */}
      <div className="mt-3 flex flex-wrap items-center gap-3">
        <label className="inline-flex items-center gap-2 font-mono text-[11px] text-gray-mid">
          Band width (px)
          <input
            type="range"
            min={MIN_WIDTH}
            max={MAX_WIDTH}
            step={1}
            value={width}
            disabled={!active}
            onChange={(e) => setWidth(Number(e.target.value))}
            className="accent-accent align-middle"
          />
          <input
            type="number"
            min={MIN_WIDTH}
            max={MAX_WIDTH}
            step={1}
            value={width}
            disabled={!active}
            onChange={(e) =>
              setWidth(
                Math.min(MAX_WIDTH, Math.max(MIN_WIDTH, Number(e.target.value) || 0)),
              )
            }
            className="w-16 bg-transparent border border-border px-2 py-1 font-mono text-[11px] text-near-black outline-none focus:border-accent"
          />
        </label>
        <span className="font-mono text-[10px] text-gray-tertiary">
          shelter band = ±{Math.round(width / 2)} px around the centre line
        </span>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" disabled={!ready || !dirty} onClick={saveCenterline}>
          {existing ? "Update centre line" : "Save centre line"}
        </Button>
        <Button variant="ghost" disabled={curLine.length === 0} onClick={undoVertex}>
          Undo point
        </Button>
        <Button variant="ghost" disabled={curLine.length === 0} onClick={() => setCurLine([])}>
          Clear line
        </Button>
        {existing ? (
          <Button variant="ghost" onClick={() => deletePanel(existing.id)}>
            Delete this centre line
          </Button>
        ) : null}
      </div>

      {camPanels.length > 0 ? (
        <div className="mt-5">
          <div className="mb-2">
            <SectionLabel>Traced centre lines (this camera)</SectionLabel>
          </div>
          <div className="flex flex-col gap-2">
            {camPanels.map((cp) => {
              const op = orthoPanels.find((p) => p.id === cp.id);
              const n = cp.centerline.length;
              const ok = n >= MIN_PTS;
              return (
                <div
                  key={cp.id}
                  className="flex items-center gap-3 bg-surface border border-border px-3 py-2"
                >
                  <span className="font-mono text-[13px] text-near-black w-12 text-center">
                    {cp.id}
                  </span>
                  <span
                    className={
                      "font-mono text-[11px] " + (ok ? "text-accent" : "text-[#e76f51]")
                    }
                  >
                    {n} pt{n === 1 ? "" : "s"}
                    {ok ? "" : ` ⚠ need ≥${MIN_PTS}`}
                  </span>
                  <span className="font-mono text-[11px] text-gray-mid">
                    band {cp.width ?? DEFAULT_WIDTH} px
                  </span>
                  <span
                    className={
                      "font-mono text-[10px] px-2 py-0.5 border " +
                      (op
                        ? "border-accent text-accent"
                        : "border-[#e76f51] text-[#e76f51]")
                    }
                    title={
                      op
                        ? "Matches a site-wide ortho panel (same id)"
                        : "No site-wide ortho panel with this id — add it in the orthophoto tab"
                    }
                  >
                    {op ? "linked ✓" : "no ortho panel"}
                  </span>
                  <div className="ml-auto flex gap-2">
                    <button
                      onClick={() => setActiveId(cp.id)}
                      className="font-mono text-[11px] px-2 py-1 border border-border text-gray-tertiary hover:border-accent hover:text-accent-deep"
                    >
                      re-trace
                    </button>
                    <button
                      onClick={() => deletePanel(cp.id)}
                      className="font-mono text-[11px] px-2 py-1 border border-border text-gray-tertiary hover:border-[#e76f51] hover:text-[#e76f51]"
                    >
                      delete
                    </button>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
      ) : null}

      <div className="mt-4 font-mono text-[11px] text-gray-tertiary">
        {tracedCount} panel centre line{tracedCount === 1 ? "" : "s"} for this camera
        (image-space shelter zones — not calibration anchors).
      </div>
    </div>
  );
}

/**
 * Static preview of the shelter band: this camera's frame with the centre line
 * and a translucent ±width/2 strip drawn over it. Self-contained (no zoom/pan)
 * so the overlay always aligns with the image — the width value is authoritative
 * for the shelter test regardless of this preview's scale. The SVG shares the
 * image's aspect ratio (`preserveAspectRatio="none"` over a box locked to
 * camW/camH), so the fat stroke reads as an even band in image px.
 */
function BandPreview({
  camSrc,
  camW,
  camH,
  line,
  width,
}: {
  camSrc: string;
  camW: number;
  camH: number;
  line: number[][];
  width: number;
}) {
  return (
    <div className="animate-fade-slide-in">
      <div className="mb-2 flex items-center justify-between">
        <SectionLabel>Shelter band preview</SectionLabel>
        <span className="font-mono text-[10px] text-gray-tertiary">
          ±{Math.round(width / 2)} px band
        </span>
      </div>
      <div
        className="relative border border-border bg-surface overflow-hidden"
        style={{ aspectRatio: `${camW} / ${camH}`, maxHeight: "68vh", margin: "0 auto" }}
      >
        <img src={camSrc} className="w-full h-full block object-fill" draggable={false} />
        <svg
          className="absolute inset-0 w-full h-full pointer-events-none"
          viewBox={`0 0 ${camW} ${camH}`}
          preserveAspectRatio="none"
        >
          {line.length >= MIN_PTS ? (
            <>
              <polyline
                points={line.map((p) => `${p[0]},${p[1]}`).join(" ")}
                fill="none"
                stroke={SHELTER}
                strokeOpacity={0.25}
                strokeWidth={width}
                strokeLinecap="round"
                strokeLinejoin="round"
              />
              <polyline
                points={line.map((p) => `${p[0]},${p[1]}`).join(" ")}
                fill="none"
                stroke={SHELTER}
                strokeWidth={1.5}
                vectorEffect="non-scaling-stroke"
              />
            </>
          ) : null}
        </svg>
        {line.length < MIN_PTS ? (
          <div className="absolute inset-0 flex items-center justify-center">
            <span className="font-mono text-[11px] text-gray-tertiary bg-near-black/50 text-white px-2 py-1">
              trace ≥{MIN_PTS} points to preview the band
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}
