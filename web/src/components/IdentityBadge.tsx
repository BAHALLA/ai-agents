import type { Identity } from "../auth/token";

interface Props {
  identity: Identity | null;
  onSignOut: () => void;
}

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
export function IdentityBadge({ identity, onSignOut }: Props) {
  if (!identity) return null;
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
          className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-semibold uppercase ${roleBadge[identity.role]}`}
          title={roleTitle[identity.role]}
        >
          {identity.role}
        </span>
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
