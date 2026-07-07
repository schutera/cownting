import {
  createContext,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { TimelineData } from "./types";
import { getTimeline } from "./api";

/**
 * Shared day-scrubber state. The scrubber lives in the (sticky) header so it can
 * be dragged from anywhere, while the Dashboard's map/segmentations/series read
 * the same frame — so both sides hang off this one context rather than lifting a
 * pile of props through the router.
 */
type TimelineCtx = {
  timeline: TimelineData | null;
  frame: number | null;
  setFrame: (frameIdx: number) => void;
  allDay: boolean;
  setAllDay: (v: boolean) => void;
};

const Ctx = createContext<TimelineCtx | null>(null);

export function TimelineProvider({ children }: { children: ReactNode }) {
  const [timeline, setTimeline] = useState<TimelineData | null>(null);
  const [frame, setFrame] = useState<number | null>(null);
  const [allDay, setAllDay] = useState(false);

  useEffect(() => {
    let alive = true;
    getTimeline()
      .then((t) => {
        if (!alive) return;
        setTimeline(t);
        // Start mid-day (clearest frames; dawn has lens condensation).
        if (t.frames.length) setFrame(t.frames[Math.floor(t.frames.length / 2)]);
      })
      .catch(() => {
        /* timeline is optional — the dashboard still works without the scrubber */
      });
    return () => {
      alive = false;
    };
  }, []);

  return (
    <Ctx.Provider value={{ timeline, frame, setFrame, allDay, setAllDay }}>
      {children}
    </Ctx.Provider>
  );
}

export function useTimeline(): TimelineCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useTimeline must be used within a TimelineProvider");
  return v;
}
