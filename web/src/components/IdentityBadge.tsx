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

/**
 * Shows who the operator is acting as and their resolved role. Display only —
 * decoded client-side from the token; the server re-derives the authoritative
 * role via RBAC on every call.
 */
export function IdentityBadge({ identity, onSignOut }: Props) {
  if (!identity) return null;
  return (
    <div className="identity" role="status">
      <div className="identity__who">
        <span className="identity__subject" title={identity.subject}>
          {identity.subject}
        </span>
        <span className={`badge badge--${identity.role}`} title={roleTitle[identity.role]}>
          {identity.role}
        </span>
      </div>
      <button type="button" className="btn btn--ghost" onClick={onSignOut}>
        Sign out
      </button>
    </div>
  );
}
