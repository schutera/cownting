import { Link } from "react-router-dom";

export default function CalibrationNudge({
  calibrated,
  camera,
}: {
  calibrated: boolean;
  camera: string;
}) {
  if (calibrated) return null;
  return (
    <div
      className="bg-surface border border-border px-4 py-3 mt-6 flex items-center gap-4 animate-fade-slide-in"
      style={{ borderLeft: "3px solid #e76f51" }}
    >
      <span className="text-gray-mid font-sans flex-1">
        ⚠ {camera} isn't calibrated — the heatmap needs it.
      </span>
      <Link
        to="/calibrate"
        className="bg-accent text-white text-sm font-medium px-5 py-3 hover:opacity-90 transition-all duration-150 no-underline"
      >
        Calibrate now
      </Link>
    </div>
  );
}
