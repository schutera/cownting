import { useState } from "react";
import { Card, Button, SectionLabel } from "./ui";
import { runJointCalibration } from "../lib/api";
import type { JointResult } from "../lib/api";

const WARN = "#e76f51";
const GOOD_PX = 5;

export function JointCalibratePanel({
  cameras,
  onDone,
}: {
  cameras: string[];
  onDone?: () => void;
}) {
  const [pending, setPending] = useState(false);
  const [result, setResult] = useState<JointResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run() {
    setPending(true);
    setError(null);
    try {
      const r = await runJointCalibration();
      setResult(r);
      onDone?.();
    } catch (e) {
      setError(String(e));
    } finally {
      setPending(false);
    }
  }

  const rows = result ? Object.entries(result.per_camera) : [];

  return (
    <Card className="mt-4 p-5">
      <SectionLabel>Joint calibration</SectionLabel>
      <p className="text-[13px] text-text mt-2 max-w-3xl">
        Joint calibration re-fits <strong>all {cameras.length} cameras together</strong>,
        tying them through shared fence corners and <strong>cross-camera tie points</strong>
        so overlapping views agree in the orthophoto. Run it after per-camera calibration,
        the fence, and any shared points (cross-camera tab).
      </p>

      <div className="mt-4">
        <Button variant="primary" disabled={pending} onClick={run}>
          {pending ? "Solving…" : "Joint calibrate all cameras"}
        </Button>
      </div>

      {result ? (
        <div className="mt-4 flex flex-col gap-4">
          <div className="border border-accent px-4 py-3 font-mono text-[12px] text-accent">
            <div className="text-[11px] tracking-[0.02em] mb-1">Global agreement</div>
            <div>
              Cross-camera agreement{" "}
              <strong>{result.global.cross_camera_px.toFixed(1)} px</strong> mean (max{" "}
              {result.global.max_cross_camera_px.toFixed(1)} px)
            </div>
            <div>
              {result.global.n_shared_corners} shared points (fence corners + tie points)
              across {result.global.n_pairs} camera pairs
            </div>
            <div>{result.updated} detections re-localized</div>
          </div>

          {rows.length ? (
            <div className="overflow-x-auto">
              <table className="font-mono text-[12px] border-collapse">
                <thead>
                  <tr className="text-gray-tertiary text-left">
                    <th className="pr-6 pb-1 font-medium">camera</th>
                    <th className="pr-6 pb-1 font-medium">method</th>
                    <th className="pr-6 pb-1 font-medium">reproj (px)</th>
                    <th className="pr-6 pb-1 font-medium">shared</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map(([cam, d]) => (
                    <tr key={cam} className="border-t border-border">
                      <td className="pr-6 py-1 text-text">{cam}</td>
                      <td className="pr-6 py-1 text-gray-mid">{d.method}</td>
                      <td
                        className="pr-6 py-1"
                        style={{ color: d.reproj_error <= GOOD_PX ? undefined : WARN }}
                      >
                        <span className={d.reproj_error <= GOOD_PX ? "text-accent" : ""}>
                          {d.reproj_error.toFixed(1)}
                        </span>
                      </td>
                      <td className="pr-6 py-1 text-gray-mid">{d.n_shared}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}

      {error ? (
        <div
          className="mt-4 px-3 py-2 border font-mono text-[12px]"
          style={{ borderColor: WARN, color: WARN }}
        >
          ✗ {error}
        </div>
      ) : null}
    </Card>
  );
}
