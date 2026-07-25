import type { Role } from "../api/types";
import { config } from "../config";

/** Claims we read for display. The token may carry many more. */
export interface TokenClaims {
  sub?: string;
  roles?: string | string[];
  exp?: number;
  [key: string]: unknown;
}

export interface Identity {
  subject: string;
  role: Role;
  /** Expiry as epoch ms, or null if the token has no `exp`. */
  expiresAt: number | null;
}

const ADMIN_ROLES = new Set(["admin", "orrery-admin", "orrery_admin"]);
const OPERATOR_ROLES = new Set(["operator", "orrery-operator", "orrery_operator"]);

/**
 * Decode a JWT payload WITHOUT verifying its signature.
 *
 * This is for display only (showing the operator who they're acting as and
 * their role). The server re-verifies every token and re-derives the role via
 * RBAC — never trust anything decoded here for an access decision.
 */
export function decodeJwt(token: string): TokenClaims | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  try {
    const payload = parts[1].replace(/-/g, "+").replace(/_/g, "/");
    const json = decodeURIComponent(
      atob(payload)
        .split("")
        .map((c) => `%${c.charCodeAt(0).toString(16).padStart(2, "0")}`)
        .join(""),
    );
    const parsed: unknown = JSON.parse(json);
    return parsed && typeof parsed === "object" ? (parsed as TokenClaims) : null;
  } catch {
    return null;
  }
}

/**
 * Read `path` from `claims`, following `.` into nested objects.
 *
 * Mirrors `_lookup_claim` in auth.py. Providers nest their roles — Keycloak
 * uses `realm_access.roles` — so the badge has to follow the same path the
 * server does, or the console shows a different role than RBAC will enforce.
 */
function lookupClaim(claims: TokenClaims, path: string): unknown {
  if (!path.includes(".")) return claims[path];
  let current: unknown = claims;
  for (const segment of path.split(".")) {
    if (typeof current !== "object" || current === null) return undefined;
    current = (current as Record<string, unknown>)[segment];
    if (current == null) return undefined;
  }
  return current;
}

/**
 * Map a roles claim to viewer/operator/admin, mirroring the server's
 * `extract_role` (auth.py). Accepts a list or a space/comma-separated string,
 * read from `roleClaim` (which may be a dotted path).
 */
export function roleFromClaims(claims: TokenClaims, roleClaim = config.roleClaim): Role {
  const raw = lookupClaim(claims, roleClaim);
  if (raw == null) return "viewer";
  if (!Array.isArray(raw) && typeof raw !== "string") return "viewer";
  const tokens = (Array.isArray(raw) ? raw : raw.replace(/,/g, " ").split(" "))
    .map((t) => String(t).trim().toLowerCase())
    .filter(Boolean);

  if (tokens.some((t) => ADMIN_ROLES.has(t))) return "admin";
  if (tokens.some((t) => OPERATOR_ROLES.has(t))) return "operator";
  return "viewer";
}

/**
 * Best human-readable name for the caller.
 *
 * `sub` is the stable identifier and what RBAC keys on, but providers are free
 * to make it opaque — Keycloak uses a UUID, so a badge reading `sub` shows
 * "7beb901e-d084-…" instead of a person. Prefer the human claims for display
 * and fall back to `sub`.
 */
function displayName(claims: TokenClaims): string {
  for (const key of ["preferred_username", "email", "name"] as const) {
    const value = claims[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return typeof claims.sub === "string" && claims.sub ? claims.sub : "unknown";
}

/** Build a display Identity from a raw token, or null if it can't be decoded. */
export function identityFromToken(token: string): Identity | null {
  const claims = decodeJwt(token);
  if (!claims) return null;
  return {
    subject: displayName(claims),
    role: roleFromClaims(claims),
    expiresAt: typeof claims.exp === "number" ? claims.exp * 1000 : null,
  };
}

/** True if the token carries an `exp` that is already in the past. */
export function isExpired(identity: Identity, now: number = Date.now()): boolean {
  return identity.expiresAt !== null && identity.expiresAt <= now;
}
