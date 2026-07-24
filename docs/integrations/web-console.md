# Web console

A single-page operator console served by the same FastAPI front door as the API
(AEP-019). It is a **transport over `AgentGateway`** — the same pipeline Slack
and Google Chat use — not a second implementation of the agent. No agent,
plugin, or guardrail logic changes when the console changes.

It is **off by default**. A browser-facing chat surface amplifies any auth gap,
so it only appears when you deliberately turn it on.

## What it is for

`adk web` is a *developer* debugger: raw events, function-call payloads, session
plumbing. This console is the *operator* surface — hold a conversation, see
which specialist ran which tool, approve a guarded action, and check whether the
environment is actually wired. Both coexist; neither replaces the other.

## Enabling it

```bash
make run-console          # builds the bundle and serves it at http://localhost:8000
```

That is `make web-build` (Vite → `serving/static/`) plus `make run-assistant-api`.
Mint a token in another terminal and paste it into the console:

```bash
make dev-token ROLE=operator
```

In a deployment, set:

| Variable | Default | Purpose |
|---|---|---|
| `ORRERY_WEB_CONSOLE_ENABLED` | `false` | Serve the bundle at `/`. Off unless set. |
| `ORRERY_CORS_ORIGINS` | *(empty)* | Only needed when the console is served from a different origin than the API. Never `*` — a wildcard disables credentialed CORS (with a warning). |
| `ORRERY_CHAT_RATE_LIMIT` | `30/minute` | Per-caller ceiling on `POST /chat`. |
| `ORRERY_SELFTEST_RATE_LIMIT` | `10/minute` | Per-caller ceiling on the environment check. |

The Docker image builds the bundle in a `node:` stage and copies it into the
Python runtime, so the runtime image stays Node-free.

## Authentication

The console holds no trust of its own. The static shell carries no secrets; the
operator pastes a JWT at load, and it rides the `Authorization` header on every
API call. The server verifies it, resolves the role via RBAC, and enforces
everything — the console's role badge is decoration.

!!! warning "The token lives in `localStorage`"
    That makes it readable by any script running on the same origin. The console
    ships no third-party scripts and renders agent replies through
    `react-markdown` (React elements, never `dangerouslySetInnerHTML`), but
    treat console tokens as short-lived and scope them to the role the operator
    actually needs.

Signing out clears the token, the transcripts, and any legacy keys — a shared
browser must not leak the previous user's conversations to whoever is next.

## What you get

### Chat with a visible tool timeline

The **Tool calls** tab lists every recorded call — which specialist, which tool,
which arguments, what status — so a multi-agent sweep reads as orchestration
rather than an opaque paragraph. While a turn is in flight the timeline is
polled every 2.5 s, so specialists appear as they finish. (Token-by-token
streaming is AEP-009; until then, this is the progress signal.)

A turn can be **stopped**. A triage sweep fans out to five specialists and can
run for a minute; the Stop button abandons the wait. The server keeps working —
this stops the client waiting, it does not cancel the run.

A failed turn offers **Retry**, which replays the same message without
duplicating it in the transcript. A rate-limited turn says so, with the wait.

### Approve / deny for guarded tools

When a `@confirm` or `@destructive` tool is gated, the console renders the
pending action and two buttons. The buttons send the literal decision words
through `POST /chat` — **the frontend decides nothing**. The
requester-verified gate is the only authority on who may approve, and it
refuses a decision from anyone but the requester, or one that predates the
action it would authorize.

### Triage view

**Run triage** sends a canned prompt to the `incident_triage_agent`. The result
renders as a severity banner (healthy / degraded / critical) from the recorded
`incident_severity` — the machine-readable verdict, not a parse of the prose —
with the full report below and a chip per system that was actually consulted.

!!! note "Chips are derived from tool calls, not from the report text"
    A system that was never called shows no chip. "We didn't ask" and "healthy"
    are different answers, and the console must not blur them.

### System tab — the environment check

The single most useful thing here for a new user. **Check my environment** runs,
concurrently and read-only:

- a **one-token round-trip** against the configured provider and model, and
- each specialist's **cheapest read-only tool** (Kafka cluster health, K8s
  cluster info, Elasticsearch cluster health, Prometheus targets, `docker ps`).

Each row is green or red with the reason and, when red, **what to configure**.
This exists because nearly every first-run failure is "a credential or endpoint
was never wired" — and the only feedback used to be a stack trace buried in a
tool result several turns into a conversation.

The tab also shows the **server-resolved** role and the **active autonomy
level** (L2/L3/L4), so a viewer understands up front why mutating tools are
unavailable rather than discovering it when one is refused.

```bash
# The same check over the API
curl -X POST -H "Authorization: Bearer $TOKEN" localhost:8000/onboarding/selftest
```

Probes must stay read-only: the self-test does not consult RBAC, so anything
with side effects does not belong in that list. Deployments register their own
via `create_app(integration_probes=...)` — core has no dependency on any agent
package.

## Endpoints the console uses

| Endpoint | Purpose |
|---|---|
| `POST /chat` | One conversation turn (rate limited per caller). |
| `GET /session/{id}/activity` | Tool-call timeline, scoped to the caller's own session. |
| `GET /session/{id}/triage` | Recorded severity + report for that session. |
| `GET /confirmations/pending` | The caller's own pending guarded action, for rendering. |
| `GET /me` | Server-resolved role, active autonomy level, configured model. |
| `POST /onboarding/selftest` | Model connectivity + integration probes. |

Session-scoped endpoints pin `user_id` to the verified subject, so another
user's session id resolves to a plain 404 — indistinguishable from one that
never existed.

## Developing

```bash
make web-install   # npm ci
make web-dev       # Vite dev server with HMR (expects the API on :8000)
make web-check     # lint + format + typecheck + tests + build (mirrors CI)
```

`web/` is a separate toolchain, deliberately outside the uv workspace: `make
test` never needs Node, and the Python contributor path stays intact. The API
contract and its consumer live in the same commit, so an endpoint change and its
frontend update land together.
