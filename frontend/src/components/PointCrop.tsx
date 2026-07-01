import type { CSSProperties } from "react";

/**
 * A small magnified tile of `src` centered on `point` (in natural pixels).
 * Shows a fixed-size window of the image around the point with a crosshair,
 * so a calibration pair can be reviewed without opening the full viewer.
 */
export function PointCrop({
  src,
  naturalWidth,
  naturalHeight,
  point,
  size = 72,
  windowFrac = 0.14,
  onClick,
  title,
}: {
  src: string;
  naturalWidth: number;
  naturalHeight: number;
  point?: number[] | null;
  size?: number;
  windowFrac?: number; // fraction of image width shown in the tile
  onClick?: () => void;
  title?: string;
}) {
  const base: CSSProperties = {
    width: size,
    height: size,
    flex: "0 0 auto",
  };

  if (!point) {
    return (
      <div
        className="border border-dashed border-border bg-surface flex items-center justify-center font-mono text-[9px] text-gray-tertiary"
        style={base}
        title={title}
      >
        —
      </div>
    );
  }

  // displayed px per natural px so the window spans `size` on screen
  const cropWindow = Math.max(1, naturalWidth * windowFrac);
  const z = size / cropWindow;
  const style: CSSProperties = {
    ...base,
    backgroundImage: `url(${src})`,
    backgroundRepeat: "no-repeat",
    backgroundSize: `${naturalWidth * z}px ${naturalHeight * z}px`,
    backgroundPosition: `${size / 2 - point[0] * z}px ${size / 2 - point[1] * z}px`,
    cursor: onClick ? "pointer" : "default",
  };

  return (
    <div
      className="relative border border-border hover:border-accent transition-colors"
      style={style}
      onClick={onClick}
      title={title}
    >
      {/* crosshair at tile center */}
      <div className="absolute inset-0 pointer-events-none">
        <div
          className="absolute bg-white/70"
          style={{ left: "50%", top: 0, width: 1, height: "100%", transform: "translateX(-0.5px)" }}
        />
        <div
          className="absolute bg-white/70"
          style={{ top: "50%", left: 0, height: 1, width: "100%", transform: "translateY(-0.5px)" }}
        />
        <div
          className="absolute"
          style={{
            left: "50%",
            top: "50%",
            width: 8,
            height: 8,
            transform: "translate(-50%, -50%)",
            border: "2px solid #e76f51",
            borderRadius: "9999px",
          }}
        />
      </div>
    </div>
  );
}
