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

## Develop

```bash
cd web
npm install
npm run dev          # http://localhost:5173, proxies /chat to VITE_DEV_API_TARGET
```

Point the dev proxy at a running front door (default `http://localhost:8000`):

```bash
# terminal 1 — the API
make run-assistant-api
# terminal 2 — the console
VITE_DEV_API_TARGET=http://localhost:8000 npm run dev
```

Copy `.env.example` to `.env.local` to override the API base or dev proxy.

## Quality gate

```bash
npm run check        # lint + typecheck + test
npm run build        # tsc --noEmit && vite build  → dist/
npm run test:watch   # vitest in watch mode
npm run format       # prettier --write .
```

CI runs the same commands in a dedicated `web` job (see
`.github/workflows/ci.yml`), independent of the Python gate.

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
