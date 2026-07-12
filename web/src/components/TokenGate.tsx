import { useState } from "react";

interface Props {
  onSubmit: (token: string) => void;
  error?: string | null;
}

/**
 * First-run gate: the front door requires a JWT bearer token. Until AEP-013's
 * OAuth flow lands, the operator pastes a token (e.g. the dev JWT from
 * `make run-assistant-api`). It is stored locally and sent as `Authorization:
 * Bearer` on every request.
 */
export function TokenGate({ onSubmit, error }: Props) {
  const [value, setValue] = useState("");

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (trimmed) onSubmit(trimmed);
  };

  return (
    <div className="gate">
      <form className="gate__card" onSubmit={handleSubmit}>
        <h1 className="gate__title">Orrery Console</h1>
        <p className="gate__subtitle">Paste a bearer token to connect to the agent front door.</p>
        <label className="gate__label" htmlFor="token">
          JWT bearer token
        </label>
        <textarea
          id="token"
          className="gate__input"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          placeholder="eyJhbGciOi..."
          rows={4}
          autoFocus
          spellCheck={false}
        />
        {error ? (
          <p className="gate__error" role="alert">
            {error}
          </p>
        ) : null}
        <button type="submit" className="btn btn--primary" disabled={!value.trim()}>
          Connect
        </button>
        <p className="gate__hint">
          The token is stored in your browser only and never leaves it except as an
          <code> Authorization </code> header to the API.
        </p>
      </form>
    </div>
  );
}
