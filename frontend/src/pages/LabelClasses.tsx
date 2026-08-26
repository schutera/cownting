import { useEffect, useMemo, useState } from "react";
import { Link } from "react-router-dom";
import type {
  LabelClass,
  LabelGroup,
  LabelIconName,
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
import { numberKeysFor, visibleClasses, visibleGroups } from "../lib/labelKeys";
import { Button, Card, INPUT_CLS, Kbd, SectionLabel } from "../components/ui";
import { ClassIcon } from "../components/ClassIcon";

/* The poweruser taxonomy editor (docs/roadmap/M3_labeling.md §5.7). Route-gated
 * by PowerUserOnly in App.tsx; it lives on its own route rather than as a Label
 * page toggle because a text input mounted in the Label tree would steal the
 * digits, the arrows and F from the hotkey layer.
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

/* Ordering for ARCHIVED rows only. Everything an annotator can still see is
   ordered by labelKeys.visibleGroups/visibleClasses instead, so the editor's
   rows and the annotator's digits come out of the same function rather than out
   of two sorts that agree by luck. Ties on sort_order are real — everything a
   poweruser adds defaults to 100 — so the immutable key is the tiebreak, or the
   archived list would reshuffle between reloads. */
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

/* The fixed icon vocabulary as VALUES. types.ts can only hand us LabelIconName
   as a type, and the list an operator picks from has to exist at runtime, so it
   lives here — this editor is the only place in the app that offers a choice.
   It mirrors labels_db.CLASS_ICONS and the server re-validates the name on
   write, so a build that drifts from the backend earns a 400 rather than
   writing a class whose icon nobody can render. Free text is never accepted:
   the icon value is rendered into the DOM, and a poweruser adding a class at
   runtime must not be able to hand us markup. */
const ICON_CHOICES: readonly LabelIconName[] = [
  "shade",
  "sun",
  "eye-off",
  "question",
  "grass",
  "lying",
  "standing",
  "probe",
  "dot",
];

/* `LabelClass.icon` is a bare string because the server may serve a name this
   build has never heard of. The picker cannot offer such a name, so it falls
   back to the neutral dot — and saving that form then WRITES 'dot'. That is the
   deliberate choice: a picker silently holding a value it cannot render is
   harder to explain than an icon that visibly reset to neutral. */
function isIconName(icon: string): icon is LabelIconName {
  return (ICON_CHOICES as readonly string[]).includes(icon);
}
function asIconName(icon: string): LabelIconName {
  return isIconName(icon) ? icon : "dot";
}

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
  // The same derivation the Label page uses, so the order shown here is the
  // order annotators answer in — sun exposure first, behaviour second.
  const activeGroups = useMemo(() => visibleGroups(groups), [groups]);
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
          recorded against it stays and still counts — restore brings it back.
        </p>
        <p className="text-gray-mid text-sm mt-2 max-w-xl">
          Annotators answer one question at a time, in the order below, and the question they are on
          binds its answers to <Kbd>1</Kbd>…<Kbd>9</Kbd> top to bottom. Reordering therefore moves
          the digits their hands have learned, so each row previews its live binding. Each answer
          also carries an icon, which is what makes an option spottable at a glance instead of read
          word by word — pick one that survives being seen for a fifth of a second.
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
            position={i + 1}
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
  position,
  isFirst,
  isLast,
  busy,
  run,
}: {
  group: LabelGroup;
  /** 1-based position in the answering order. Not sort_order: the VALUE is
      meaningless to an annotator, the position is the whole story. */
  position: number;
  isFirst: boolean;
  isLast: boolean;
  busy: boolean;
  run: Run;
}) {
  const [editOpen, setEditOpen] = useState(false);
  // Rows and badges both come off visibleClasses(), via numberKeysFor() for the
  // second — the digits are POSITIONAL, so rendering a different order than the
  // keymap computed would make every badge a quiet lie.
  const activeClasses = visibleClasses(group);
  const archivedClasses = ordered(group.classes.filter((c) => !c.active), (c) => c.class_key);
  const keyByClass = new Map(numberKeysFor(group).map((o) => [o.class_key, o.label]));

  return (
    <Card className="p-5">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-baseline gap-2.5 min-w-0">
          <span className="font-mono text-[11px] text-gray-tertiary">#{position}</span>
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
            <div className="flex items-center gap-2.5 min-w-0">
              <span className="text-gray-tertiary">
                <ClassIcon name={c.icon} />
              </span>
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

/* ------------------------------------------------------------------- icons */

/* Pick an icon by SEEING it. A name list would make an operator translate
   "probe" into the shape an annotator will actually scan for, and they would
   get it wrong roughly as often as they got it right. Rendered as a radiogroup
   of buttons rather than a <select>, because a native select cannot show the
   glyphs and because there is no free-text path to leave open: the value ends up
   in the DOM, so the only names that exist are the nine in ICON_CHOICES (the
   server checks the same list again on write). */
function IconPicker({
  value,
  onChange,
  disabled,
}: {
  value: LabelIconName;
  onChange: (icon: LabelIconName) => void;
  disabled: boolean;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <span className="text-[12px] text-gray-tertiary">
        Icon — what the annotator's eye lands on before the word
      </span>
      <div role="radiogroup" aria-label="answer icon" className="flex items-center gap-1.5 flex-wrap">
        {ICON_CHOICES.map((name) => {
          const on = name === value;
          return (
            <button
              key={name}
              type="button"
              role="radio"
              aria-checked={on}
              aria-label={name}
              title={name}
              disabled={disabled}
              onClick={() => onChange(name)}
              className={
                "w-9 h-9 grid place-items-center rounded-xl border transition-colors duration-150 " +
                (on
                  ? "border-accent bg-accent-soft text-accent-deep"
                  : "border-border text-gray-mid hover:border-accent hover:text-accent-deep") +
                (disabled ? " opacity-40 cursor-not-allowed" : "")
              }
            >
              <ClassIcon name={name} />
            </button>
          );
        })}
      </div>
    </div>
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
  /** The live hotkey badge from labelKeys — "" means this answer sits past the
      ninth in its question and is mouse-only. */
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
            /* Past the ninth answer: say "mouse only" rather than showing a
               blank slot, which reads as a rendering bug. A question this long
               is a taxonomy problem — the digits deliberately do not wrap. */
            <span
              className="inline-grid place-items-center h-6 px-1.5 rounded-md border border-dashed border-border font-mono text-[10px] leading-none text-gray-tertiary"
              title="Past the ninth answer in this question — annotators pick it with the mouse"
            >
              mouse only
            </span>
          )}
          <span className="text-gray-mid">
            <ClassIcon name={cls.icon} />
          </span>
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
  const [icon, setIcon] = useState<LabelIconName>(asIconName(cls.icon));
  const [escape, setEscape] = useState(cls.is_escape);

  async function save() {
    const ok = await run(
      updateLabelClass(cls.class_key, {
        name: name.trim(),
        description: desc.trim(),
        icon,
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
      <IconPicker value={icon} onChange={setIcon} disabled={busy} />
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
  /* 'dot' is the neutral default, and it is a real choice rather than an absent
     one: an answer with no icon still has to occupy the same slot on the option
     row, or the rows would jump between iconed and un-iconed answers. */
  const [icon, setIcon] = useState<LabelIconName>("dot");
  const [escape, setEscape] = useState(false);

  async function add() {
    const ok = await run(
      createLabelClass(groupKey, {
        name: name.trim(),
        description: desc.trim(),
        icon,
        is_escape: escape,
      }),
    );
    if (ok) {
      setName("");
      setDesc("");
      setIcon("dot");
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
      <div className="mt-3">
        <IconPicker value={icon} onChange={setIcon} disabled={busy} />
      </div>
      <div className="flex items-center gap-5 flex-wrap mt-3">
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
              annotator disagreement. The icon is never a gate — 'dot' is a
              legitimate answer to "which icon", and blocking on it would only
              teach powerusers to pick one at random. */}
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
        title="Move up (changes the number keys annotators see)"
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
        title="Move down (changes the number keys annotators see)"
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
