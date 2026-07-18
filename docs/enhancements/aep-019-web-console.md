# AEP-019: Web Console for Onboarding & Operator Usage

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--blue">in-progress</span> (Milestone 1 complete; Milestone 2 triage view shipped — verdict banner + report + in-flight timeline polling; Milestone 3 remaining) |
| **Priority** | <span class="badge badge--blue">P2</span> |
| **Effort** | High (8-12 days) |
| **Impact** | Medium-High (adoption / onboarding) |
| **Dependencies** | AEP-013 (auth layer — a user-facing UI must not ship before the perimeter closes), AEP-009 (streaming — strongly recommended for chat UX, not blocking) |

## Gap Analysis

### Current Implementation

The platform is reachable today through five surfaces, none of which is a
purpose-built product UI for a first-time user or an operator running a triage:

- **`adk web` Dev UI** (`docker compose --profile demo`, port `8000`) — ADK's
  built-in **developer** console. It exposes internal event traces, raw
  function-call payloads, and session plumbing. It's a debugging tool, not an
  onboarding surface, and it has no concept of the platform's RBAC roles,
  autonomy levels, or the requester-verified confirmation flow.
- **`POST /chat`** (`core/orrery_core/serving/server.py`) — a JSON-only front
  door behind JWT. It returns `{"reply": str, "session_id": str}` with no UI.
- **CLI** (`make run-assistant-cli`) — terminal only.
- **Slack / Google Chat bots** — excellent for existing teams already in those
  tools, but they assume the platform is already configured, credentialed, and
  deployed. They are not an onboarding path.

### Gap

A new user evaluating or adopting Orrery has no low-friction way to:

1. **See what the platform can do** without reading the docs and wiring a
   transport (there is no "try it" surface with the specialists visible).
2. **Run a chat conversation** with streamed output, tool-call visibility, and
   the confirmation flow rendered as a real approve/deny UI (today the
   requester-verified handshake only has Slack/Chat card renderings).
3. **Watch a triage sweep** — the batch `orrery_triage_workflow` fans out to five
   specialists and produces a severity verdict, but there's no view of that
   graph running, per-system status, or the remediation act→verify→retry loop.
4. **Understand the safety posture at a glance** — their role (viewer/operator/
   admin), the active autonomy level (L2/L3/L4), and *why* a given tool was
   blocked or is awaiting confirmation.
5. **Onboard** — pick a provider/model, drop in credentials, and confirm
   connectivity, without editing `.env` by hand.

The building blocks already exist server-side. `AgentGateway`
(`serving/gateway.py`) is a ports-and-adapters pipeline any transport can drive;
`extract_reply_text()` already funnels events for every transport;
`ActivityPlugin` records tool calls to session state; `MetricsPlugin` and the
tracing stack already surface tool-call rate, latency, and cost. A web console is
**a new `ChannelAdapter` + a frontend**, not new agent logic.

## Proposed Solution

A single-page web console served by the existing FastAPI app, reusing
`AgentGateway` so no agent or plugin code changes. Ship in three incremental
milestones so the first one is usable on its own.

### Milestone 1 — Chat console (MVP)

A minimal, dependency-light SPA (see "Frontend stack" below) that talks to the
existing HTTP front door:

- **Chat pane** — send a message, render the reply. Uses the streaming endpoint
  from AEP-009 if available (token-by-token); falls back to the current
  request/response `POST /chat` otherwise.
- **Tool-call timeline** — render `ActivityPlugin`'s recorded calls inline
  (which specialist, which tool, status, duration) so the user *sees* the
  orchestration instead of an opaque paragraph. Data already lives in session
  state; expose it via a read endpoint (`GET /session/{id}/activity`).
- **Confirmation UI** — when a `@confirm`/`@destructive` tool returns the
  pending-confirmation payload, render an **Approve / Deny** panel that posts the
  deliberate approve word back through the gateway. This reuses the exact
  requester-verified rule already enforced (`verified_confirmation=True`): the
  approver must be the same authenticated subject; the console just needs to send
  the JWT and the decision word. **No new trust logic** — the gate stays the
  source of truth.
- **Identity/role badge** — show the authenticated subject, resolved role, and
  active autonomy level (read from the JWT claims + `session.state`), so a viewer
  understands up front why mutating tools are unavailable.

### Milestone 2 — Triage & remediation view

- **Run triage** button that invokes the single-turn `incident_triage_agent`
  (already an `AgentTool` on the chat root) and renders the five specialist
  statuses as they land, the severity verdict (`record_triage_verdict`), and the
  journal entry.
- **Remediation trace** — when the closed-loop remediation runs, render the
  act → verify → retry iterations and the 3-iteration cap, sourced from the
  tracing spans / activity state that already exist.

### Milestone 3 — Onboarding wizard

- **Provider setup** — pick `MODEL_PROVIDER` / `MODEL_NAME`, paste a key, and hit
  a **connectivity check** endpoint that does a one-token round-trip and reports
  success/failure. This is the single biggest first-run friction point today
  (hand-editing `.env`).
- **Specialist self-test** — a "check my environment" panel that runs each
  specialist's cheapest read-only tool (e.g. Kafka list-brokers, K8s list-nodes,
  Docker ps) under **L2 autonomy** and shows green/red per specialist, so a new
  user learns immediately which integrations are wired.
- **Guided first query** — a few canned prompts ("What's the health of my Kafka
  cluster?") to demonstrate routing.

### Serving & security

- Serve static assets from the existing FastAPI app behind the same JWT
  dependency the API uses (`FileResponse` / `StaticFiles`), gated by a
  `ORRERY_WEB_CONSOLE_ENABLED` flag (default off) so it never exposes a surface
  unless deliberately turned on. Add a `[console]` extra if any server-side deps
  are needed, mirroring the `[server]` / `[otel]` pattern.
- **The console must not ship before AEP-013's auth layer** is complete — a
  browser-facing chat surface with no authentication is the exact "anyone on the
  network gets admin" risk that moved AEP-013 to P0. The console inherits the
  JWT front door and the requester-verified confirmation flow rather than adding
  its own auth.
- Reuse rate limiting from AEP-011 on the console's endpoints.

### Frontend stack

**Decision: a Vite + React SPA, in-repo under a top-level `web/` directory, built
as an isolated Node toolchain and baked into the Python image at build time.**
This keeps the API contract and its consumer in the same commit while keeping the
two toolchains cleanly separated. (An earlier draft weighed a build-light
Preact/vanilla option; React is the deliberate choice for the Milestone 2/3
triage and remediation visualizations — e.g. a charting library for the metrics
and severity views — and for ecosystem familiarity.)

**Monorepo, not a separate repo.** The SPA lives or dies by
`serving/server.py`'s endpoints (`/chat`, `/session/{id}/activity`, the
confirmation and onboarding routes), so a PR that changes an endpoint changes the
frontend in the same commit — no cross-repo version skew, and the demo stays a
single shipped image. Split `web/` into its own repo only if it later grows an
independent release cadence, its own team, or a second backend; none of that is
true today, and extracting it later is cheap while merging two repos back is not.

**Isolated from the uv workspace.** React is a separate toolchain, not a
workspace member:

- `web/` at the repo root with its own `package.json` / Vite build;
  `web/node_modules` and `web/dist` are git-ignored.
- **CI:** a separate `web-ci` job (Node setup → `npm ci` → lint → `vite build`)
  that runs in parallel with the Python quality gate. `make test` must **not**
  depend on Node being installed — the pure-Python contributor path stays intact.
- **Docker:** a two-stage build — a `node:` stage runs `vite build`, then the
  Python runtime image copies `web/dist` into `core/orrery_core/serving/static/`.
  The runtime image stays Python-only (no Node in production). FastAPI serves the
  bundle via `StaticFiles` behind the same JWT dependency.
- **Typed API boundary:** generate the TS client/types from FastAPI's OpenAPI
  schema (e.g. `openapi-typescript`) rather than hand-writing them — this is what
  makes the monorepo pay off, since the compiler then catches an endpoint change.

The `serving/static/` directory therefore holds **build output**, not
hand-authored assets — the source of truth is `web/`.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/serving/server.py` | Serve static console behind JWT + `ORRERY_WEB_CONSOLE_ENABLED`; add `GET /session/{id}/activity`, streaming chat endpoint (AEP-009), and `POST /onboarding/check-connectivity` |
| `core/orrery_core/serving/console_adapter.py` | New — `ChannelAdapter` bridging browser requests to `AgentGateway` |
| `web/` | New — Vite + React SPA source (chat, tool timeline, confirmation panel, triage/remediation views, onboarding wizard) with its own `package.json`; git-ignore `web/node_modules` and `web/dist` |
| `core/orrery_core/serving/static/` | Build output only — `web/dist` is copied here at image build time; not hand-authored, not committed |
| `core/pyproject.toml` | Optional `[console]` extra if server-side deps are needed |
| `Dockerfile` | Two-stage build — a `node:` stage runs `vite build`; the Python runtime stage copies `web/dist` into `serving/static/` (no Node in the runtime image) |
| `.github/workflows/ci.yml` | New `web-ci` job (Node setup → `npm ci` → lint → `vite build`), parallel to the Python gate; `make test` stays Node-free |
| `.gitignore` | Add `web/node_modules`, `web/dist` |
| `docker-compose.yml` | Point the demo web service at the FastAPI console instead of (or alongside) `adk web`; keep Dev UI available for developers |
| `docs/integrations/web-console.md` | New — enabling, auth, and screenshots |
| `mkdocs.yml` | Add the console page to nav |
| `.env.example` | Add `ORRERY_WEB_CONSOLE_ENABLED` |

## Acceptance Criteria

- [x] Console is served by the existing FastAPI app, behind the JWT dependency, and is **off by default** (`ORRERY_WEB_CONSOLE_ENABLED`)
- [x] Milestone 1: a user can hold a chat conversation, see the tool-call timeline (`GET /session/{id}/activity`, owner-scoped by the JWT subject), and approve/deny a guarded action (`GET /confirmations/pending` renders it; the buttons send the literal decision words through `POST /chat`) — the approval is enforced by the existing requester-verified gate; a second user cannot approve someone else's action because pendings are requester-scoped and the gate, not the frontend, decides
- [ ] The role + autonomy-level badge reflects the authenticated subject and blocks mutating tools for a viewer with a clear reason *(role badge shipped; surfacing the active autonomy level remains)*
- [ ] Milestone 2: a triage run renders the five specialist statuses, the severity verdict, and the remediation loop iterations *(shipped: a **Run triage** header button sends the canned prompt to the `incident_triage_agent`; `GET /session/{id}/triage` exposes the recorded `incident_severity` + `triage_report` — ADK's `AgentTool` forwards the sub-session state delta to the parent, so the chat-root verdict lands in the HTTP session — rendered as a severity banner (healthy/degraded/critical) with the full report collapsed inside; the tool-call timeline is polled every 2.5s while a request is in flight so multi-specialist sweeps become visible as each specialist completes. Remaining: structured per-system status chips, and the remediation-loop trace — the act→verify→retry loop only exists in the batch `orrery_triage_workflow`, which the console does not host)*
- [ ] Milestone 3: the onboarding connectivity check reports provider/model reachability and per-specialist read-only self-test status
- [ ] No changes to agent, plugin, or guardrail logic — the console is a transport (`ChannelAdapter`) over `AgentGateway`
- [ ] Documented in `docs/integrations/web-console.md` and reachable from the getting-started guide
- [ ] The demo compose stack shows the product console; `adk web` Dev UI remains available for developers

## Notes

- **Do not reimplement `adk web`.** Its value is developer debugging (raw events,
  traces). The product console's value is onboarding, safe operator usage, and
  making the multi-agent orchestration + safety posture legible. They coexist.
- Streaming (AEP-009) is not a hard dependency but the chat UX is noticeably worse
  without it on multi-specialist sweeps — sequence AEP-009 first, or ship
  Milestone 1 with request/response and upgrade in place.
- Keep the confirmation UI a thin renderer over the gate's pending payload. The
  moment the frontend starts deciding *who* may approve, the requester-verified
  guarantee is bypassed. The browser sends the decision word + JWT; the server
  decides.
- The onboarding self-test is the highest-leverage single feature here — most
  first-run failures are "a credential/endpoint isn't wired," and today the only
  feedback is a stack trace.

## Priority Rationale

P2, not P0/P1. A web console is an **adoption and UX** accelerator, not a
production-blocking capability — the platform already runs in production via
Slack/Chat/HTTP. It sits behind the unfinished security perimeter (AEP-013,
AEP-014) because a browser-facing surface *amplifies* any auth gap, and it pairs
naturally with streaming (AEP-009), which is also P2. Promote to P1 if onboarding
friction becomes the top adoption blocker in practice.
