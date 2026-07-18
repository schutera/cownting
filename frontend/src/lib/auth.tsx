import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useState,
  type ReactNode,
} from "react";
import type { User } from "./types";
import { getMe, login as apiLogin, logout as apiLogout, setUnauthorizedHandler } from "./api";
import { Button } from "../components/ui";

interface AuthCtx {
  user: User | null;
  login: (username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
}

const Ctx = createContext<AuthCtx | null>(null);

export function useAuth(): AuthCtx {
  const v = useContext(Ctx);
  if (!v) throw new Error("useAuth must be used within <AuthProvider>");
  return v;
}

/**
 * Whether a user may manage data — upload, download (CSV), or delete days. Only
 * powerusers and admins can; plain `user` accounts are view-only. Mirrors the
 * backend's `can_manage_data` gate (auth.py), so the UI only offers what the API
 * will actually allow. The server stays the source of truth — this just hides
 * controls that would 403.
 */
export function canManageData(user: User | null): boolean {
  return user?.role === "admin" || user?.role === "poweruser";
}

/**
 * Owns the session. On mount it probes /api/me; while that resolves the gate
 * shows nothing, then either the login screen or the app. Any 401 from an
 * ordinary API call (expired cookie, server restart) drops us back to login via
 * the module-level unauthorized handler.
 */
export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [ready, setReady] = useState(false);

  const refresh = useCallback(async () => {
    try {
      setUser(await getMe());
    } catch {
      setUser(null);
    } finally {
      setReady(true);
    }
  }, []);

  useEffect(() => {
    setUnauthorizedHandler(() => setUser(null));
    refresh();
    return () => setUnauthorizedHandler(null);
  }, [refresh]);

  const login = useCallback(async (username: string, password: string) => {
    setUser(await apiLogin(username, password));
  }, []);

  const logout = useCallback(async () => {
    await apiLogout();
    setUser(null);
  }, []);

  if (!ready) return null; // brief: waiting on the /api/me probe

  if (!user) {
    return (
      <Ctx.Provider value={{ user, login, logout }}>
        <LoginScreen />
      </Ctx.Provider>
    );
  }

  return <Ctx.Provider value={{ user, login, logout }}>{children}</Ctx.Provider>;
}

function LoginScreen() {
  const { login } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  }

  const input =
    "w-full bg-bg border border-border rounded-xl px-4 py-2.5 text-sm text-text " +
    "focus:outline-none focus:border-accent transition-colors";

  return (
    <div className="min-h-screen grid place-items-center bg-bg px-6">
      <form onSubmit={submit} className="w-full max-w-sm flex flex-col gap-5">
        <div className="flex items-center gap-3 justify-center mb-2">
          <div className="w-10 h-10 bg-accent text-white grid place-items-center rounded">🐄</div>
          <div>
            <div className="font-sans text-xl text-near-black leading-none">Cownting</div>
            <div className="text-[11px] font-mono uppercase tracking-[0.18em] text-gray-tertiary mt-1">
              solar-field herd analytics
            </div>
          </div>
        </div>
        <div className="flex flex-col gap-3">
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] text-gray-tertiary">Username</span>
            <input
              className={input}
              value={username}
              autoFocus
              autoComplete="username"
              onChange={(e) => setUsername(e.target.value)}
            />
          </label>
          <label className="flex flex-col gap-1.5">
            <span className="text-[12px] text-gray-tertiary">Password</span>
            <input
              className={input}
              type="password"
              value={password}
              autoComplete="current-password"
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
        </div>
        {error ? <p className="text-[13px] text-red-500">{error}</p> : null}
        <Button onClick={() => {}} disabled={busy || !username || !password} className="w-full justify-center">
          {busy ? "Signing in…" : "Sign in"}
        </Button>
      </form>
    </div>
  );
}
