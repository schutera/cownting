import { useEffect, useState } from "react";
import type { Role, User } from "../lib/types";
import { listUsers, createUser, updateUser, deleteUser } from "../lib/api";
import { useAuth } from "../lib/auth";
import { Button, Card, SectionLabel } from "../components/ui";

const inputCls =
  "bg-bg border border-border rounded-xl px-3 h-10 box-border text-sm text-text " +
  "focus:outline-none focus:border-accent transition-colors";

const ROLE_OPTIONS: Role[] = ["user", "poweruser", "admin"];

// One-line plain-language description of what each role can do, shown as a hint.
const ROLE_BLURB: Record<Role, string> = {
  user: "View the dashboard only.",
  poweruser: "View, plus upload, download, and delete data.",
  admin: "Everything a poweruser can do, plus manage user accounts.",
};

// Badge colour by privilege: admin is the strongest accent, poweruser a softer
// accent, plain user the muted default.
function roleBadgeCls(role: Role): string {
  const base = "text-[11px] font-mono uppercase tracking-wider px-2 py-0.5 rounded-full border ";
  if (role === "admin") return base + "text-accent border-accent";
  if (role === "poweruser") return base + "text-accent-deep border-accent/40 bg-accent-soft";
  return base + "text-gray-tertiary border-border";
}

// --- Credential proposer: a memorable colour-vegetable-"Farmer" username + a
// strong random password, both drawn from the browser CSPRNG (crypto). Kept in
// the same house style as the accounts seeded from the CLI.
const COLOURS = [
  "Red", "Green", "Blue", "Purple", "Yellow", "Orange", "White", "Black",
  "Golden", "Silver", "Crimson", "Amber", "Teal", "Violet", "Indigo", "Olive",
];
const VEGETABLES = [
  "Pepper", "Pea", "Carrot", "Squash", "Pumpkin", "Onion", "Bean", "Corn",
  "Tomato", "Broccoli", "Cabbage", "Radish", "Turnip", "Beet", "Leek", "Kale",
  "Spinach", "Celery", "Parsnip", "Fennel",
];
// Ambiguity-free alphabet (no 0/O/1/l/I) so a proposed password is easy to read
// aloud and retype.
const PW_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789";

function randInt(n: number): number {
  const buf = new Uint32Array(1);
  crypto.getRandomValues(buf);
  return buf[0] % n;
}
function pick<T>(arr: T[]): T {
  return arr[randInt(arr.length)];
}
function proposePassword(len = 14): string {
  let s = "";
  for (let i = 0; i < len; i++) s += PW_ALPHABET[randInt(PW_ALPHABET.length)];
  return s;
}
function proposeUsername(taken: Set<string>): string {
  for (let i = 0; i < 60; i++) {
    const u = `${pick(COLOURS)}${pick(VEGETABLES)}Farmer`;
    if (!taken.has(u.toLowerCase())) return u;
  }
  // Vanishingly unlikely fallback if every combo is taken.
  return `${pick(COLOURS)}${pick(VEGETABLES)}Farmer${randInt(1000)}`;
}

/**
 * Admin-only user management: list accounts, add one, reset a password, flip a
 * role, or remove an account. Every mutation returns the fresh user list from the
 * server, so the table always reflects the store (including its guard rails — the
 * last admin can't be demoted or deleted).
 */
export default function Admin() {
  const { user } = useAuth();
  const [users, setUsers] = useState<User[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Add-user form
  const [nu, setNu] = useState("");
  const [np, setNp] = useState("");
  const [nr, setNr] = useState<Role>("user");
  const [reveal, setReveal] = useState(false);
  const [adding, setAdding] = useState(false);

  function propose() {
    const taken = new Set((users ?? []).map((u) => u.username.toLowerCase()));
    setNu(proposeUsername(taken));
    setNp(proposePassword());
    setReveal(true); // show it so it can be copied / shared before creating
    setError(null);
  }

  useEffect(() => {
    listUsers()
      .then(setUsers)
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }, []);

  function run(p: Promise<{ users: User[] }>) {
    setError(null);
    return p
      .then((r) => setUsers(r.users))
      .catch((e: unknown) => setError(e instanceof Error ? e.message : String(e)));
  }

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setAdding(true);
    await run(createUser(nu.trim(), np, nr));
    setNu("");
    setNp("");
    setNr("user");
    setReveal(false);
    setAdding(false);
  }

  if (error && users === null) {
    return <p className="text-gray-tertiary font-mono text-sm">Couldn't load users — {error}</p>;
  }
  if (users === null) {
    return <div className="animate-shimmer h-64 bg-surface border border-border rounded-2xl" />;
  }

  return (
    <div className="flex flex-col gap-6 max-w-2xl">
      <div>
        <SectionLabel>Access</SectionLabel>
        <h1 className="font-display text-3xl font-light text-near-black leading-tight mt-1">
          User management
        </h1>
        <p className="text-[13px] text-gray-mid mt-2">
          Signed in as <span className="text-text">{user?.username}</span>. Admins can add
          accounts, reset passwords, and change roles.
        </p>
      </div>

      {error ? <p className="text-[13px] text-red-500">{error}</p> : null}

      {/* Add a user */}
      <Card className="p-5">
        <div className="flex items-center justify-between gap-3">
          <SectionLabel>Add a user</SectionLabel>
          <button
            type="button"
            onClick={propose}
            className="text-[13px] text-accent hover:text-accent-deep transition-colors"
            title="Fill in a suggested username + strong password"
          >
            ✨ Propose credentials
          </button>
        </div>
        <form onSubmit={add} className="mt-3 flex flex-col gap-4">
          <div className="grid gap-3 sm:grid-cols-2 items-start">
            <label className="flex flex-col gap-1.5">
              <span className="text-[12px] text-gray-tertiary">Username</span>
              <input
                className={inputCls + " w-full"}
                value={nu}
                onChange={(e) => setNu(e.target.value)}
              />
            </label>
            <label className="flex flex-col gap-1.5">
              <span className="flex items-center justify-between text-[12px] text-gray-tertiary">
                Password
                <button
                  type="button"
                  onClick={() => setReveal((r) => !r)}
                  className="hover:text-accent-deep transition-colors"
                  title={reveal ? "Hide password" : "Show password"}
                >
                  {reveal ? "Hide" : "Show"}
                </button>
              </span>
              <input
                className={inputCls + " w-full"}
                type={reveal ? "text" : "password"}
                value={np}
                autoComplete="new-password"
                onChange={(e) => setNp(e.target.value)}
              />
            </label>
          </div>
          <label className="flex flex-col gap-1.5 max-w-sm">
            <span className="text-[12px] text-gray-tertiary">Role</span>
            <select
              className={inputCls + " w-full"}
              value={nr}
              onChange={(e) => setNr(e.target.value as Role)}
            >
              <option value="user">user</option>
              <option value="poweruser">poweruser</option>
              <option value="admin">admin</option>
            </select>
            <span className="text-[11px] text-gray-tertiary">{ROLE_BLURB[nr]}</span>
          </label>
          <div>
            <Button onClick={() => {}} disabled={adding || !nu.trim() || !np}>
              {adding ? "Adding…" : "Add"}
            </Button>
          </div>
        </form>
        {reveal && np ? (
          <p className="text-[12px] text-gray-tertiary mt-2">
            Copy this password now — it can’t be shown again once the account is created
            (you can only reset it later).
          </p>
        ) : null}
      </Card>

      {/* Existing users */}
      <div className="flex flex-col gap-2">
        <SectionLabel>Accounts ({users.length})</SectionLabel>
        {users.map((u) => (
          <UserRow key={u.username} u={u} me={user?.username ?? ""} run={run} />
        ))}
      </div>
    </div>
  );
}

function UserRow({
  u,
  me,
  run,
}: {
  u: User;
  me: string;
  run: (p: Promise<{ users: User[] }>) => Promise<void>;
}) {
  const [pw, setPw] = useState("");
  const [open, setOpen] = useState(false);
  const isMe = u.username === me;

  return (
    <Card className="p-4">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <div className="flex items-center gap-3">
          <span className="text-text text-sm">{u.username}</span>
          <span className={roleBadgeCls(u.role)}>{u.role}</span>
          {isMe ? <span className="text-[11px] text-gray-tertiary">(you)</span> : null}
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setOpen((o) => !o)}
            className="text-[13px] text-gray-mid hover:text-accent-deep transition-colors"
          >
            Password
          </button>
          <label className="sr-only" htmlFor={`role-${u.username}`}>
            Role for {u.username}
          </label>
          <select
            id={`role-${u.username}`}
            value={u.role}
            onChange={(e) => run(updateUser(u.username, { role: e.target.value as Role }))}
            title="Change this user's role"
            className="text-[13px] text-gray-mid bg-bg border border-border rounded-lg px-2 h-8 box-border hover:text-accent-deep focus:outline-none focus:border-accent transition-colors"
          >
            {ROLE_OPTIONS.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
          <button
            onClick={() => {
              if (confirm(`Delete user "${u.username}"? This cannot be undone.`)) {
                run(deleteUser(u.username));
              }
            }}
            disabled={isMe}
            className={
              "text-[13px] transition-colors " +
              (isMe ? "text-gray-tertiary opacity-40 cursor-not-allowed" : "text-red-500 hover:text-red-600")
            }
          >
            Delete
          </button>
        </div>
      </div>
      {open ? (
        <div className="flex items-end gap-2 mt-3 pt-3 border-t border-border">
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] text-gray-tertiary">New password</span>
            <input
              className={inputCls}
              type="password"
              value={pw}
              autoComplete="new-password"
              onChange={(e) => setPw(e.target.value)}
            />
          </label>
          <Button
            variant="ghost"
            onClick={() => {
              if (!pw) return;
              run(updateUser(u.username, { password: pw })).then(() => {
                setPw("");
                setOpen(false);
              });
            }}
            disabled={!pw}
          >
            Update
          </Button>
        </div>
      ) : null}
    </Card>
  );
}
