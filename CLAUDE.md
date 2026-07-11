# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
make install          # Install all workspace packages (uv sync --all-extras)
make test             # Run all 869 unit tests across all packages
make eval             # Run 33 agent eval scenarios (requires LLM credentials)
make lint             # ruff check + format check
make fmt              # Auto-fix linting and formatting
```

Run tests for a single agent:
```bash
uv run pytest agents/kafka-health/tests/ -v
```

Run infrastructure (Kafka + Zookeeper + Kafka UI):
```bash
make infra-up         # Start infrastructure
make infra-down       # Stop infrastructure
make infra-reset      # Stop + wipe volumes (fixes cluster.id mismatch)
```

Docker demo (full stack with web UI on :8000):
```bash
docker compose --profile demo up -d --build   # (Makefile docker-* wrappers were removed)
docker compose --profile demo down
```

Run the orchestrator (orrery-assistant composes every specialist agent, so run it
directly rather than each agent standalone):
```bash
make run-assistant              # ADK Dev UI (in-memory)
make run-assistant-cli          # Terminal mode
make run-assistant-api          # FastAPI front door (auth ON, dev JWT)
make run-assistant-persistent   # Persistent store (Postgres via DATABASE_URL, else in-memory)
make run-triage                 # Deterministic triage Workflow, one batch run
```

## Architecture

This is a **DevOps/SRE agent platform** built on **Google ADK** (Agent Development Kit). It uses a **uv workspace** with Python 3.14.

### Workspace Layout

- **`core/`** — Shared library (`orrery-core`): agent factory, multi-provider LLM support (Gemini/Claude/OpenAI/Ollama via LiteLLM), RBAC, config, guardrails, input validation, resilience (circuit breaker + retry), structured logging, audit trail, activity tracking, error handlers, persistent runner
- **`agents/`** — Independent agent packages, each runnable standalone or composable:
  - `kafka-health/` — Kafka cluster monitoring (8 tools, uses confluent-kafka) + Strimzi operator tools
  - `k8s-health/` — Kubernetes cluster management (11 tools, uses kubernetes client) + operator-aware tools
  - `elasticsearch/` — Elasticsearch cluster/index/shard diagnostics (19 REST tools) + ECK operator tools (5 tools)
  - `ops-journal/` — State management demo with 4 state scopes (session/user/app/temp)
  - `orrery-assistant/` — Multi-agent orchestrator that composes all above agents + Docker tools

### Key Design Patterns

- **Plugins over per-agent callbacks**: Cross-cutting concerns (RBAC, guardrails, autonomy, metrics, audit, activity tracking, resilience, output capping, error handling) are packaged as ADK `BasePlugin` subclasses in `core/orrery_core/plugins/` and registered once on the `Runner` via `default_plugins()`. Plugins apply globally to every agent, tool, and LLM call — no per-agent callback wiring needed. Order matters: `AuditPlugin` is registered **before** the gates (ADK's before-tool chain early-exits on the first non-None return, so audit records the attempt even when a gate denies the call), and `ToolOutputCapPlugin` runs last among after-tool observers (it returns a replacement for oversized results, which would early-exit the after chain).
- **Autonomy levels (L2/L3/L4)**: `AutonomyPlugin` gates by *process mode*, orthogonal to RBAC's *who*: L2 read-only (fail-closed — only unguarded tools + whitelist), L3 mutating with `@destructive` blocked (+ blacklist), L4 destructive allowed after ADK-native `request_confirmation`. Opt-in: registered only when `ORRERY_AUTONOMY_LEVEL` (or `default_plugins(autonomy_level=...)`) is set; a per-request override reads `session.state["autonomy_level"]`. Blocked calls return a structured `{"status": "BLOCKED"}` dict so audit/metrics still observe them.
- **Tool output cap**: `ToolOutputCapPlugin` bounds each tool result to `max_tool_result_bytes` (default 4 MiB; `0` disables) so one chatty `logs`/wide ES result re-sent every turn can't push the request past the Gemini/Vertex ~10 MiB limit (`400 INVALID_ARGUMENT`). Trimming is structure-preserving (longest string field / list kept element-wise, valid JSON) with a truncation note telling the model to narrow its query.
- **Async tools**: All tool functions are `async def` and use `asyncio.to_thread()`, `asyncio.create_subprocess_exec()`, or `_run_sync()` to offload blocking I/O (Kafka, K8s, Docker, HTTP) to thread pool executors.
- **Agent factory function** in `core/orrery_core/agent/base.py`: `create_agent()`. Multi-step orchestration uses ADK 2.0 graph `Workflow`s (see ADR-003), not the deprecated `SequentialAgent`/`ParallelAgent`/`LoopAgent` wrappers.
- **Output keys for data flow**: In multi-agent workflows (like `orrery-assistant`), sub-agents write results to session state via `output_key`; downstream agents read them.
- **RBAC via guardrail metadata**: `authorize()` in `core/orrery_core/security/rbac.py` infers minimum roles from `@destructive`/`@confirm` decorators (admin/operator/viewer). User role is read from `session.state["user_role"]`. Enforced globally via `GuardrailsPlugin`. See `docs/adr/001-rbac.md`.
- **Input validation**: `core/orrery_core/security/validation.py` provides `validate_string()`, `validate_positive_int()`, `validate_url()`, `validate_path()`, `validate_list()` — all tools validate inputs at entry using the walrus operator pattern: `if err := validate_string(...): return err`.
- **Guardrails as decorators**: `@destructive(reason)` and `@confirm(reason)` attach metadata to tool functions. `GuardrailsPlugin` reads this metadata at runtime. Confirmations use args-hash + TTL to prevent bypass. Two modes: model-mediated (the default for bare `AgentGateway`/`adk web`/evals — a re-call in a new invocation counts as confirmed) and **requester-verified** (`AgentGateway(verified_confirmation=True)` — enabled on every shipped exposition: HTTP server, persistent runner, Slack bot, Google Chat bot) where the gate additionally requires a human decision recorded by the gateway — a deliberate word (`approve`/`confirm`/`proceed`/`go ahead`; a casual "ok"/"yes" doesn't count, deny is broad) sent by the *same verified actor* who triggered the pending action. Fail-closed: unknown requester or a second person's approval is refused. The Slack/Google Chat bots gate through their own Approve/Deny buttons instead (`slack_confirmation` / `google_chat_confirmation`); their click handlers enforce the same requester-only rule (`approval_refusal` / `_refuse_non_requester`), while Deny stays open to anyone.
- **Per-turn caller identity**: `create_agent()` wraps every instruction in an `identity_aware_instruction` provider that appends "who you are talking to" when a transport stamped the turn's `actor` into state (`AgentGateway` stamps `msg.user_id` automatically; `_auth.subject` is the fallback) — so in shared threads the model acts for the current sender and never reports a tool's service account as the user. Side effect: instructions are used **verbatim** (no `{var}` state templating — literal braces are safe). Tests read prompt text via `base_instruction(agent)`.
- **Authentication enforcement**: `set_user_role()` marks roles as server-trusted. `GuardrailsPlugin` calls `ensure_default_role()` via `before_agent_callback` to force `viewer` if the role wasn't set by the server, preventing privilege escalation.
- **Structured JSON logging**: `setup_logging()` configures JSON output to stdout (called automatically by `load_agent_env()`). `AuditPlugin` emits tool-call audit entries via the logging system. `ActivityPlugin` records tool calls to session state for cross-agent visibility.
- **Connection pooling**: Kafka `AdminClient`, K8s API clients, and HTTP sessions are cached as module-level singletons to avoid per-call connection overhead.
- **Multi-provider LLM**: `resolve_model()` in `core/orrery_core/agent/base.py` reads `MODEL_PROVIDER` + `MODEL_NAME` env vars. For Gemini returns a string; for others returns `LiteLlm(model=...)`. All agents use this via `create_agent()` — no per-agent changes needed.
- **Reply-text extraction**: All user-facing transports (Google Chat, Slack, HTTP `/chat`, CLI) build the response by funneling runner events through `extract_reply_text()` in `core/orrery_core/serving/events.py`. It concatenates part text but skips ADK "thought" parts (`part.thought is True`) — Gemini native thinking, `PlanReActPlanner` planning phases, and LiteLLM-surfaced provider reasoning are all normalized onto that flag — so planner/thinking output never leaks into a user reply regardless of provider. Add a new transport? Call this helper rather than iterating `content.parts` yourself.
- **Prometheus metrics**: `MetricsPlugin` in `core/orrery_core/plugins/` wraps `MetricsCollector` to track tool call counts, latency histograms, error rates, circuit breaker state, and LLM tokens globally. `start_server(port=9100)` exposes `/metrics` for Prometheus scraping.
- **Distributed tracing (OpenTelemetry)**: `core/orrery_core/observability/tracing.py` provides `configure_tracing()` (installs a global `TracerProvider` → OTLP exporter, idempotent, gated by `OTEL_TRACING_ENABLED`) and `TracingPlugin`. ADK 2.0 already emits native spans for agent/tool/LLM calls under the `gcp.vertex.agent` tracer, so `TracingPlugin` **enriches the current span** (`orrery.request_id`, `orrery.user_role`, `orrery.tool.status`/`result_size`, exception recording) rather than creating duplicate spans; `after_model` only bridges tokens to `track_llm_tokens()` since ADK already sets `gen_ai.usage.*`. `default_plugins(enable_tracing=None)` resolves from `OTEL_TRACING_ENABLED` and prepends the plugin first, so a single env flag turns tracing on across every transport — a missing `[otel]` extra is a skip-with-warning, not a crash. Requires `orrery-core[otel]`; imported lazily (not re-exported from `__init__.py`), mirroring `server.py`/`[server]`. Log↔trace correlation: `JSONFormatter` (`log.py`) stamps `request_id` (a ContextVar, dependency-free) plus `trace_id`/`span_id` (lazy OTel) onto every record. Local stack: `make tracing-up` (Tempo + Grafana under the `tracing` compose profile, with a provisioned `Orrery — Agent Observability` dashboard).
- **Resilience**: `ResiliencePlugin` in `core/orrery_core/plugins/` wraps `CircuitBreaker` for per-tool circuit breaking globally. `@with_retry` decorator adds exponential backoff with jitter to async tool functions.
- **Context caching**: `create_context_cache_config()` in `core/orrery_core/serving/runner.py` creates an ADK `ContextCacheConfig` with env-var defaults (`CONTEXT_CACHE_MIN_TOKENS`, `CONTEXT_CACHE_TTL_SECONDS`, `CONTEXT_CACHE_INTERVALS`). Only effective with Gemini models. Enabled in orrery-assistant via the `App` object.
- **Closed-loop remediation**: the remediation subgraph in `agents/orrery-assistant/orrery_assistant/remediation.py` runs act → verify → retry as `remediation_actor → remediation_verifier → verify_route` wired by a `RoutingMap` (`{"retry": actor, "done": summarizer}`). The verifier calls `mark_remediation_resolved` to signal success; `verify_route` enforces the 3-iteration cap via a state counter (replaces the deprecated `LoopAgent` + `exit_loop`/`escalate`). See [ADR-003](docs/adr/003-graph-workflow-inversion.md).
- **Pydantic-settings config**: Each agent subclasses `AgentConfig` for typed env var loading from `.env` files colocated with the agent module.
- **All tests use mocks**: `@patch` on internal client getters (e.g., `_get_admin_client`). All tool tests are `async` with `@pytest.mark.asyncio`. No running Kafka/K8s/Docker required. Autouse fixtures reset cached clients between tests.
- **Agent evals** (`make eval`): 33 scenarios across 5 specialist agents (kafka, k8s, elasticsearch, observability, docker) using ADK's `AgentEvaluator`. The orrery-assistant root no longer has a routing eval — its graph root is exercised by deterministic unit tests instead (see ADR-003). Each agent has `tests/evals/` with `.test.json` datasets and a `test_*_eval.py` runner. Evals use a real LLM (gated behind `@pytest.mark.eval`) with mocked external dependencies. Criteria: `tool_trajectory_avg_score >= 1.0` (exact tool call match). The whole workspace collects in one `pytest` run via `--import-mode=importlib` (configured in the root `pyproject.toml`), so duplicate test basenames across agents (e.g. `test_app.py`, `test_handler.py`) no longer collide.

### orrery-assistant: chat root + deterministic triage Workflow (ADK 2.0)

There are **two roots** that reuse the same node agents (see
[ADR-003](docs/adr/003-graph-workflow-inversion.md), supersedes ADR-002):

1. **Interactive root** — `orrery_chat_agent`, a chat-mode `LlmAgent`
   (`mode="chat"`, set via `create_agent(mode=...)`). It holds real conversation
   history and routes free-form queries to the six specialist `AgentTool`s
   (kafka/k8s/observability/elasticsearch/docker/ops-journal), plus an
   `incident_triage_agent` `AgentTool` for single-turn full sweeps and a
   `PreloadMemoryTool`. This is the root for `adk web` / CLI / Slack / Chat,
   hosted by `App(root_agent=orrery_chat_agent)`. A chat-mode agent **must** be a
   root — ADK 2.0 forbids it as a routed node inside a graph.
2. **Batch root** — `orrery_triage_workflow`, a graph `Workflow` run by
   `make run-triage` for scheduled/batch incident response. A `Workflow` is not a
   `BaseAgent`, so it can't be an `AgentTool`/sub-agent of the chat root — hence the
   two are separate entrypoints. `create_agent()` LlmAgents are graph **nodes** (an
   `LlmAgent` is a `BaseNode` in ADK 2.0); routing is pure-Python `FunctionNode`s.
   Edges are chain-tuples (sequential), node-tuples (parallel), and `RoutingMap`
   dicts (conditional/loop); a `JoinNode` is the parallel fan-in barrier. Routing
   functions set `ctx.route`; nodes share data via `ctx.state` / `output_key`.
   `runner.py`/`server.py` accept `Agent | Workflow`.

```
orrery_chat_agent (chat-mode LlmAgent, interactive root)
  ├─ AgentTool: kafka / k8s / observability / elasticsearch / docker / ops_journal
  ├─ AgentTool: incident_triage_agent (single-turn full health sweep)
  └─ PreloadMemoryTool

orrery_triage_workflow (Workflow, batch root — `make run-triage`)
  START ─▶ [parallel] kafka/k8s/docker/observability/elasticsearch checkers
        ─▶ health_join (JoinNode, waits for all 5)
        ─▶ triage_summarizer (record_triage_verdict → incident_severity)
        ─▶ journal_writer ─▶ triage_route
              ├─("remediate")─▶ remediation_actor ⇄ remediation_verifier
              │                     └▶ verify_route ─("retry")▶ actor
              │                                     └("done")▶ summarizer ─▶ final_report
              └─("resolved")────────────────────────────────────▶ final_report
```

`triage_route` fails safe: if the LLM skips `record_triage_verdict`, it infers severity
from the per-system status reports and flags `triage_verdict_missing` rather than
silently resolving. The deprecated `create_sequential_agent` / `create_parallel_agent` /
`create_loop_agent` factories were removed — compose multi-step flows as graph
`Workflow`s instead.

## Code Style

- **Ruff** for linting and formatting (line-length: 100, target: py314)
- Lint rules: E, W, F, I (isort), UP, B, SIM
- Known first-party packages configured in `[tool.ruff.lint.isort]`
- CI runs both `ruff check` and `ruff format --check`
