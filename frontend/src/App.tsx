import {
  BrowserRouter,
  Routes,
  Route,
  NavLink,
  useLocation,
} from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import CountArea from "./pages/CountArea";
import { TimelineProvider, useTimeline } from "./lib/timeline";
import { TimeScrubber } from "./components/TimeScrubber";

function navClass({ isActive }: { isActive: boolean }): string {
  return (
    "font-mono text-[11px] uppercase tracking-[0.16em] px-2 " +
    (isActive ? "text-accent" : "text-gray-tertiary hover:text-text")
  );
}

/**
 * Day scrubber that rides in the sticky header, so the time of day can be
 * dragged from anywhere on the dashboard. Hidden on the count-area editor
 * (no timeline there) and until the timeline has loaded.
 */
function HeaderScrubber() {
  const { pathname } = useLocation();
  const { timeline, frame, setFrame, allDay, setAllDay } = useTimeline();
  if (pathname !== "/" || !timeline || frame === null) return null;
  return (
    <TimeScrubber
      timeline={timeline}
      frame={frame}
      onFrame={setFrame}
      allDay={allDay}
      onAllDay={setAllDay}
    />
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <TimelineProvider>
        <header className="sticky top-0 z-50 border-b border-border bg-bg">
          <div className="flex justify-between items-center px-6 sm:px-10 py-4">
            <div className="flex items-center gap-3">
              <div className="w-10 h-10 bg-accent text-white grid place-items-center">🐄</div>
              <div>
                <div className="font-sans text-xl text-near-black leading-none">Cownting</div>
                <div className="text-[11px] font-mono uppercase tracking-[0.18em] text-gray-tertiary mt-1">
                  solar-field herd analytics
                </div>
              </div>
            </div>
            <nav className="flex items-center">
              <NavLink to="/" end className={navClass}>
                Dashboard
              </NavLink>
            </nav>
          </div>
          <HeaderScrubber />
        </header>
        <main className="max-w-[1400px] mx-auto px-6 sm:px-10 py-10 sm:py-12">
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/count-area/:camera" element={<CountArea />} />
          </Routes>
        </main>
      </TimelineProvider>
    </BrowserRouter>
  );
}
