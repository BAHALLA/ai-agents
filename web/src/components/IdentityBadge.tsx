import type { MeResponse } from "../api/types";
import type { Identity } from "../auth/token";

interface Props {
  identity: Identity | null;
  /** The server's own view, once loaded. Authoritative where it disagrees. */
  me: MeResponse | null;
  onSignOut: () => void;
}

const autonomyTitle: Record<string, string> = {
  L2: "Autonomy L2 — read-only. Every mutating tool is blocked, whatever your role.",
  L3: "Autonomy L3 — mutating tools run; destructive ones are blocked.",
  L4: "Autonomy L4 — destructive tools run after an explicit human confirmation.",
};

const roleTitle: Record<Identity["role"], string> = {
  viewer: "Viewer — read-only. Mutating and destructive tools are blocked.",
  operator: "Operator — may run mutating tools; destructive tools need confirmation.",
  admin: "Admin — may run destructive tools after confirmation.",
};

const roleBadge: Record<Identity["role"], string> = {
  viewer: "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
  operator: "bg-blue-100 text-blue-700 dark:bg-blue-950 dark:text-blue-300",
  admin: "bg-pink-100 text-pink-700 dark:bg-pink-950 dark:text-pink-300",
};

/**
 * Shows who the operator is acting as and their resolved role. Display only —
 * decoded client-side from the token; the server re-derives the authoritative
 * role via RBAC on every call.
 */
export function IdentityBadge({ identity, me, onSignOut }: Props) {
  if (!identity) return null;
  // The server's resolution wins where the two differ — the token badge is the
  // browser's reading of a signature it cannot check.
  const role = me?.role ?? identity.role;
  const autonomy = me?.autonomy_level ?? null;
  return (
    <div role="status" className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <span
          className="min-w-0 flex-1 truncate text-sm text-slate-700 dark:text-slate-200"
          title={identity.subject}
        >
          {identity.subject}
        </span>
        <span
          className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold uppercase ${roleBadge[role]}`}
          title={roleTitle[role]}
        >
          {role}
        </span>
        {autonomy ? (
          <span
            className="shrink-0 rounded-full bg-slate-200 px-2 py-0.5 text-xs font-semibold text-slate-600 dark:bg-slate-700 dark:text-slate-300"
            title={autonomyTitle[autonomy] ?? `Autonomy ${autonomy}`}
          >
            {autonomy}
          </span>
        ) : null}
      </div>
      <button
        type="button"
        onClick={onSignOut}
        className="rounded-lg border border-slate-300 px-3 py-1.5 text-sm text-slate-600 hover:bg-slate-100 dark:border-slate-700 dark:text-slate-300 dark:hover:bg-slate-800"
      >
        Sign out
      </button>
    </div>
  );
}
