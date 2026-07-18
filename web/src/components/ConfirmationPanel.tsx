import type { PendingConfirmation } from "../api/types";

interface Props {
  pending: PendingConfirmation;
  disabled: boolean;
  onDecide: (word: "approve" | "deny") => void;
}

/**
 * Approve/Deny panel for the caller's pending guarded action.
 *
 * Strictly a renderer: the buttons send the literal words "approve"/"deny"
 * through the normal chat flow, and the server's requester-verified gate
 * remains the sole authority on whether that decision authorizes anything.
 * The moment this component started deciding *who* may approve, the
 * guarantee would be bypassed — so it never does.
 */
export function ConfirmationPanel({ pending, disabled, onDecide }: Props) {
  const argEntries = Object.entries(pending.args);

  return (
    <section
      role="alertdialog"
      aria-label="Pending confirmation"
      className="mx-4 mb-2 rounded-xl border border-slate-200 border-l-4 border-l-amber-500 bg-white p-4 shadow-sm dark:border-slate-700 dark:border-l-amber-500 dark:bg-slate-800"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded-full bg-amber-100 px-2 py-0.5 text-xs font-semibold uppercase tracking-wide text-amber-700 dark:bg-amber-950 dark:text-amber-300">
          {pending.level || "guarded"}
        </span>
        <span className="text-sm text-slate-700 dark:text-slate-200">
          Awaiting your approval: <code className="font-mono font-medium">{pending.tool_name}</code>
        </span>
      </div>
      {argEntries.length > 0 ? (
        <dl className="mt-2 grid gap-1 text-sm">
          {argEntries.map(([key, value]) => (
            <div key={key} className="flex gap-2">
              <dt className="min-w-32 text-slate-500 dark:text-slate-400">{key}</dt>
              <dd className="m-0 font-mono break-all text-slate-800 dark:text-slate-200">
                {typeof value === "string" ? value : JSON.stringify(value)}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      <div className="mt-3 flex gap-2">
        <button
          type="button"
          disabled={disabled}
          onClick={() => onDecide("approve")}
          className="rounded-lg bg-blue-600 px-4 py-1.5 text-sm font-medium text-white hover:bg-blue-700 disabled:opacity-50"
        >
          Approve
        </button>
        <button
          type="button"
          disabled={disabled}
          onClick={() => onDecide("deny")}
          className="rounded-lg border border-red-500 px-4 py-1.5 text-sm font-medium text-red-600 hover:bg-red-50 disabled:opacity-50 dark:text-red-400 dark:hover:bg-red-950/40"
        >
          Deny
        </button>
      </div>
    </section>
  );
}
