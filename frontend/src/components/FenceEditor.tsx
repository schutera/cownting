import { useState } from "react";
import { ImageClicker } from "./ImageClicker";
import { Button, SectionLabel } from "./ui";
import { saveFence } from "../lib/api";

/**
 * Site-wide cow-enclosure polygon, drawn on the orthophoto. Localized
 * detections outside it are dropped from the heatmap. Shared across cameras.
 */
export function FenceEditor({
  orthoSrc,
  orthoW,
  orthoH,
  initial,
  onSaved,
}: {
  orthoSrc: string;
  orthoW: number;
  orthoH: number;
  initial: number[][];
  onSaved: () => void;
}) {
  // The stored fence is a closed ring (first == last); edit it as distinct
  // corners and let the backend re-close on save.
  const [poly, setPoly] = useState<number[][]>(() => openRing(initial));
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [saving, setSaving] = useState(false);

  const complete = poly.length >= 3;
  const incomplete = poly.length > 0 && poly.length < 3;

  async function save() {
    setSaving(true);
    setStatus({ ok: true, msg: complete ? "Saving fence…" : "Clearing fence…" });
    try {
      const r = await saveFence(poly);
      onSaved();
      setStatus({
        ok: true,
        msg: complete
          ? `Saved fence (${r.n_vertices} vertices) · re-localized ${r.updated} detections.`
          : `Fence cleared · re-localized ${r.updated} detections.`,
      });
    } catch (e) {
      setStatus({ ok: false, msg: String(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="mt-8 border-t border-border pt-6">
      <SectionLabel>Fenced area · site-wide</SectionLabel>
      <p className="text-[13px] text-text my-2 max-w-3xl">
        Trace the cow enclosure on the orthophoto — click its corners in order.
        The polygon closes automatically (the last corner joins back to the
        first). Localized cows outside it are dropped from the heatmap (a
        physical bound, tighter than the per-camera hull). Shared across all
        cameras. ≥3 corners.
      </p>
      <div className="max-w-2xl">
        <ImageClicker
          title="Orthophoto — enclosure polygon"
          src={orthoSrc}
          naturalWidth={orthoW}
          naturalHeight={orthoH}
          mode="polyline"
          points={poly}
          lines={[]}
          closed
          onPlace={(pt) => setPoly([...poly, pt])}
        />
      </div>
      <div className="mt-3 flex flex-wrap gap-3 items-center">
        <Button variant="primary" disabled={saving || incomplete} onClick={save}>
          {saving ? "Saving…" : complete ? "Save fence" : "Clear fence"}
        </Button>
        <Button
          variant="ghost"
          disabled={!poly.length}
          onClick={() => setPoly(poly.slice(0, -1))}
        >
          Undo point
        </Button>
        <Button variant="ghost" disabled={!poly.length} onClick={() => setPoly([])}>
          Clear
        </Button>
        <span className="font-mono text-[11px] text-gray-tertiary">
          {poly.length} vertices
        </span>
        {status ? (
          <span
            className={
              "font-mono text-[11px] " +
              (status.ok ? "text-accent" : "text-[#e76f51]")
            }
          >
            {status.msg}
          </span>
        ) : null}
      </div>
    </div>
  );
}

/** Drop a trailing vertex equal to the first, so a closed ring edits as its
 *  distinct corners. The backend re-closes on save. */
function openRing(poly: number[][]): number[][] {
  const n = poly.length;
  if (n > 1 && poly[0][0] === poly[n - 1][0] && poly[0][1] === poly[n - 1][1]) {
    return poly.slice(0, -1);
  }
  return poly;
}
