# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Build & Development Commands

```bash
make install          # Install all workspace packages (uv sync)
make test             # Run all 700 unit tests across all packages
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
make docker-build && make docker-demo
```

Run individual agents (each has `make run-<name>`, `make run-<name>-cli`, some have `-persistent`):
```bash
make run-devops       # ADK Dev UI (in-memory)
make run-devops-cli   # Terminal mode
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

- **Plugins over per-agent callbacks**: Cross-cutting concerns (RBAC, guardrails, metrics, audit, activity tracking, resilience, error handling) are packaged as ADK `BasePlugin` subclasses in `core/orrery_core/plugins.py` and registered once on the `Runner` via `default_plugins()`. Plugins apply globally to every agent, tool, and LLM call — no per-agent callback wiring needed.
- **Async tools**: All tool functions are `async def` and use `asyncio.to_thread()`, `asyncio.create_subprocess_exec()`, or `_run_sync()` to offload blocking I/O (Kafka, K8s, Docker, HTTP) to thread pool executors.
- **Agent factory function** in `core/orrery_core/base.py`: `create_agent()`. Multi-step orchestration uses ADK 2.0 graph `Workflow`s (see ADR-003), not the deprecated `SequentialAgent`/`ParallelAgent`/`LoopAgent` wrappers.
- **Output keys for data flow**: In multi-agent workflows (like `orrery-assistant`), sub-agents write results to session state via `output_key`; downstream agents read them.
- **RBAC via guardrail metadata**: `authorize()` in `core/orrery_core/rbac.py` infers minimum roles from `@destructive`/`@confirm` decorators (admin/operator/viewer). User role is read from `session.state["user_role"]`. Enforced globally via `GuardrailsPlugin`. See `docs/adr/001-rbac.md`.
- **Input validation**: `core/orrery_core/validation.py` provides `validate_string()`, `validate_positive_int()`, `validate_url()`, `validate_path()`, `validate_list()` — all tools validate inputs at entry using the walrus operator pattern: `if err := validate_string(...): return err`.
- **Guardrails as decorators**: `@destructive(reason)` and `@confirm(reason)` attach metadata to tool functions. `GuardrailsPlugin` reads this metadata at runtime. Confirmations use args-hash + TTL to prevent bypass.
- **Authentication enforcement**: `set_user_role()` marks roles as server-trusted. `GuardrailsPlugin` calls `ensure_default_role()` via `before_agent_callback` to force `viewer` if the role wasn't set by the server, preventing privilege escalation.
- **Structured JSON logging**: `setup_logging()` configures JSON output to stdout (called automatically by `load_agent_env()`). `AuditPlugin` emits tool-call audit entries via the logging system. `ActivityPlugin` records tool calls to session state for cross-agent visibility.
- **Connection pooling**: Kafka `AdminClient`, K8s API clients, and HTTP sessions are cached as module-level singletons to avoid per-call connection overhead.
- **Multi-provider LLM**: `resolve_model()` in `core/orrery_core/base.py` reads `MODEL_PROVIDER` + `MODEL_NAME` env vars. For Gemini returns a string; for others returns `LiteLlm(model=...)`. All agents use this via `create_agent()` — no per-agent changes needed.
- **Prometheus metrics**: `MetricsPlugin` in `core/orrery_core/plugins.py` wraps `MetricsCollector` to track tool call counts, latency histograms, error rates, circuit breaker state, and LLM tokens globally. `start_server(port=9100)` exposes `/metrics` for Prometheus scraping.
- **Resilience**: `ResiliencePlugin` in `core/orrery_core/plugins.py` wraps `CircuitBreaker` for per-tool circuit breaking globally. `@with_retry` decorator adds exponential backoff with jitter to async tool functions.
- **Context caching**: `create_context_cache_config()` in `core/orrery_core/runner.py` creates an ADK `ContextCacheConfig` with env-var defaults (`CONTEXT_CACHE_MIN_TOKENS`, `CONTEXT_CACHE_TTL_SECONDS`, `CONTEXT_CACHE_INTERVALS`). Only effective with Gemini models. Enabled in orrery-assistant via the `App` object.
- **Closed-loop remediation**: the remediation subgraph in `agents/orrery-assistant/orrery_assistant/remediation.py` runs act → verify → retry as `remediation_actor → remediation_verifier → verify_route` wired by a `RoutingMap` (`{"retry": actor, "done": summarizer}`). The verifier calls `mark_remediation_resolved` to signal success; `verify_route` enforces the 3-iteration cap via a state counter (replaces the deprecated `LoopAgent` + `exit_loop`/`escalate`). See [ADR-003](docs/adr/003-graph-workflow-inversion.md).
- **Pydantic-settings config**: Each agent subclasses `AgentConfig` for typed env var loading from `.env` files colocated with the agent module.
- **All tests use mocks**: `@patch` on internal client getters (e.g., `_get_admin_client`). All tool tests are `async` with `@pytest.mark.asyncio`. No running Kafka/K8s/Docker required. Autouse fixtures reset cached clients between tests.
- **Agent evals** (`make eval`): 33 scenarios across 5 specialist agents (kafka, k8s, elasticsearch, observability, docker) using ADK's `AgentEvaluator`. The orrery-assistant root no longer has a routing eval — its graph root is exercised by deterministic unit tests instead (see ADR-003). Each agent has `tests/evals/` with `.test.json` datasets and a `test_*_eval.py` runner. Evals use a real LLM (gated behind `@pytest.mark.eval`) with mocked external dependencies. Criteria: `tool_trajectory_avg_score >= 1.0` (exact tool call match). Eval test files must have unique names across agents to avoid pytest import collisions.

### orrery-assistant Hybrid Graph Root (ADK 2.0 Workflow)

The root is a graph-based `Workflow` (`orrery_assistant.agent.orrery_workflow`). An
`intent_router` `FunctionNode` dispatches each turn: explicit "run a full triage"
requests take a deterministic incident-response pipeline; everything else falls through
to `orrery_chat_agent` — the conversational LLM orchestrator that routes free-form
queries to the six specialist `AgentTool`s (kafka/k8s/observability/elasticsearch/
docker/ops-journal) plus memory. Existing `create_agent()` LlmAgents are graph **nodes**
(an `LlmAgent` is a `BaseNode` in ADK 2.0); routing is pure-Python `FunctionNode`s. Edges
are chain-tuples (sequential), node-tuples (parallel), and `RoutingMap` dicts
(conditional/loop); a `JoinNode` is the parallel fan-in barrier. See
[ADR-003](docs/adr/003-graph-workflow-inversion.md) (supersedes ADR-002). Routing
functions set `ctx.route`; nodes share data via `ctx.state` / `output_key`. The graph is
hosted by `App(root_agent=orrery_workflow)` — `runner.py`/`server.py` accept `Agent | Workflow`.

```
orrery_workflow (Workflow root)
  START ─▶ intent_router
       ├─("chat", default)─▶ orrery_chat_agent (LLM + 6 specialist AgentTools + memory)
       └─("triage")─▶ [parallel] kafka/k8s/docker/observability/elasticsearch checkers
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
