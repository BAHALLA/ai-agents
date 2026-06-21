# General Configuration

The platform's core behavior, including LLM providers and infrastructure services, is controlled via environment variables.

## LLM Provider

The platform supports multiple LLM providers through [LiteLLM](https://docs.litellm.ai/). Switch providers by setting two environment variables — no code changes needed.

| Variable | Default | Description |
|----------|---------|-------------|
| `MODEL_PROVIDER` | `gemini` | LLM backend: `gemini`, `anthropic`, `openai`, `ollama`, etc. |
| `MODEL_NAME` | `gemini-2.0-flash` | Model identifier (provider prefix auto-added if missing) |

### Provider examples

<details>
<summary>Google Gemini (Default)</summary>

```env
MODEL_PROVIDER=gemini
MODEL_NAME=gemini-2.5-pro
# Either Vertex AI:
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
# Or AI Studio:
GOOGLE_GENAI_USE_VERTEXAI=FALSE
GOOGLE_API_KEY=your-api-key
```
</details>

<details>
<summary>Anthropic Claude</summary>

```env
MODEL_PROVIDER=anthropic
MODEL_NAME=anthropic/claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-api03-...
```
</details>

<details>
<summary>OpenAI</summary>

```env
MODEL_PROVIDER=openai
MODEL_NAME=openai/gpt-4o
OPENAI_API_KEY=sk-...
```
</details>

<details>
<summary>Ollama (Local)</summary>

```env
MODEL_PROVIDER=ollama
MODEL_NAME=ollama/llama3
OLLAMA_API_BASE=http://localhost:11434
```
</details>

### Getting API keys

| Provider | How to get a key | Env var |
|----------|-----------------|---------|
| **Google AI Studio** | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) | `GOOGLE_API_KEY` |
| **Google Vertex AI** | GCP Project + `gcloud auth application-default login` | `GOOGLE_CLOUD_PROJECT` |
| **Anthropic** | [console.anthropic.com](https://console.anthropic.com/settings/keys) | `ANTHROPIC_API_KEY` |
| **OpenAI** | [platform.openai.com](https://platform.openai.com/api-keys) | `OPENAI_API_KEY` |
| **Ollama** | Install [Ollama](https://ollama.com/), run `ollama pull llama3` | N/A |

---

## Context Caching

Context caching reduces token usage and latency by caching static system instructions (agent descriptions, tool schemas, RBAC rules) across requests. This is only effective with **Gemini models** — when using Claude/OpenAI via LiteLLM, the config is accepted but has no effect.

| Variable | Default | Description |
|----------|---------|-------------|
| `CONTEXT_CACHE_MIN_TOKENS` | `2048` | Only cache if context exceeds this token count |
| `CONTEXT_CACHE_TTL_SECONDS` | `600` | Cache lifetime in seconds (10 minutes) |
| `CONTEXT_CACHE_INTERVALS` | `10` | Max invocations before cache refresh |

Context caching is enabled by default in the `orrery-assistant` agent. You can tune the values via environment variables or disable it by not passing a `context_cache_config` to `run_persistent()`.

Cache hit/miss events are exposed as the `orrery_context_cache_events_total` Prometheus counter on the `/metrics` endpoint.

---

## Distributed Tracing

OpenTelemetry tracing complements the Prometheus metrics by following a single request *through* the system — `chat agent → specialist AgentTool → tool → LLM → external system` — so you can attribute latency and localize failures across the agent hierarchy. ADK 2.0 already emits spans for agent, tool, and LLM calls; the platform's job is to export them and enrich them with orrery context.

![Trace waterfall in Grafana Tempo showing an orrery invocation routing through the chat agent, an LLM call, the k8s_health_agent AgentTool, and its nested tool calls](../images/tracing-trace-waterfall.png)

*A real `orrery` turn in Grafana Tempo: the 27s invocation fans out through `orrery_chat_agent` → `call_llm` → the `k8s_health_agent` AgentTool → its own `call_llm` and `execute_tool` spans (`get_cluster_info`, `get_nodes`, `get_events`). The span widths make the latency hotspot obvious at a glance.*

Tracing requires the `otel` extra and is **off by default** — when `OTEL_TRACING_ENABLED` is unset or `false`, no provider is installed and `TracingPlugin` is skipped, so there is zero overhead.

```bash
uv sync --extra otel
```

| Variable | Default | Description |
|----------|---------|-------------|
| `OTEL_TRACING_ENABLED` | `false` | Master switch. Must be `true` for any tracing to occur. |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP gRPC collector endpoint (e.g. `http://localhost:4317`). When empty, spans print to the console — useful for local debugging. |
| `OTEL_SERVICE_NAME` | `orrery` | `service.name` resource attribute attached to every span. |
| `OTEL_TRACES_SAMPLER_ARG` | `1.0` | Head-sampling ratio `0.0`–`1.0`. Uses a `ParentBased` sampler, so a trace already sampled upstream is always kept. |

Tracing is **env-driven**: `default_plugins()` reads `OTEL_TRACING_ENABLED` automatically, so every transport (Google Chat, Slack, the HTTP server, the persistent runner) picks it up from that single flag — no per-agent code change needed. When enabled, `default_plugins()` calls `configure_tracing()` and prepends `TracingPlugin` first in the chain so it wraps every downstream agent, tool, and LLM call.

```python
from orrery_core import default_plugins

# enable_tracing defaults to None -> resolved from OTEL_TRACING_ENABLED.
plugins = default_plugins()

# Pass an explicit bool only to force it regardless of the env var:
plugins = default_plugins(enable_tracing=True)
```

If `OTEL_TRACING_ENABLED=true` but the `otel` extra isn't installed, tracing is skipped with a warning rather than crashing.

`TracingPlugin` does **not** create its own spans — that would duplicate ADK's. Instead it annotates the active span with `orrery.request_id`, `orrery.user_role`, `orrery.tool.status`/`result_size`, and `gen_ai.usage.*` token counts (kept consistent with the `orrery_llm_tokens_total` metric), and records exceptions on tool/model errors.

### Log ↔ trace correlation

Every log line emitted while handling a request carries a `request_id`, and — when a span is active — the `trace_id` and `span_id`, so you can pivot from a JSON log line straight to the matching trace in Tempo/Jaeger. The `request_id` works even without the `otel` extra installed.

### Local tracing stack

`make tracing-up` starts Tempo (OTLP ingest + storage) and Grafana (visualization, pre-wired to Tempo and Prometheus) under the `tracing` compose profile:

```bash
make tracing-up                                      # Tempo :4317/:3200, Grafana :3001
OTEL_TRACING_ENABLED=true \
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
make run-assistant                                   # spans flow to Tempo
make tracing-down
```

Open Grafana at [http://localhost:3001](http://localhost:3001) (anonymous admin). It ships with two provisioned artifacts:

- **Datasources** — Tempo (traces) and Prometheus (metrics), with Tempo's trace→metrics link pre-wired so you can pivot from a span to the `orrery_*` metrics for its service.
- **Dashboard** — *Orrery — Agent Observability* (in the **Orrery** folder): tool call rate, p95 tool latency, error rate by type, LLM tokens/s, circuit-breaker state, and a live table of recent `service.name = orrery` traces. Click any Trace ID to open the full span tree — slow turns are usually dominated by a single large `call_llm` span, which is the cue to trim what a tool feeds back to the model.

---

## Planning

The reasoning-heavy agents in `orrery-assistant` (the root orchestrator, `triage_summarizer`, and `remediation_actor`) accept an optional [ADK planner](https://adk.dev/agents/llm-agents/) that injects an explicit reasoning step before tool calls. Planning is **opt-in** and **off by default** — setting `ORRERY_PLANNER` is the only knob you need.

| Variable | Default | Description |
|----------|---------|-------------|
| `ORRERY_PLANNER` | `none` | Planner choice: `none`, `plan_react`, or `builtin`. |
| `ORRERY_PLANNER_THINKING_BUDGET` | unset | Integer token budget for `builtin` (Gemini-only). |
| `ORRERY_PLANNER_INCLUDE_THOUGHTS` | `true` | Whether `builtin` surfaces the model's thoughts. |

**Choosing a planner:**

- `plan_react` is **provider-agnostic** — works with every backend `MODEL_PROVIDER` supports (Gemini, Claude, OpenAI, Ollama via LiteLLM). The model output is structured into `/*PLANNING*/`, `/*ACTION*/`, `/*REASONING*/`, and `/*FINAL_ANSWER*/` phases. Use this if you run on anything other than Gemini, or if you want plan steps you can surface in a UI (the Google Chat progress card already keys off `state_delta` writes, so the additional structure shows up naturally).
- `builtin` uses **Gemini's native thinking tokens** via `BuiltInPlanner(ThinkingConfig(...))`. Cheaper and lower-latency than `plan_react` on Gemini, with no output-shape change. Falls back to no planner (with a warning) when `MODEL_PROVIDER != gemini`, since LiteLLM-routed models do not consume the ADK thinking config.

**Tradeoffs to be aware of:**

- `plan_react` makes responses noticeably more verbose and adds an extra reasoning round-trip per turn — A/B-test before flipping it on for latency-sensitive surfaces.
- Planners are intentionally **not** attached to per-system health checkers, the remediation verifier, or the journal writer. Those agents execute one short tool sequence per turn; planning would add latency without changing the output.
- **Reasoning never leaks to users.** Whatever planner you choose, the model's reasoning (`builtin` thought tokens, `plan_react` `/*PLANNING*/`/`/*REASONING*/` phases, and provider reasoning surfaced through LiteLLM) is marked as a *thought* part by ADK. Every user-facing transport — Google Chat, Slack, the HTTP `/chat` API, and the CLI — funnels event text through `extract_reply_text()` (`orrery_core.events`), which drops thought parts and emits only the final answer. So `ORRERY_PLANNER_INCLUDE_THOUGHTS=true` makes thoughts available for tracing/progress without showing them in the reply.

---

## Infrastructure

The included `docker-compose.yml` starts the local diagnostic stack.

| Service | Port | Description |
|---------|------|-------------|
| Kafka | `9092` | Kafka broker (running in KRaft mode) |
| PostgreSQL | `5432` | Shared session storage for agents |
| Kafka UI | `8080` | Web UI for browsing topics and consumer groups |
| Kafka Exporter | `9308` | Prometheus exporter for Kafka metrics |
| Prometheus | `9090` | Metrics collection and alerting rules |
| Loki | `3100` | Log aggregation |
| Alertmanager | `9093` | Alert routing and silence management |
| Tempo | `4317` / `3200` | OTLP span ingest + Tempo query API (`tracing` profile) |
| Grafana | `3001` | Trace/metric visualization (`tracing` profile) |
| Elasticsearch | `9200` | Elasticsearch REST endpoint |
| Kibana | `5601` | Kibana web UI |

### Management Commands

```bash
make infra-up     # start all services
make infra-down   # stop all services
make infra-reset  # stop and wipe volumes
make tracing-up   # start the tracing stack (Tempo + Grafana)
make tracing-down # stop the tracing stack
```

### Docker Compose profiles

| Command | What it starts |
|---------|---------------|
| `docker compose up -d` | Infrastructure only |
| `docker compose --profile demo up -d` | Infrastructure + orrery-assistant web UI on `:8000` |
| `docker compose --profile slack up -d` | Infrastructure + Slack bot on `:3000` |
| `docker compose --profile tracing up -d` | Tempo (OTLP `:4317`) + Grafana (`:3001`) |
| `docker compose --profile elastic up -d` | Elasticsearch + Kibana |
