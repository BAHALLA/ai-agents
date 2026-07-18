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
    <section className="confirm" role="alertdialog" aria-label="Pending confirmation">
      <div className="confirm__header">
        <span className={`confirm__level confirm__level--${pending.level || "confirm"}`}>
          {pending.level || "guarded"}
        </span>
        <span className="confirm__title">
          Awaiting your approval: <code>{pending.tool_name}</code>
        </span>
      </div>
      {argEntries.length > 0 ? (
        <dl className="confirm__args">
          {argEntries.map(([key, value]) => (
            <div key={key} className="confirm__arg">
              <dt>{key}</dt>
              <dd>{typeof value === "string" ? value : JSON.stringify(value)}</dd>
            </div>
          ))}
        </dl>
      ) : null}
      <div className="confirm__actions">
        <button
          type="button"
          className="btn btn--approve"
          disabled={disabled}
          onClick={() => onDecide("approve")}
        >
          Approve
        </button>
        <button
          type="button"
          className="btn btn--deny"
          disabled={disabled}
          onClick={() => onDecide("deny")}
        >
          Deny
        </button>
      </div>
    </section>
  );
}
