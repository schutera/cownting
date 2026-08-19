import {
  BrowserRouter,
  Routes,
  Route,
  NavLink,
  Navigate,
  useLocation,
} from "react-router-dom";
import type { ReactNode } from "react";
import Dashboard from "./pages/Dashboard";
import CountArea from "./pages/CountArea";
import DataOverview from "./pages/DataOverview";
import CameraManage from "./pages/CameraManage";
import Manual from "./pages/Manual";
import Admin from "./pages/Admin";
import { TimelineProvider, useTimeline } from "./lib/timeline";
import { DatasetProvider, useDataset } from "./lib/dataset";
import { AuthProvider, useAuth, canManageData } from "./lib/auth";
import { TimeScrubber } from "./components/TimeScrubber";
import { Chip } from "./components/ui";

function navClass({ isActive }: { isActive: boolean }): string {
  return (
    "font-mono text-[11px] uppercase tracking-[0.16em] px-2 " +
    (isActive ? "text-accent" : "text-gray-tertiary hover:text-text")
  );
}

/**
 * Bug-report link: opens the user's mail client with a prefilled report that
 * nudges them to describe what they did, what went wrong, and to attach a
 * screenshot. The current page URL is baked in so we know where it broke.
 */
function BugReportButton() {
  const to = "schutera@dhbw-ravensburg.de";
  const subject = "Cownting bug report";
  const body = [
    "Thanks for helping improve Cownting! Please fill in the details below.",
    "",
    "What I was doing:",
    "(e.g. which page/dataset, what I clicked)",
    "",
    "What went wrong:",
    "(what you expected vs. what actually happened)",
    "",
    "Steps to reproduce:",
    "1. ",
    "2. ",
    "3. ",
    "",
    "Screenshot: (please attach one — it helps a lot)",
    "",
    "---",
    `Page: ${typeof window !== "undefined" ? window.location.href : ""}`,
    `Browser: ${typeof navigator !== "undefined" ? navigator.userAgent : ""}`,
  ].join("\n");
  const href = `mailto:${to}?subject=${encodeURIComponent(
    subject,
  )}&body=${encodeURIComponent(body)}`;
  return (
    <a
      href={href}
      className="font-mono text-[11px] uppercase tracking-[0.16em] px-2 text-gray-tertiary hover:text-accent"
      title="Report a bug via email"
    >
      Report bug
    </a>
  );
}

/**
 * Day scrubber that rides in the sticky header, so the time of day can be
 * dragged from anywhere on the dashboard. Hidden on the count-area editor
 * (no timeline there) and until the timeline has loaded.
 */
function HeaderScrubber() {
  const { pathname } = useLocation();
  const { timeline, frame, setFrame } = useTimeline();
  if (pathname !== "/" || !timeline || frame === null) return null;
  return <TimeScrubber timeline={timeline} frame={frame} onFrame={setFrame} />;
}

/**
 * Signed-in identity + logout in the header. Hidden when auth is disabled.
 * Uses the same "label + pill" layout the day selector used to carry (the
 * username as a static pill, logout as a Chip), so the header reads as one
 * consistent styled block now that the Day picker has moved to the dashboard.
 *
 * While an admin is previewing a lower role (Users page → "Experience as"),
 * the pill turns amber and a "Back to admin" chip appears HERE — the header is
 * the one place still reachable when the preview locks you out of the Users
 * page itself.
 */
function UserMenu() {
  const { user, logout, actAs } = useAuth();
  if (!user || user.auth_disabled) return null;
  const acting = user.real_role === "admin" && user.role !== "admin";
  return (
    <div className="flex items-center gap-1.5 ml-6 sm:ml-8">
      <span
        className={
          "text-[13px] px-3 py-1.5 rounded-full border font-medium " +
          (acting
            ? "bg-amber-50 text-amber-700 border-amber-600/40"
            : "bg-green-50 text-green-700 border-green-600/40")
        }
        title={acting ? `Signed in as ${user.username} (admin), previewing the ${user.role} role` : undefined}
      >
        {user.username}
        {acting ? ` · as ${user.role}` : ""}
      </span>
      {acting ? <Chip onClick={() => actAs("admin")}>Back to admin</Chip> : null}
      <Chip onClick={() => logout()}>Logout</Chip>
    </div>
  );
}

/** Route wrapper that only lets admins through; everyone else bounces home. */
function AdminOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  if (!user || (user.role !== "admin" && !user.auth_disabled)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

/** Route wrapper for data-management pages (e.g. the camera manager): powerusers
 *  and admins only; plain viewers bounce home. */
function PowerUserOnly({ children }: { children: ReactNode }) {
  const { user } = useAuth();
  if (!user || !canManageData(user)) return <Navigate to="/" replace />;
  return <>{children}</>;
}

function AppInner() {
  // Remount the timeline + routed content whenever the day changes, so the
  // scrubber and every dashboard fetch pick up the newly-selected dataset.
  const { dataset } = useDataset();
  const { user } = useAuth();
  const isAdmin = !!user && (user.role === "admin" || !!user.auth_disabled);
  return (
    <TimelineProvider key={dataset ?? "whole-db"}>
      {/* Fixed height (40px logo + py-4 + 1px border) is mirrored by the
          --app-header-h token in index.css, which page-level sticky toolbars
          offset by. Change one, change the other. */}
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
          <nav className="flex items-center gap-5">
            <NavLink to="/" end className={navClass}>
              Dashboard
            </NavLink>
            <NavLink to="/data" className={navClass}>
              Data
            </NavLink>
            <NavLink to="/manual" className={navClass}>
              Manual
            </NavLink>
            {isAdmin ? (
              <NavLink to="/admin" className={navClass}>
                Users
              </NavLink>
            ) : null}
            <BugReportButton />
            <UserMenu />
          </nav>
        </div>
        <HeaderScrubber />
      </header>
      <main className="max-w-[1400px] mx-auto px-6 sm:px-10 py-10 sm:py-12">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/data" element={<DataOverview />} />
          <Route
            path="/data/:dataset/cameras"
            element={
              <PowerUserOnly>
                <CameraManage />
              </PowerUserOnly>
            }
          />
          <Route path="/manual" element={<Manual />} />
          {/* Editing areas is a poweruser action (saves are require_poweruser
              server-side) — gate the whole editor so a viewer can't draw for ten
              minutes and only hit the 403 at Save. */}
          <Route
            path="/count-area/:dataset/:camera"
            element={
              <PowerUserOnly>
                <CountArea />
              </PowerUserOnly>
            }
          />
          <Route
            path="/admin"
            element={
              <AdminOnly>
                <Admin />
              </AdminOnly>
            }
          />
        </Routes>
      </main>
    </TimelineProvider>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <DatasetProvider>
          <AppInner />
        </DatasetProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
