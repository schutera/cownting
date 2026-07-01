import { useEffect, useState } from "react";
import type { FrameRow } from "../lib/types";
import { getFrames, frameImg } from "../lib/api";
import { Card } from "../components/ui";

export default function SegmentationReview({ camera }: { camera: string }) {
  const [frames, setFrames] = useState<FrameRow[]>([]);
  const [idx, setIdx] = useState(0);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setError(null);
    getFrames(camera)
      .then((rows) => {
        if (!alive) return;
        setFrames(rows);
        setIdx(0);
      })
      .catch((e: unknown) => {
        if (alive) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      alive = false;
    };
  }, [camera]);

  if (error) {
    return (
      <p className="text-gray-tertiary font-mono text-[11px] mt-4">
        Couldn't load frames — {error}
      </p>
    );
  }

  if (frames.length === 0) {
    return (
      <p className="text-gray-tertiary font-mono text-[11px] mt-4">
        No frames available for {camera}.
      </p>
    );
  }

  const current = frames[Math.min(idx, frames.length - 1)];

  return (
    <div className="mt-4">
      <Card className="p-0">
        <img
          src={frameImg(camera, current.frame_idx, "overlay")}
          className="w-full block border border-border"
          alt={`${camera} frame ${current.frame_idx}`}
        />
      </Card>
      <input
        type="range"
        min={0}
        max={frames.length - 1}
        value={Math.min(idx, frames.length - 1)}
        onChange={(e) => setIdx(Number(e.target.value))}
        className="w-full accent-accent mt-4"
      />
      <div className="font-mono text-[11px] text-gray-tertiary mt-2">
        {camera} · frame {current.frame_idx} · {current.ts}
      </div>
    </div>
  );
}
