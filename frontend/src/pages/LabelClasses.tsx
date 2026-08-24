import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type {
  LabelClass,
  LabelGroup,
  LabelMoveDir,
  Taxonomy,
} from "../lib/types";
import {
  createLabelClass,
  createLabelGroup,
  getTaxonomy,
  moveLabelClass,
  moveLabelGroup,
  updateLabelClass,
  updateLabelGroup,
} from "../lib/api";
import type { LabelGroupKeys } from "../lib/labelKeys";
import { buildKeyMap } from "../lib/labelKeys";
import { Button, Card, INPUT_CLS, Kbd, SectionLabel } from "../components/ui";

/* The poweruser taxonomy editor (docs/roadmap/M3_labeling.md §5.7). Route-gated
 * by PowerUserOnly in App.tsx; it lives on its own route rather than as a Label
 * page toggle because a text input mounted in the Label tree would steal
 * 1/Q/S/Enter from the hotkey layer.
 *
 * Every mutation returns the WHOLE taxonomy (the Admin.tsx idiom), so there is
 * no optimistic state to reconcile and the revision on screen is always the
 * server's. Archiving is PATCH {active: false} and restoring {active: true} —
 * there are no delete routes anywhere in this feature, because a hard delete
 * would orphan every stored answer.
 *
 * A 409 taxonomy_stale (another poweruser edited concurrently) refetches the
 * taxonomy but never discards the user's edit: drafts live in the form
 * components and only clear on a SUCCESSFUL save, so after the refresh the
 * user re-checks their edit against the new state and saves again.
 */

/* Sorting mirrors labelKeys.buildKeyMap's byOrder (not exported from there):
   ties on sort_order are real — everything a poweruser adds defaults to 100 —
   so the immutable key is the tiebreak, or the rows would reshuffle between
   reloads. The editor MUST show the same order the annotator's hotkeys use. */
function ordered<T extends { sort_order: number }>(rows: T[], key: (row: T) => string): T[] {
  return [...rows].sort((a, b) => {
    if (a.sort_order !== b.sort_order) return a.sort_order - b.sort_order;
    const ka = key(a);
    const kb = key(b);
    return ka < kb ? -1 : ka > kb ? 1 : 0;
  });
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/* Whether a mutation failure looks like the 409 taxonomy_stale conflict. jRaw
   flattens the error body to a message, so this is a string sniff by necessity:
   an object detail stringifies to "[object Object]", a bare 409 keeps its
   status line. False positives merely cause an extra refetch, which is safe. */
function isStaleMessage(msg: string): boolean {
  return msg.includes("taxonomy_stale") || msg.includes("409") || msg === "[object Object]";
}

/* Server-side slug rule for a new group_key, mirrored as a hint (never a hard
   block — the server is the source of truth). A dot is what makes a class_key
   ('group.slug'); a group_key must never contain one. */
const GROUP_KEY_RE = /^[a-z][a-z0-9_]*$/;

type Run = (p: Promise<Taxonomy>) => Promise<boolean>;

export default function LabelClasses() {
  const [taxonomy, setTaxonomy] = useState<Taxonomy | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    getTaxonomy()
      .then(setTaxonomy)
      .catch((e: unknown) => setError(errMsg(e)));
  }, []);

  /* Every mutation funnels through here. Returns true on success so the form
     that fired it knows to clear its draft — on ANY failure the draft stays. */
  const run: Run = async (p) => {
    if (busy) return false;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setTaxonomy(await p);
      setBusy(false);
      return true;
    } catch (e) {
      const msg = errMsg(e);
      if (isStaleMessage(msg)) {
        // Refetch, don't discard: the list below refreshes to the state that
        // beat us, the draft stays in its form for the user to re-check.
        try {
          setTaxonomy(await getTaxonomy());
        } catch {
          /* keep the last known taxonomy on screen */
        }
        setNotice(
          "Someone else changed the taxonomy at the same time — the list below has been " +
            "refreshed. Your edit is still in its form: check it against the new state and save again.",
        );
      } else {
        setError(msg);
      }
      setBusy(false);
      return false;
    }
  };

  const groups = useMemo(() => taxonomy?.groups ?? [], [taxonomy]);
  // The live key preview — the same derivation the Label page's hotkeys use, so
  // what the editor promises is what the annotator's hands get (§5.5, §5.7).
  const keys = useMemo(() => buildKeyMap(groups), [groups]);
  const activeGroups = useMemo(() => ordered(groups.filter((g) => g.active), (g) => g.group_key), [groups]);
  const archivedGroups = useMemo(
    () => ordered(groups.filter((g) => !g.active), (g) => g.group_key),
    [groups],
  );

  if (error !== null && taxonomy === null) {
    return (
      <p className="text-gray-tertiary font-mono text-sm">Couldn't load the taxonomy — {error}</p>
    );
  }
  if (taxonomy === null) {
    return <div className="animate-shimmer h-64 bg-surface border border-border rounded-2xl" />;
  }

  return (
    <div className="flex flex-col gap-6 max-w-3xl animate-fade-slide-in">
      <header>
        <div className="flex items-end justify-between gap-4 flex-wrap">
          <div>
            <SectionLabel>LABEL</SectionLabel>
            <h1 className="font-display text-3xl sm:text-4xl font-light text-near-black leading-tight mt-1">
              Label classes
            </h1>
          </div>
          <Link
            to="/label"
            className="font-mono text-[11px] uppercase tracking-[0.16em] text-gray-tertiary hover:text-accent transition-colors"
          >
            ← Back to labeling
          </Link>
        </div>
        <p className="text-gray-mid text-sm mt-2 max-w-xl">
          A <span className="text-text">group</span> is a question, a{" "}
          <span className="text-text">class</span> is an answer inside it. Nothing here is ever
          deleted: archiving hides a question or answer from annotators, but every answer already
          recorded against it stays and still counts — restore brings it back. Reordering moves the
          keys annotators' hands have learned, so each row previews its live binding.
        </p>
        <p className="font-mono text-[11px] text-gray-tertiary mt-2">
          taxonomy revision {taxonomy.revision}
        </p>
      </header>

      {notice !== null ? (
        <p role="alert" className="text-[13px] text-warn">
          {notice}
        </p>
      ) : null}
      {error !== null ? <p className="text-[13px] text-danger">{error}</p> : null}

      <AddGroupForm busy={busy} run={run} />

      <div className="flex flex-col gap-4">
        {activeGroups.map((g, i) => (
          <GroupCard
            key={g.group_key}
            group={g}
            groupKeys={keys.groups.find((k) => k.group_key === g.group_key)}
            isFirst={i === 0}
            isLast={i === activeGroups.length - 1}
            busy={busy}
            run={run}
          />
        ))}
      </div>

      {archivedGroups.length > 0 ? (
        <section>
          <SectionLabel>ARCHIVED QUESTIONS</SectionLabel>
          <div className="flex flex-col gap-2 mt-2">
            {archivedGroups.map((g) => (
              <Card key={g.group_key} className="p-4 opacity-70">
                <div className="flex items-center justify-between gap-3 flex-wrap">
                  <div className="flex items-baseline gap-2.5">
                    <span className="text-text text-sm">{g.name}</span>
                    <span className="font-mono text-[11px] text-gray-tertiary">{g.group_key}</span>
                  </div>
                  <Button
                    variant="ghost"
                    onClick={() => void run(updateLabelGroup(g.group_key, { active: true }))}
                    disabled={busy}
                  >
                    Restore
                  </Button>
                </div>
              </Card>
            ))}
          </div>
        </section>
      ) : null}
    </div>
  );
}

/* ------------------------------------------------------------------ groups */

function GroupCard({
  group,
  groupKeys,
  isFirst,
  isLast,
  busy,
  run,
}: {
  group: LabelGroup;
  /** The group's slice of the live key map — undefined never happens for an
      active group, but the lookup is honest about it. */
  groupKeys: LabelGroupKeys | undefined;
  isFirst: boolean;
  isLast: boolean;
  busy: boolean;
  run: Run;
}) {
  const [editOpen, setEditOpen] = useState(false);
  const activeClasses = ordered(group.classes.filter((c) => c.active), (c) => c.class_key);
  const archivedClasses = ordered(group.classes.filter((c) => !c.active), (c) => c.class_key);
  const keyByClass = new Map((groupKeys?.options ?? []).map((o) => [o.class_key, o.label]));
  // A group past the end of the key ladder gets no bindings; say so plainly
  // rather than pretending (§5.7).
  const pastLadder = groupKeys === undefined || groupKeys.row === null;

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-2.5 min-w-0">
          <span className="font-display text-xl text-near-black leading-none">{group.name}</span>
          <span className="font-mono text-[11px] text-gray-tertiary">{group.group_key}</span>
          {group.multi_select ? (
            <span className="text-[11px] text-gray-tertiary">multi-select</span>
          ) : null}
          {!group.required ? <span className="text-[11px] text-gray-tertiary">optional</span> : null}
        </div>
        <div className="flex items-center gap-1.5">
          <MoveButtons
            what={`question “${group.name}”`}
            disabledUp={busy || isFirst}
            disabledDown={busy || isLast}
            onMove={(dir) => void run(moveLabelGroup(group.group_key, dir))}
          />
          <button
            type="button"
            onClick={() => setEditOpen((o) => !o)}
            className="text-[13px] text-gray-mid hover:text-accent-deep transition-colors px-1.5"
          >
            {editOpen ? "Close" : "Edit"}
          </button>
          <button
            type="button"
            onClick={() => void run(updateLabelGroup(group.group_key, { active: false }))}
            disabled={busy}
            className="text-[13px] text-gray-mid hover:text-danger transition-colors px-1.5"
          >
            Archive
          </button>
        </div>
      </div>

      {group.description !== null && group.description !== "" ? (
        <p className="text-[12px] leading-snug text-gray-mid mt-1.5">{group.description}</p>
      ) : null}
      {pastLadder ? (
        <p className="text-[11px] text-gray-tertiary mt-1.5">
          Past the key ladder — annotators answer this question with the mouse only.
        </p>
      ) : null}

      {editOpen ? (
        <GroupEditForm
          group={group}
          busy={busy}
          run={run}
          onSaved={() => setEditOpen(false)}
        />
      ) : null}

      <div className="flex flex-col gap-2 mt-4">
        {activeClasses.map((c, i) => (
          <ClassRow
            key={c.class_key}
            cls={c}
            keyLabel={keyByClass.get(c.class_key) ?? ""}
            isFirst={i === 0}
            isLast={i === activeClasses.length - 1}
            busy={busy}
            run={run}
          />
        ))}
        {archivedClasses.map((c) => (
          <div
            key={c.class_key}
            className="flex items-center justify-between gap-3 rounded-xl border border-border px-3 py-2 opacity-70"
          >
            <div className="flex items-baseline gap-2.5 min-w-0">
              <span className="text-[13px] text-text">{c.name}</span>
              <span className="font-mono text-[11px] text-gray-tertiary">archived</span>
            </div>
            <Button
              variant="ghost"
              onClick={() => void run(updateLabelClass(c.class_key, { active: true }))}
              disabled={busy}
            >
              Restore
            </Button>
          </div>
        ))}
      </div>

      <AddClassForm groupKey={group.group_key} busy={busy} run={run} />
    </Card>
  );
}

/* Mounted only while open, so its drafts initialise from the CURRENT group and
   survive a failed save; a successful one unmounts it via onSaved. */
function GroupEditForm({
  group,
  busy,
  run,
  onSaved,
}: {
  group: LabelGroup;
  busy: boolean;
  run: Run;
  onSaved: () => void;
}) {
  const [name, setName] = useState(group.name);
  const [desc, setDesc] = useState(group.description ?? "");
  const [multi, setMulti] = useState(group.multi_select);
  const [required, setRequired] = useState(group.required);

  async function save() {
    const ok = await run(
      updateLabelGroup(group.group_key, {
        name: name.trim(),
        description: desc.trim() === "" ? null : desc.trim(),
        multi_select: multi,
        required,
      }),
    );
    if (ok) onSaved();
  }

  return (
    <div className="mt-3 pt-3 border-t border-border flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2 items-start">
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Question name</span>
          <input className={INPUT_CLS + " w-full"} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Description (behind the ⓘ)</span>
          <input className={INPUT_CLS + " w-full"} value={desc} onChange={(e) => setDesc(e.target.value)} />
        </label>
      </div>
      <div className="flex items-center gap-5 flex-wrap">
        <label className="flex items-center gap-2 cursor-pointer text-[12px] text-gray-mid">
          <input
            type="checkbox"
            checked={multi}
            onChange={(e) => setMulti(e.target.checked)}
            className="accent-accent"
          />
          multi-select (pick all that apply)
        </label>
        <label className="flex items-center gap-2 cursor-pointer text-[12px] text-gray-mid">
          <input
            type="checkbox"
            checked={required}
            onChange={(e) => setRequired(e.target.checked)}
            className="accent-accent"
          />
          required to save an answer
        </label>
        <span className="ml-auto">
          <Button variant="ghost" onClick={() => void save()} disabled={busy || name.trim() === ""}>
            Save
          </Button>
        </span>
      </div>
    </div>
  );
}

function AddGroupForm({ busy, run }: { busy: boolean; run: Run }) {
  const [key, setKey] = useState("");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [multi, setMulti] = useState(false);
  const [required, setRequired] = useState(true);

  const keyOk = key.trim() === "" || GROUP_KEY_RE.test(key.trim());

  async function add() {
    const ok = await run(
      createLabelGroup({
        group_key: key.trim(),
        name: name.trim(),
        description: desc.trim() === "" ? null : desc.trim(),
        multi_select: multi,
        required,
      }),
    );
    if (ok) {
      setKey("");
      setName("");
      setDesc("");
      setMulti(false);
      setRequired(true);
    }
  }

  return (
    <Card className="p-5">
      <SectionLabel>Add a question</SectionLabel>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 items-start">
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">
            Key — a permanent slug, e.g. <span className="font-mono">body_condition</span>
          </span>
          <input
            className={INPUT_CLS + " w-full font-mono"}
            value={key}
            onChange={(e) => setKey(e.target.value)}
          />
          {!keyOk ? (
            <span className="text-[11px] text-danger">
              lowercase letters, digits and underscores, starting with a letter
            </span>
          ) : (
            <span className="text-[11px] text-gray-tertiary">
              Immutable once created — it names the question in every stored answer.
            </span>
          )}
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Question name</span>
          <input className={INPUT_CLS + " w-full"} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
      </div>
      <label className="flex flex-col gap-1.5 mt-3">
        <span className="text-[12px] text-gray-tertiary">Description (optional, shown behind the ⓘ)</span>
        <input className={INPUT_CLS + " w-full"} value={desc} onChange={(e) => setDesc(e.target.value)} />
      </label>
      <div className="flex items-center gap-5 flex-wrap mt-3">
        <label className="flex items-center gap-2 cursor-pointer text-[12px] text-gray-mid">
          <input
            type="checkbox"
            checked={multi}
            onChange={(e) => setMulti(e.target.checked)}
            className="accent-accent"
          />
          multi-select
        </label>
        <label className="flex items-center gap-2 cursor-pointer text-[12px] text-gray-mid">
          <input
            type="checkbox"
            checked={required}
            onChange={(e) => setRequired(e.target.checked)}
            className="accent-accent"
          />
          required
        </label>
        <span className="ml-auto">
          <Button
            onClick={() => void add()}
            disabled={busy || key.trim() === "" || name.trim() === "" || !keyOk}
          >
            Add question
          </Button>
        </span>
      </div>
    </Card>
  );
}

/* ----------------------------------------------------------------- classes */

function ClassRow({
  cls,
  keyLabel,
  isFirst,
  isLast,
  busy,
  run,
}: {
  cls: LabelClass;
  /** The live hotkey badge from labelKeys — "" means the row ran out of
      characters and this answer is mouse-only. */
  keyLabel: string;
  isFirst: boolean;
  isLast: boolean;
  busy: boolean;
  run: Run;
}) {
  const [editOpen, setEditOpen] = useState(false);

  return (
    <div className="rounded-xl border border-border px-3 py-2">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-2.5 min-w-0">
          {keyLabel !== "" ? (
            <Kbd>{keyLabel}</Kbd>
          ) : (
            <span
              className="inline-grid place-items-center min-w-6 h-6 text-[11px] text-gray-tertiary"
              title="No key left on this row — mouse only"
            >
              ·
            </span>
          )}
          <span className="text-[13px] text-text">{cls.name}</span>
          {cls.is_escape ? (
            <span className="text-[11px] font-mono uppercase tracking-wider text-gray-tertiary border border-border rounded-full px-2 py-0.5">
              escape
            </span>
          ) : null}
        </div>
        <div className="flex items-center gap-1.5">
          <MoveButtons
            what={`answer “${cls.name}”`}
            disabledUp={busy || isFirst}
            disabledDown={busy || isLast}
            onMove={(dir) => void run(moveLabelClass(cls.class_key, dir))}
          />
          <button
            type="button"
            onClick={() => setEditOpen((o) => !o)}
            className="text-[13px] text-gray-mid hover:text-accent-deep transition-colors px-1.5"
          >
            {editOpen ? "Close" : "Edit"}
          </button>
          <button
            type="button"
            onClick={() => void run(updateLabelClass(cls.class_key, { active: false }))}
            disabled={busy}
            className="text-[13px] text-gray-mid hover:text-danger transition-colors px-1.5"
          >
            Archive
          </button>
        </div>
      </div>
      <p className="text-[12px] leading-snug text-gray-mid mt-1">{cls.description}</p>
      {editOpen ? (
        <ClassEditForm cls={cls} busy={busy} run={run} onSaved={() => setEditOpen(false)} />
      ) : null}
    </div>
  );
}

/* Same mount-while-open draft rule as GroupEditForm. */
function ClassEditForm({
  cls,
  busy,
  run,
  onSaved,
}: {
  cls: LabelClass;
  busy: boolean;
  run: Run;
  onSaved: () => void;
}) {
  const [name, setName] = useState(cls.name);
  const [desc, setDesc] = useState(cls.description);
  const [escape, setEscape] = useState(cls.is_escape);

  async function save() {
    const ok = await run(
      updateLabelClass(cls.class_key, {
        name: name.trim(),
        description: desc.trim(),
        is_escape: escape,
      }),
    );
    if (ok) onSaved();
  }

  return (
    <div className="mt-2.5 pt-2.5 border-t border-border flex flex-col gap-3">
      <div className="grid gap-3 sm:grid-cols-2 items-start">
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Answer name</span>
          <input className={INPUT_CLS + " w-full"} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Definition (required)</span>
          <input className={INPUT_CLS + " w-full"} value={desc} onChange={(e) => setDesc(e.target.value)} />
        </label>
      </div>
      <div className="flex items-center gap-5 flex-wrap">
        <label className="flex items-center gap-2 cursor-pointer text-[12px] text-gray-mid">
          <input
            type="checkbox"
            checked={escape}
            onChange={(e) => setEscape(e.target.checked)}
            className="accent-accent"
          />
          escape hatch (“Cannot tell” — a forced guess is noise)
        </label>
        <span className="ml-auto">
          <Button
            variant="ghost"
            onClick={() => void save()}
            disabled={busy || name.trim() === "" || desc.trim() === ""}
          >
            Save
          </Button>
        </span>
      </div>
    </div>
  );
}

function AddClassForm({ groupKey, busy, run }: { groupKey: string; busy: boolean; run: Run }) {
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [escape, setEscape] = useState(false);

  async function add() {
    const ok = await run(
      createLabelClass(groupKey, { name: name.trim(), description: desc.trim(), is_escape: escape }),
    );
    if (ok) {
      setName("");
      setDesc("");
      setEscape(false);
    }
  }

  return (
    <div className="mt-4 pt-3 border-t border-border">
      <span className="text-[12px] text-gray-tertiary">Add an answer</span>
      <div className="mt-2 grid gap-3 sm:grid-cols-2 items-start">
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Name</span>
          <input className={INPUT_CLS + " w-full"} value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-[12px] text-gray-tertiary">Definition — what should an annotator pick this for?</span>
          <input className={INPUT_CLS + " w-full"} value={desc} onChange={(e) => setDesc(e.target.value)} />
        </label>
      </div>
      <div className="flex items-center gap-5 flex-wrap mt-2.5">
        <label className="flex items-center gap-2 cursor-pointer text-[12px] text-gray-mid">
          <input
            type="checkbox"
            checked={escape}
            onChange={(e) => setEscape(e.target.checked)}
            className="accent-accent"
          />
          escape hatch
        </label>
        <span className="ml-auto">
          {/* Disabled until BOTH name and definition are written (§5.4): an
              option with no definition is the single largest source of
              annotator disagreement. */}
          <Button
            variant="ghost"
            onClick={() => void add()}
            disabled={busy || name.trim() === "" || desc.trim() === ""}
          >
            Add answer
          </Button>
        </span>
      </div>
    </div>
  );
}

/* Up/down, not drag-and-drop: keyboard-reachable, screen-reader announceable,
   dependency-free (§5.7). Edge rows disable the impossible direction. */
function MoveButtons({
  what,
  disabledUp,
  disabledDown,
  onMove,
}: {
  what: string;
  disabledUp: boolean;
  disabledDown: boolean;
  onMove: (dir: LabelMoveDir) => void;
}) {
  const base =
    "w-7 h-7 grid place-items-center rounded-full text-[13px] transition-colors duration-150 ";
  return (
    <span className="flex items-center gap-0.5">
      <button
        type="button"
        aria-label={`move ${what} up`}
        title="Move up (changes the hotkeys annotators see)"
        disabled={disabledUp}
        onClick={() => onMove("up")}
        className={
          base +
          (disabledUp
            ? "text-gray-tertiary opacity-40 cursor-not-allowed"
            : "text-gray-mid hover:bg-accent-soft hover:text-accent-deep")
        }
      >
        ↑
      </button>
      <button
        type="button"
        aria-label={`move ${what} down`}
        title="Move down (changes the hotkeys annotators see)"
        disabled={disabledDown}
        onClick={() => onMove("down")}
        className={
          base +
          (disabledDown
            ? "text-gray-tertiary opacity-40 cursor-not-allowed"
            : "text-gray-mid hover:bg-accent-soft hover:text-accent-deep")
        }
      >
        ↓
      </button>
    </span>
  );
}
