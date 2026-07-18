import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { useDataset } from "../lib/dataset";
import { useAuth, canManageData } from "../lib/auth";
import type { DatasetRow } from "../lib/types";
import { Button, SectionLabel } from "../components/ui";
import { UploadPanel } from "../components/UploadPanel";
import { deleteDataset, exportCsvUrl } from "../lib/api";

// The confirmation phrase the user must type to delete a day: its capture date as
// ddmmyy (e.g. 2026-07-03 -> "030726"). Falls back to the dataset id when a day
// was never recorded. Kept in sync with the backend's expected value.
function confirmPhrase(row: DatasetRow): string {
  const iso = row.day?.slice(0, 10);
  const m = iso?.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  return m ? `${m[3]}${m[2]}${m[1].slice(2)}` : row.dataset_id;
}

/**
 * Data page: upload a new day of footage (UploadPanel) up top, then the gallery
 * of existing days below. Picking a day activates it app-wide and drops back to
 * the dashboard. Laid out as one flat page divided by a hairline rule — no nested
 * cards; the drop tiles and day cards are the only surfaces.
 */
export default function DataOverview() {
  const { datasets, dataset, setDataset, refresh, loaded } = useDataset();
  const { user } = useAuth();
  const canManage = canManageData(user);
  const navigate = useNavigate();
  const [pendingDelete, setPendingDelete] = useState<DatasetRow | null>(null);

  function pick(id: string) {
    setDataset(id);
    navigate("/"); // remounts the dashboard subtree against the chosen day
  }

  // After a day is archived: drop it from the list and, if it was the active day,
  // re-point to whatever remains (newest first) so the dashboard isn't left on a
  // now-empty package.
  async function afterDeleted(deletedId: string) {
    const rows = await refresh();
    if (dataset === deletedId && rows.length) setDataset(rows[0].dataset_id);
    setPendingDelete(null);
  }

  return (
    <div className="flex flex-col gap-12 animate-fade-slide-in">
      <header>
        <SectionLabel>DATA</SectionLabel>
        <h1 className="font-display text-3xl sm:text-4xl font-light text-near-black leading-tight mt-1">
          Your footage
        </h1>
        <p className="text-gray-mid text-sm mt-2 max-w-xl">
          Every day of footage is one data package.{" "}
          {canManage
            ? "Upload a new one, or pick an existing day to explore it on the dashboard."
            : "Pick a day to explore it on the dashboard."}
        </p>
      </header>

      {canManage ? <UploadPanel /> : null}

      <div className="h-px bg-border" />

      <section>
        <div className="flex items-baseline gap-3">
          <SectionLabel>AVAILABLE DAYS</SectionLabel>
          {loaded && datasets.length ? (
            <span className="text-[12px] text-gray-tertiary tabular-nums">{datasets.length}</span>
          ) : null}
          {loaded && datasets.length && canManage ? (
            <a
              href={exportCsvUrl()}
              download
              title="Download the whole database as CSV (one row per detection)"
              className="ml-auto font-mono text-[11px] uppercase tracking-[0.16em] text-gray-tertiary hover:text-accent transition-colors"
            >
              ⬇ Download CSV
            </a>
          ) : null}
        </div>

        {!loaded ? (
          <p className="text-gray-tertiary font-mono text-sm mt-4">Loading…</p>
        ) : datasets.length === 0 ? (
          <p className="text-gray-tertiary text-sm mt-4 max-w-xl">
            No days yet — upload your first above to get started.
          </p>
        ) : (
          <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4 mt-5">
            {datasets.map((d) => (
              <DayCard
                key={d.dataset_id}
                row={d}
                active={dataset === d.dataset_id}
                canManage={canManage}
                onClick={() => pick(d.dataset_id)}
                onDelete={() => setPendingDelete(d)}
              />
            ))}
          </div>
        )}
      </section>

      {pendingDelete ? (
        <DeleteModal
          row={pendingDelete}
          onClose={() => setPendingDelete(null)}
          onDeleted={() => afterDeleted(pendingDelete.dataset_id)}
        />
      ) : null}
    </div>
  );
}

/**
 * Type-to-confirm delete dialog. The day is not destroyed — it is moved into the
 * archive DB — but it does leave the dashboard, so we gate it behind typing the
 * capture date in ddmmyy. The Delete button unlocks only on an exact match.
 */
function DeleteModal({
  row,
  onClose,
  onDeleted,
}: {
  row: DatasetRow;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const phrase = confirmPhrase(row);
  const title = row.label ?? row.day ?? row.dataset_id;
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const match = typed.trim() === phrase;

  // Close on Escape for keyboard users.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !busy) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  async function confirm() {
    if (!match || busy) return;
    setBusy(true);
    setError(null);
    try {
      await deleteDataset(row.dataset_id, phrase);
      onDeleted();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <div
      className="fixed inset-0 z-[100] grid place-items-center px-4"
      onClick={busy ? undefined : onClose}
    >
      <div
        className="w-full max-w-md bg-surface border border-border rounded-2xl shadow-xl p-6 animate-fade-slide-in"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-3">
          <span className="grid place-items-center w-10 h-10 rounded-full bg-accent-soft text-xl">🗑</span>
          <div>
            <h3 className="font-display text-xl text-near-black leading-tight">Delete this day?</h3>
            <p className="text-[12px] text-gray-tertiary mt-0.5">
              <span className="text-near-black">{title}</span> · {row.n_frames.toLocaleString()} frames ·{" "}
              {row.n_detections.toLocaleString()} cows
            </p>
          </div>
        </div>

        <p className="text-sm text-gray-mid mt-4">
          This deletes the day and all of its data — every frame and detection is
          removed and this cannot be undone. To confirm, type the capture date{" "}
          <span className="font-mono text-near-black">ddmmyy</span>:
        </p>

        <div className="mt-2 flex items-baseline gap-2">
          <span className="text-[13px] text-gray-tertiary">Expected</span>
          <code className="font-mono text-near-black tracking-[0.15em]">{phrase}</code>
        </div>

        <input
          autoFocus
          value={typed}
          disabled={busy}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") confirm();
          }}
          placeholder="ddmmyy"
          inputMode="numeric"
          className={
            "mt-3 w-full bg-surface-sunk border rounded-xl px-3.5 py-2.5 font-mono tracking-[0.15em] outline-none transition-colors " +
            (typed.length === 0
              ? "border-border focus:border-accent"
              : match
                ? "border-accent text-near-black"
                : "border-warn text-accent-deep")
          }
        />

        {error ? (
          <p className="mt-3 text-sm text-accent-deep bg-accent-soft border border-accent/30 rounded-xl px-3.5 py-2.5">
            {error}
          </p>
        ) : null}

        <div className="mt-5 flex items-center justify-end gap-3">
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={confirm} disabled={!match || busy}>
            {busy ? "Deleting…" : "Delete day"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function DayCard({
  row,
  active,
  canManage,
  onClick,
  onDelete,
}: {
  row: DatasetRow;
  active: boolean;
  canManage: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  const title = row.label ?? row.day ?? row.dataset_id;
  // The pick surface and the trash affordance are SIBLINGS (not nested buttons):
  // the card is one full-size button; the bin is a second button pinned to the
  // top-right corner. The status dot moves under the title so the corner is free
  // for an always-visible (not hover-only) delete control.
  return (
    <div className="group relative">
      <button
        onClick={onClick}
        className={
          "w-full text-left bg-surface border rounded-2xl p-5 flex flex-col min-h-[140px] transition-colors duration-150 " +
          (active ? "border-accent" : "border-border hover:border-accent")
        }
      >
        <div className={"min-w-0 " + (canManage ? "pr-16" : "")}>
          <div className="font-display text-2xl text-near-black leading-none truncate">{title}</div>
          <div className="mt-2 flex items-center gap-2">
            {row.day && row.label ? (
              <span className="font-mono text-[11px] text-gray-tertiary">{row.day.slice(0, 10)}</span>
            ) : null}
            <StatusDot status={row.status} />
          </div>
        </div>

        <div className="mt-auto pt-4 flex items-center gap-2 text-[12px] text-gray-mid tabular-nums">
          <Metric value={row.n_frames} label="frames" />
          <Dot />
          <Metric value={row.n_detections} label="cows" />
          <Dot />
          <Metric value={row.n_cameras} label="cams" />
          <span
            className={
              "ml-auto text-[11px] font-mono uppercase tracking-[0.16em] " +
              (active ? "text-accent" : "text-gray-tertiary group-hover:text-accent-deep transition-colors")
            }
          >
            {active ? "Active" : "Open →"}
          </span>
        </div>
      </button>

      {/* Corner controls: per-day CSV download + archive. Siblings of the pick
          button (not nested), so clicking either does not activate the day. Only
          powerusers/admins can download or delete data, so view-only users get a
          clean card with no corner affordances. */}
      {canManage ? (
        <div className="absolute top-3.5 right-3.5 flex items-center gap-0.5">
          <a
            href={exportCsvUrl(row.dataset_id)}
            download
            aria-label={`download ${title} as CSV`}
            title="Download this day as CSV"
            onClick={(e) => e.stopPropagation()}
            className="w-7 h-7 grid place-items-center rounded-full text-[13px] text-gray-tertiary opacity-70 hover:opacity-100 hover:bg-accent-soft hover:text-accent-deep transition-all duration-150"
          >
            ⬇
          </a>
          <button
            onClick={onDelete}
            aria-label={`delete ${title}`}
            title="Delete this day"
            className="w-7 h-7 grid place-items-center rounded-full text-[13px] text-gray-tertiary opacity-70 hover:opacity-100 hover:bg-accent-soft hover:text-accent-deep transition-all duration-150"
          >
            🗑
          </button>
        </div>
      ) : null}
    </div>
  );
}

function Metric({ value, label }: { value: number; label: string }) {
  return (
    <span>
      <span className="text-near-black font-medium">{value.toLocaleString()}</span> {label}
    </span>
  );
}

function Dot() {
  return <span className="text-border">·</span>;
}

function StatusDot({ status }: { status: string }) {
  // 'localized' = fully processed (sage); anything mid-pipeline is amber.
  const done = status === "localized";
  return (
    <span className="flex items-center gap-1.5 text-[11px] font-mono text-gray-tertiary shrink-0">
      <span className={"inline-block w-2 h-2 rounded-full " + (done ? "bg-accent" : "bg-warn")} />
      {status}
    </span>
  );
}
