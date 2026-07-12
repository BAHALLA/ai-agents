import { useCallback, useMemo, useState } from "react";
import { storageKeys } from "../config";
import { identityFromToken, isExpired, type Identity } from "./token";

export interface AuthState {
  token: string | null;
  identity: Identity | null;
  /** True when a token is present and either has no expiry or hasn't expired. */
  isAuthenticated: boolean;
  signIn: (token: string) => void;
  signOut: () => void;
}

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(storageKeys.token);
  } catch {
    return null; // storage disabled (private mode / SSR)
  }
}

/**
 * Owns the bearer token: persists it to localStorage and derives the display
 * Identity from it. The token is the only auth state — the server verifies it
 * on every request, so there is no client-side session to expire independently.
 */
export function useAuth(): AuthState {
  const [token, setToken] = useState<string | null>(readStoredToken);

  const signIn = useCallback((next: string) => {
    const trimmed = next.trim();
    try {
      localStorage.setItem(storageKeys.token, trimmed);
    } catch {
      // Non-persistent session if storage is unavailable — still usable in-memory.
    }
    setToken(trimmed);
  }, []);

  const signOut = useCallback(() => {
    try {
      localStorage.removeItem(storageKeys.token);
      localStorage.removeItem(storageKeys.sessionId);
    } catch {
      // ignore
    }
    setToken(null);
  }, []);

  const identity = useMemo(() => (token ? identityFromToken(token) : null), [token]);

  const isAuthenticated = useMemo(
    () => token !== null && (identity === null || !isExpired(identity)),
    [token, identity],
  );

  return { token, identity, isAuthenticated, signIn, signOut };
}
