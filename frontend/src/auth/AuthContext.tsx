/**
 * Session state.
 *
 * The token lives in localStorage so a refresh does not log you out. That is a
 * deliberate trade-off for a demo dashboard: it is readable by any script on
 * this origin, which is acceptable here and would not be in production, where
 * an httpOnly cookie plus CSRF protection is the right answer.
 *
 * The role is never trusted from the token. On boot we call /auth/me, so a
 * revoked or demoted account loses access as soon as it tries to do anything.
 */

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";

import { UnauthorizedError, api } from "../api/client";
import type { CurrentUser } from "../api/types";

const TOKEN_KEY = "ledger.token";

interface AuthState {
  token: string | null;
  user: CurrentUser | null;
  /** True until the stored token has been checked against the API. */
  loading: boolean;
  isAdmin: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => void;
  /** Called when any request 401s, so one dead token logs the app out. */
  onUnauthorized: () => void;
}

const AuthContext = createContext<AuthState | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [token, setToken] = useState<string | null>(() => localStorage.getItem(TOKEN_KEY));
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [loading, setLoading] = useState(true);

  const logout = useCallback(() => {
    localStorage.removeItem(TOKEN_KEY);
    setToken(null);
    setUser(null);
  }, []);

  // Validate whatever is in storage before showing the dashboard.
  useEffect(() => {
    if (!token) {
      setUser(null);
      setLoading(false);
      return;
    }

    let cancelled = false;
    setLoading(true);
    api
      .me(token)
      .then((current) => {
        if (!cancelled) setUser(current);
      })
      .catch(() => {
        if (!cancelled) logout();
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [token, logout]);

  const login = useCallback(async (email: string, password: string) => {
    const response = await api.login(email, password);
    localStorage.setItem(TOKEN_KEY, response.access_token);
    setToken(response.access_token);
  }, []);

  const value = useMemo<AuthState>(
    () => ({
      token,
      user,
      loading,
      isAdmin: user?.role === "admin",
      login,
      logout,
      onUnauthorized: logout,
    }),
    [token, user, loading, login, logout],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (!context) throw new Error("useAuth must be used inside <AuthProvider>");
  return context;
}

/** Shared by every panel: fetch, track loading/error, and log out on 401. */
export function useApiResource<T>(
  fetcher: (token: string) => Promise<T>,
  deps: unknown[] = [],
): { data: T | null; error: string | null; loading: boolean; reload: () => void } {
  const { token, onUnauthorized } = useAuth();
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    setLoading(true);

    fetcher(token)
      .then((result) => {
        if (cancelled) return;
        setData(result);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (cancelled) return;
        if (caught instanceof UnauthorizedError) {
          onUnauthorized();
          return;
        }
        setError(caught instanceof Error ? caught.message : "something went wrong");
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, nonce, ...deps]);

  return { data, error, loading, reload };
}
