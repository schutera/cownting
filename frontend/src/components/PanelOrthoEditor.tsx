import { useState } from "react";
import type { Panel } from "../lib/types";
import { ImageClicker } from "./ImageClicker";
import { Button, SectionLabel } from "./ui";

const MIN_PTS = 2; // a centre line needs ≥2 points to define a direction

/**
 * Site-wide solar-panel centre lines, drawn on the orthophoto. This manages
 * MULTIPLE named panels (`site.panels.ortho`): each is an `id` + an OPEN
 * centre-line polyline that follows the panel row on the map. One panel is
 * "active" for editing at a time; clicks append vertices to the active line and
 * the others render as read-only guide lines.
 *
 * These ortho centre lines are the shared reference every camera traces against
 * (the `id` links a camera's centre line to its ortho panel by name) and the
 * heatmap overlay. They are NOT calibration anchors — panels don't feed the fit.
 *
 * The parent owns the panel list and persists it (`savePanels`); this component
 * only edits it. Lines are OPEN polylines (no ring closing).
 */
export function PanelOrthoEditor({
  orthoSrc,
  orthoW,
  orthoH,
  panels,
  onChange,
}: {
  orthoSrc: string;
  orthoW: number;
  orthoH: number;
  panels: Panel[];
  onChange: (panels: Panel[]) => void;
}) {
  // Which panel is being edited. Clamp defensively against list changes.
  const [active, setActive] = useState(0);
  const activeIdx = panels.length ? Math.min(active, panels.length - 1) : -1;
  const activePanel = activeIdx >= 0 ? panels[activeIdx] : null;
  const activeLine = activePanel ? activePanel.centerline : [];

  // The other panels' centre lines render as static guide lines.
  const guides = panels
    .filter((_, i) => i !== activeIdx)
    .map((p) => p.centerline)
    .filter((line) => line.length >= 2);

  function setActiveLine(line: number[][]) {
    if (activeIdx < 0) return;
    onChange(panels.map((p, i) => (i === activeIdx ? { ...p, centerline: line } : p)));
  }

  function addPanel() {
    const id = nextId(panels);
    onChange([...panels, { id, centerline: [] }]);
    setActive(panels.length); // select the freshly added one
  }

  function deletePanel(i: number) {
    onChange(panels.filter((_, idx) => idx !== i));
    setActive((a) => (a > i ? a - 1 : a));
  }

  function renamePanel(i: number, id: string) {
    onChange(panels.map((p, idx) => (idx === i ? { ...p, id } : p)));
  }

  const complete = activeLine.length >= MIN_PTS;
  const incomplete = activeLine.length > 0 && activeLine.length < MIN_PTS;
  const totalVerts = panels.reduce((n, p) => n + p.centerline.length, 0);

  return (
    <div className="mt-8 border-t border-border pt-6">
      <SectionLabel>Solar-panel centre lines · site-wide</SectionLabel>
      <p className="text-[13px] text-text my-2 max-w-3xl">
        Trace each solar panel's <strong>centre line</strong> on the orthophoto —
        an open polyline that follows the panel row along the map. Add a panel,
        then click points along its centre line (≥{MIN_PTS}). These shared centre
        lines name the panels and overlay the heatmap, and are the reference every
        camera traces its own centre line against (matched by panel <strong>id</strong>).
        They are a <strong>shelter</strong> reference, not calibration anchors.
      </p>

      {/* Panel chips: add / select / rename / delete. */}
      <div className="mt-3 mb-3 flex flex-wrap gap-2 items-center">
        {panels.map((p, i) => {
          const n = p.centerline.length;
          const ready = n >= MIN_PTS;
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
                onClick={() => setActive(i)}
                className="font-mono text-[11px]"
                title="Edit this panel"
              >
                Panel
              </button>
              <input
                value={p.id}
                onChange={(e) => renamePanel(i, e.target.value)}
                className="w-12 bg-transparent border-b border-border font-mono text-[11px] text-near-black outline-none focus:border-accent"
                title="Panel id — links this centre line to each camera's tracing"
              />
              <span className={ready ? "text-accent" : "text-[#e76f51]"}>
                {n} pt{n === 1 ? "" : "s"}
                {ready ? "" : " ⚠"}
              </span>
              <button
                onClick={() => deletePanel(i)}
                className="text-gray-tertiary hover:text-[#e76f51]"
                title="Delete this panel"
              >
                ✕
              </button>
            </div>
          );
        })}
        <Button variant="ghost" onClick={addPanel}>
          + Add panel
        </Button>
      </div>

      <div className="max-w-2xl">
        <ImageClicker
          title={
            activePanel
              ? `Orthophoto — panel ${activePanel.id} centre line`
              : "Orthophoto — add a panel to begin"
          }
          src={orthoSrc}
          naturalWidth={orthoW}
          naturalHeight={orthoH}
          mode="polyline"
          points={activeLine}
          lines={guides}
          interactive={activeIdx >= 0}
          onPlace={(pt) => setActiveLine([...activeLine, pt])}
        />
      </div>

      <div className="mt-3 flex flex-wrap gap-3 items-center">
        <Button
          variant="ghost"
          disabled={!activeLine.length}
          onClick={() => setActiveLine(activeLine.slice(0, -1))}
        >
          Undo point
        </Button>
        <Button
          variant="ghost"
          disabled={!activeLine.length}
          onClick={() => setActiveLine([])}
        >
          Clear panel
        </Button>
        <span className="font-mono text-[11px] text-gray-tertiary">
          {activePanel
            ? `panel ${activePanel.id}: ${activeLine.length} pt${activeLine.length === 1 ? "" : "s"}${incomplete ? ` — need ≥${MIN_PTS}` : complete ? " ✓" : ""}`
            : "no panel selected"}
        </span>
        <span className="font-mono text-[11px] text-gray-tertiary">
          · {panels.length} panel{panels.length === 1 ? "" : "s"} · {totalVerts}{" "}
          point{totalVerts === 1 ? "" : "s"} total
        </span>
      </div>
    </div>
  );
}

/** Next free single-letter id (A, B, …, Z, then P1, P2, …). */
function nextId(panels: Panel[]): string {
  const used = new Set(panels.map((p) => p.id));
  for (let i = 0; i < 26; i++) {
    const c = String.fromCharCode(65 + i);
    if (!used.has(c)) return c;
  }
  let k = 1;
  while (used.has(`P${k}`)) k++;
  return `P${k}`;
}
