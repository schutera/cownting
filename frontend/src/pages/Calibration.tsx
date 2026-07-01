import { useEffect, useState } from "react";
import type { Site } from "../lib/types";
import {
  getSite,
  refImg,
  orthoImg,
  saveCalibration,
  runLocalize,
} from "../lib/api";
import { Card, Button, Chip, SectionLabel } from "../components/ui";
import { ImageClicker } from "../components/ImageClicker";
import { PointCrop } from "../components/PointCrop";

type Editing = { pair: number; which: "cam" | "ortho" } | null;

export default function Calibration() {
  const [site, setSite] = useState<Site | null>(null);
  const [camera, setCamera] = useState<string>("");
  const [camPts, setCamPts] = useState<number[][]>([]);
  const [orthoPts, setOrthoPts] = useState<number[][]>([]);
  const [editing, setEditing] = useState<Editing>(null);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    getSite().then((s) => {
      setSite(s);
      setCamera(s.cameras[0] ?? "");
    });
  }, []);

  useEffect(() => {
    if (!site || !camera) return;
    const entry = site.calibration[camera];
    setCamPts(entry?.cam_points ?? []);
    setOrthoPts(entry?.ortho_points ?? []);
    setEditing(null);
    // NB: don't clear `status` here — this effect also re-runs after a save
    // re-fetches the site, and we want the "Saved" banner to survive that.
  }, [site, camera]);

  // Esc cancels an in-progress edit.
  useEffect(() => {
    if (!editing) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") setEditing(null);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [editing]);

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

  // A cam point placed but not yet matched with an ortho point.
  const pendingCam = camPts.length > orthoPts.length;
  const pairCount = Math.min(camPts.length, orthoPts.length);
  const rowCount = Math.max(camPts.length, orthoPts.length);

  // What the next click targets.
  const activeWhich: "cam" | "ortho" = editing
    ? editing.which
    : pendingCam
      ? "ortho"
      : "cam";
  const activeIsCam = activeWhich === "cam";

  function handlePlace(pt: [number, number]) {
    if (editing) {
      if (editing.which === "cam") {
        setCamPts(camPts.map((p, i) => (i === editing.pair ? pt : p)));
      } else {
        setOrthoPts(orthoPts.map((p, i) => (i === editing.pair ? pt : p)));
      }
      setEditing(null);
      return;
    }
    if (activeIsCam) setCamPts([...camPts, pt]);
    else setOrthoPts([...orthoPts, pt]);
  }

  function deletePair(i: number) {
    setCamPts(camPts.filter((_, idx) => idx !== i));
    setOrthoPts(orthoPts.filter((_, idx) => idx !== i));
    setEditing(null);
  }

  function undoLast() {
    // Remove the most recently placed point.
    if (pendingCam) setCamPts(camPts.slice(0, -1));
    else if (orthoPts.length > 0) setOrthoPts(orthoPts.slice(0, -1));
    setEditing(null);
  }

  function clearAll() {
    setCamPts([]);
    setOrthoPts([]);
    setEditing(null);
  }

  async function computeAndSave() {
    setSaving(true);
    setStatus({ ok: true, msg: "Computing homography…" });
    try {
      const r = await saveCalibration(camera, camPts, orthoPts);
      const l = await runLocalize();
      // Re-fetch so the calibrated-status header (reproj, saved_at) updates.
      setSite(await getSite());
      setStatus({
        ok: true,
        msg: `Saved — reproj ${r.reproj_error.toFixed(1)} px · localized ${l.updated} detections.`,
      });
    } catch (e) {
      setStatus({ ok: false, msg: String(e) });
    } finally {
      setSaving(false);
    }
  }

  // The reference crop shown next to the active image: the matching point on
  // the OTHER image, so you know which landmark you're looking for.
  let refCrop: { label: string; src: string; w: number; h: number; pt?: number[] } | null =
    null;
  if (editing) {
    if (editing.which === "ortho" && camRef) {
      refCrop = { label: "Camera (match this)", src: refImg(camera), w: camRef.width, h: camRef.height, pt: camPts[editing.pair] };
    } else if (editing.which === "cam" && ortho) {
      refCrop = { label: "Orthophoto (match this)", src: orthoImg(), w: ortho.width, h: ortho.height, pt: orthoPts[editing.pair] };
    }
  } else if (pendingCam && camRef) {
    refCrop = { label: "Camera (match this)", src: refImg(camera), w: camRef.width, h: camRef.height, pt: camPts[camPts.length - 1] };
  }

  const banner = editing
    ? `Editing the ${editing.which === "cam" ? "camera" : "orthophoto"} point of pair #${editing.pair + 1} — click its new location.`
    : activeIsCam
      ? `Click landmark #${pairCount + 1} on the camera frame.`
      : `Now click the same landmark on the orthophoto.`;

  const activeSrc = activeIsCam ? refImg(camera) : orthoImg();
  const activeW = activeIsCam ? camRef?.width ?? 1 : ortho?.width ?? 1;
  const activeH = activeIsCam ? camRef?.height ?? 1 : ortho?.height ?? 1;
  const activePoints = activeIsCam ? camPts : orthoPts;
  const activeHighlight = editing ? editing.pair : undefined;

  return (
    <div className="p-6 animate-fade-slide-in">
      <h1 className="font-sans text-3xl font-light text-near-black">Calibration</h1>

      <div className="mt-4 flex flex-wrap gap-2">
        {site.cameras.map((c) => (
          <Chip
            key={c}
            active={c === camera}
            onClick={() => {
              setCamera(c);
              setStatus(null);
            }}
          >
            {c}
          </Chip>
        ))}
      </div>

      <div className="font-mono text-[11px] mt-2">
        {entry ? (
          <span className="text-accent">
            ✓ {camera} calibrated — {entry.n_points} pts, reproj{" "}
            {entry.reproj_error.toFixed(1)} px, saved {entry.saved_at}
          </span>
        ) : (
          <span className="text-gray-tertiary">
            ○ {camera} not calibrated — add ≥4 matched pairs
          </span>
        )}
      </div>

      {!ortho ? (
        <Card className="mt-4">
          <div className="text-gray-mid font-sans">No orthophoto configured.</div>
          <div className="text-[11px] font-mono text-gray-tertiary mt-2">
            Set paths.orthophoto in the site config to enable calibration.
          </div>
        </Card>
      ) : (
        <>
          <p className="text-[13px] text-text mt-4 max-w-3xl">
            Pick points on the <strong>ground plane only</strong> — panel-post
            bases, barn corners, curb edges, the tank base. The homography assumes
            flat ground, so rooftops, cow backs, or anything raised will distort
            the fit. Use ≥4 well-spread pairs, and scroll to zoom in for precise
            placement.
          </p>

          {/* Guided placement: one image at a time, with a reference crop of the
              matching point on the other image. */}
          <div className="mt-4 grid grid-cols-1 lg:grid-cols-[1fr_240px] gap-4">
            <ImageClicker
              title={activeIsCam ? "Camera frame" : "Orthophoto"}
              src={activeSrc}
              naturalWidth={activeW}
              naturalHeight={activeH}
              points={activePoints}
              activeIndex={activeHighlight}
              onPlace={handlePlace}
            />

            <div className="flex flex-col gap-3">
              <div
                className={
                  "px-3 py-2 border font-mono text-[12px] " +
                  (editing
                    ? "border-[#e76f51] text-[#e76f51]"
                    : "border-accent text-accent")
                }
              >
                {banner}
              </div>

              {refCrop ? (
                <div>
                  <div className="mb-1">
                    <SectionLabel>{refCrop.label}</SectionLabel>
                  </div>
                  <PointCrop
                    src={refCrop.src}
                    naturalWidth={refCrop.w}
                    naturalHeight={refCrop.h}
                    point={refCrop.pt}
                    size={200}
                    windowFrac={0.22}
                  />
                </div>
              ) : null}

              {editing ? (
                <Button variant="ghost" onClick={() => setEditing(null)}>
                  Cancel edit (Esc)
                </Button>
              ) : null}

              <div className="font-mono text-[11px] text-gray-tertiary">
                {pairCount} complete pair{pairCount === 1 ? "" : "s"}
                {pendingCam ? " · 1 awaiting orthophoto" : ""}
              </div>
            </div>
          </div>

          {/* Pairs list — cropped tiles for both images; click a tile to edit,
              trash to delete the pair. */}
          {rowCount > 0 ? (
            <div className="mt-6">
              <div className="mb-2">
                <SectionLabel>Matched points</SectionLabel>
              </div>
              <div className="flex flex-col gap-2">
                {Array.from({ length: rowCount }).map((_, i) => {
                  const cam = camPts[i];
                  const orth = orthoPts[i];
                  const complete = cam && orth;
                  return (
                    <div
                      key={i}
                      className="flex items-center gap-3 bg-surface border border-border px-3 py-2"
                    >
                      <span className="font-mono text-[13px] text-near-black w-6 text-center">
                        {i + 1}
                      </span>
                      <div className="flex flex-col items-center gap-1">
                        <PointCrop
                          src={refImg(camera)}
                          naturalWidth={camRef?.width ?? 1}
                          naturalHeight={camRef?.height ?? 1}
                          point={cam}
                          onClick={cam ? () => setEditing({ pair: i, which: "cam" }) : undefined}
                          title="Edit camera point"
                        />
                        <span className="font-mono text-[9px] text-gray-tertiary">cam</span>
                      </div>
                      <div className="flex flex-col items-center gap-1">
                        <PointCrop
                          src={orthoImg()}
                          naturalWidth={ortho.width}
                          naturalHeight={ortho.height}
                          point={orth}
                          onClick={orth ? () => setEditing({ pair: i, which: "ortho" }) : undefined}
                          title="Edit orthophoto point"
                        />
                        <span className="font-mono text-[9px] text-gray-tertiary">ortho</span>
                      </div>
                      <span
                        className={
                          "font-mono text-[10px] " +
                          (complete ? "text-accent" : "text-gray-tertiary")
                        }
                      >
                        {complete ? "matched" : "awaiting orthophoto"}
                      </span>
                      <div className="ml-auto flex gap-2">
                        <button
                          onClick={() => deletePair(i)}
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

          <div className="mt-6 flex flex-wrap gap-3 items-center">
            <Button
              variant="primary"
              disabled={
                camPts.length < 4 ||
                camPts.length !== orthoPts.length ||
                !!editing ||
                saving
              }
              onClick={computeAndSave}
            >
              {saving ? "Computing…" : "Compute & save"}
            </Button>
            <Button variant="ghost" onClick={undoLast}>
              Undo last
            </Button>
            <Button variant="ghost" onClick={clearAll}>
              Clear
            </Button>
          </div>

          {status ? (
            <div
              className={
                "mt-3 px-3 py-2 border font-mono text-[12px] " +
                (status.ok
                  ? "border-accent text-accent"
                  : "border-[#e76f51] text-[#e76f51]")
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
