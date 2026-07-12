# Orrery Web Console

Production web console for the Orrery agent platform — see
[AEP-019](../docs/enhancements/aep-019-web-console.md).

This is an **isolated Node toolchain**, not a member of the Python `uv`
workspace. It builds to a static bundle that the FastAPI front door
(`core/orrery_core/serving/server.py`) serves from `serving/static/` behind the
same JWT auth as the API. `make test` (Python) never touches Node.

## Status

Milestone 1 (MVP) — an authenticated chat console:

- Bearer-token gate (paste the dev JWT from `make run-assistant-api`)
- Chat against `POST /chat`, threading the server-issued `session_id`
- Identity + role badge (viewer / operator / admin), decoded client-side for
  display — the server remains authoritative for RBAC
- Loading, error, and auth-expiry states

Milestones 2 (triage/remediation view) and 3 (onboarding wizard) are tracked in
the AEP and not yet built.

## Run

From the repo root, wrapped as Make targets (one command each):

```bash
make run-console     # build the SPA + serve it behind the API at http://localhost:8000
make web-dev         # Vite dev server on :5173 with hot reload (proxies /chat to :8000)
make web-install     # npm ci
```

`make run-console` is the production-like path (same-origin, single port). For
fast iteration use two terminals — `make run-assistant-api` and `make web-dev` —
then mint a token with `make dev-token` and paste it into the gate.

The underlying npm scripts still work directly if you prefer:

```bash
cd web && npm install && npm run dev
```

Copy `.env.example` to `.env.local` to override the API base or dev proxy.

## Quality gate

```bash
make web-check       # lint + format + typecheck + test + build (mirrors CI)
make web-fmt         # prettier --write .
```

CI runs `make web-check` in a dedicated `web` job (see
`.github/workflows/ci.yml`), so the gate is defined once and matches local.
Underlying scripts: `npm run check`, `npm run test:watch`, `npm run build`.

## Layout

```
src/
  api/        HTTP client + wire types (mirror server.py; will be OpenAPI-generated)
  auth/       token storage, JWT display-decode, role mapping, useAuth
  chat/       useChat controller + message types
  components/ ChatConsole, MessageList, MessageInput, TokenGate, IdentityBadge
  styles/     theme-aware CSS (light/dark via prefers-color-scheme)
  test/       vitest setup (jsdom shims)
```

## Design notes

- **The console never makes access decisions.** The JWT is decoded client-side
  only to display the subject and role; every request is re-verified server-side
  and RBAC re-derives the role. When the confirmation UI lands (Milestone 1
  follow-up), it will be a thin renderer over the gate's pending payload — the
  browser sends the decision word + token, the server decides who may approve.
- **Same-origin by default.** `VITE_API_BASE_URL` is empty in production because
  FastAPI serves the bundle itself; a full URL is only for cross-origin dev.
- **Types are hand-maintained for now.** `src/api/types.ts` mirrors the FastAPI
  models; AEP-019 will generate them from the OpenAPI schema so the compiler
  catches contract drift.
