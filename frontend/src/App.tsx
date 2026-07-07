import { BrowserRouter, Routes, Route, NavLink } from "react-router-dom";
import Dashboard from "./pages/Dashboard";
import CountArea from "./pages/CountArea";

function navClass({ isActive }: { isActive: boolean }): string {
  return (
    "font-mono text-[11px] uppercase tracking-[0.16em] px-2 " +
    (isActive ? "text-accent" : "text-gray-tertiary hover:text-text")
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <div className="sticky top-0 z-50 flex justify-between items-center px-6 sm:px-10 py-4 border-b border-border bg-bg">
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
      <main className="max-w-[1400px] mx-auto px-6 sm:px-10 py-10 sm:py-12">
        <Routes>
          <Route path="/" element={<Dashboard />} />
          <Route path="/count-area/:camera" element={<CountArea />} />
        </Routes>
      </main>
    </BrowserRouter>
  );
}
