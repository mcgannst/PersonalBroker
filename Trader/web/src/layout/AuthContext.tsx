// Who is logged in (P4-T12). On start the provider asks `/auth/me`; a 401 means logged out. The HTTP
// client reports a 401 on any other request through `notifyUnauthorized()`, which clears the session here,
// and `RequireAuth` then sends the visitor to /login?next=<where they were>.
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { isApiError, useApi } from "../api/client";
import type { SessionOut, UserOut } from "../api/types";

export type AuthStatus = "loading" | "in" | "out" | "error";

export interface AuthState {
  user: UserOut | null;
  csrf: string | null;
  /** `loading` until the first `/auth/me` answers; `error` when the server could not be reached. */
  status: AuthStatus;
  /** The error of the last failed check (only while `status` is `error`). */
  error: unknown;
  /** Asks `/auth/me` again. */
  refresh(): Promise<void>;
  /** Ends the session on the server (errors ignored) and forgets it here. */
  logout(): Promise<void>;
  /** Stores the session a successful login returned. */
  setSession(session: SessionOut): void;
  /** Forgets the session without calling the server (after a 401). */
  clear(): void;
}

/** The DOM event the HTTP client's `onUnauthorized` fires; `AuthProvider` listens for it. */
export const UNAUTHORIZED_EVENT = "trader:unauthorized";

export function notifyUnauthorized(): void {
  window.dispatchEvent(new Event(UNAUTHORIZED_EVENT));
}

const AuthContext = createContext<AuthState | null>(null);

interface Session {
  status: AuthStatus;
  user: UserOut | null;
  csrf: string | null;
  error: unknown;
}

const LOADING: Session = { status: "loading", user: null, csrf: null, error: null };
const OUT: Session = { status: "out", user: null, csrf: null, error: null };

export function AuthProvider({ children }: { children?: ReactNode }) {
  const api = useApi();
  const queryClient = useQueryClient();
  const [session, setSessionState] = useState<Session>(LOADING);
  const mounted = useRef(true);

  const setSession = useCallback((s: SessionOut) => {
    setSessionState({ status: "in", user: s.user, csrf: s.csrf_token, error: null });
  }, []);

  const clear = useCallback(() => {
    setSessionState((prev) => (prev.status === "out" ? prev : OUT));
    queryClient.clear();
  }, [queryClient]);

  const refresh = useCallback(async () => {
    try {
      const s = await api.me();
      if (mounted.current) setSession(s);
    } catch (err) {
      if (!mounted.current) return;
      if (isApiError(err) && err.status === 401) {
        setSessionState(OUT);
      } else {
        // Keep a known session through a blip; only a first check that fails shows the error.
        setSessionState((prev) => (prev.status === "in" ? prev : { status: "error", user: null, csrf: null, error: err }));
      }
    }
  }, [api, setSession]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // The session is forgotten here either way.
    }
    if (mounted.current) clear();
  }, [api, clear]);

  useEffect(() => {
    mounted.current = true;
    void refresh();
    return () => {
      mounted.current = false;
    };
  }, [refresh]);

  useEffect(() => {
    const onUnauthorized = () => clear();
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, [clear]);

  const value = useMemo<AuthState>(
    () => ({ ...session, refresh, logout, setSession, clear }),
    [session, refresh, logout, setSession, clear],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

/** The auth state, or null outside an `AuthProvider` (the login page renders standalone in tests). */
export function useOptionalAuth(): AuthState | null {
  return useContext(AuthContext);
}

export function useAuth(): AuthState {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error("useAuth() needs an <AuthProvider> above it");
  return ctx;
}
