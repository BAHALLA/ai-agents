# Prometheus Metrics

The platform exposes Prometheus metrics for every tool call across all agents. This provides real-time visibility into tool latency, error rates, agent usage, and circuit breaker state.

## Quick Start

Metrics are enabled automatically via `default_plugins()` — no per-agent wiring needed:

```python
from orrery_core import MetricsPlugin, default_plugins
from google.adk.runners import Runner

plugins = default_plugins()
runner = Runner(
    agent=root_agent,
    app_name="my_agent",
    session_service=session_service,
    plugins=plugins,
)

# Expose /metrics on port 9100 (call once at startup).
# The Slack and Google Chat bots do this in their FastAPI lifespan.
metrics_plugin = next(p for p in plugins if isinstance(p, MetricsPlugin))
metrics_plugin.start_server()
```

!!! note "`default_plugins()` does not auto-start the metrics server"
    The `MetricsPlugin` is registered, but the `/metrics` HTTP server is only started when a host calls `start_server()` explicitly. This is intentional — the ADK CLI and tests don't want a port binding. The Slack bot (`agents/slack-bot/slack_bot/app.py`) and Google Chat bot start it in their FastAPI `lifespan`; the persistent runner starts it when `ENABLE_METRICS_SERVER=true`.

## Available Metrics

All metrics use the `orrery_` namespace prefix following [Prometheus naming conventions](https://prometheus.io/docs/practices/naming/).

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `orrery_tool_calls_total` | Counter | `agent`, `tool`, `status` | Total tool invocations with bounded status values |
| `orrery_tool_duration_seconds` | Histogram | `agent`, `tool` | Tool execution latency (buckets: 50ms to 60s) |
| `orrery_tool_errors_total` | Counter | `agent`, `tool`, `error_type` | Errors broken down by exception type |
| `orrery_circuit_breaker_state` | Gauge | `tool` | Circuit breaker state: 0=closed, 1=open, 2=half_open |
| `orrery_llm_tokens_total` | Counter | `agent`, `direction` | LLM token consumption (input/output) |

### Bounded Status Labels

The `status` label on `orrery_tool_calls_total` is restricted to a fixed set of values to prevent [cardinality explosion](https://prometheus.io/docs/practices/naming/#labels):

- `ok` — successful execution (default)
- `success` — explicit success from tool response
- `error` — tool returned an error or raised an exception
- `confirmation_required` — tool blocked by guardrail

Any other status value from a tool response is normalised to `ok`.

## How It Works

`MetricsPlugin` wraps `MetricsCollector` and registers as a global plugin on the `Runner`. It implements three plugin callbacks:

- **`before_tool_callback`** — generates a unique invocation ID and starts a timer
- **`after_tool_callback`** — records duration and success/error status
- **`on_tool_error_callback`** — records error type, duration, and increments error counters

Since plugins apply globally, metrics are automatically collected for every tool across every agent — no per-agent callback wiring needed.

## Circuit Breaker Integration

`default_plugins()` automatically wires the `ResiliencePlugin`'s circuit breaker into `MetricsPlugin`, so the `orrery_circuit_breaker_state` gauge reflects state changes (closed/open/half_open) for each tool.

For custom configurations:

```python
from orrery_core import ResiliencePlugin, MetricsPlugin

resilience = ResiliencePlugin(failure_threshold=3)
metrics = MetricsPlugin(circuit_breaker=resilience.circuit_breaker)
```

## LLM Token Tracking

Use `track_llm_tokens()` to record token consumption from custom LLM wrappers or model callbacks:

```python
from orrery_core import track_llm_tokens

track_llm_tokens("my_agent", input_tokens=150, output_tokens=300)
```

When [distributed tracing](config/general.md#distributed-tracing) is enabled, `TracingPlugin` records the same token counts on each LLM span (`gen_ai.usage.*`) and forwards them to `track_llm_tokens()`, so the Prometheus counter and the trace attributes stay in agreement.

## Distributed Tracing

Metrics answer *how much* and *how often*; traces answer *where* within a single request. OpenTelemetry tracing exports ADK's native agent/tool/LLM spans and adds `request_id`, `user_role`, tool status, and token attributes — and stamps `trace_id`/`request_id` into the JSON logs for log↔trace correlation. It is an opt-in extra (`uv sync --extra otel`) and gated by `OTEL_TRACING_ENABLED`. `make tracing-up` brings up Tempo + Grafana with a provisioned *Orrery — Agent Observability* dashboard that combines these `orrery_*` metrics with a live trace table. See [Distributed Tracing](config/general.md#distributed-tracing) for the full configuration.

## Configuration

| Environment Variable | Default | Description |
|---------------------|---------|-------------|
| `METRICS_PORT` | `9100` | TCP port for the `/metrics` HTTP server |

The port can also be passed explicitly: `metrics_plugin.start_server(port=9200)`.

`start_server()` is safe to call from multiple instances — only the first call in the process starts the HTTP server.

## Prometheus Scraping

The `infra/prometheus.yml` includes a pre-configured scrape job. For **local development** (agent on host, Prometheus in Docker):

```yaml
- job_name: "agents"
  static_configs:
    - targets: ["host.docker.internal:9100"]
      labels:
        service: "orrery-assistant"
```

For **Docker deployment** (agent and Prometheus both in containers):

```yaml
- job_name: "agents"
  static_configs:
    - targets: ["orrery-assistant:9100"]
      labels:
        service: "orrery-assistant"
    - targets: ["slack-bot:9100"]
      labels:
        service: "slack-bot"
```

The `docker-compose.yml` Prometheus service includes `extra_hosts: ["host.docker.internal:host-gateway"]` so the local development config works on Linux.

## Example PromQL Queries

**Tool error rate (5m window):**
```promql
rate(orrery_tool_errors_total[5m])
```

**p95 tool latency by agent:**
```promql
histogram_quantile(0.95, rate(orrery_tool_duration_seconds_bucket[5m]))
```

**Tool calls per minute by tool:**
```promql
rate(orrery_tool_calls_total[1m]) * 60
```

**Circuit breaker state (1 = open):**
```promql
orrery_circuit_breaker_state == 1
```

**LLM tokens consumed per agent (last hour):**
```promql
increase(orrery_llm_tokens_total[1h])
```

## Agents with Metrics Enabled

All agents get metrics automatically through `default_plugins()` registered on the `Runner`. The `MetricsPlugin` applies globally — no per-agent setup needed.

| Deployment | Metrics Server |
|------------|----------------|
| CLI / persistent runner | Started via `metrics_plugin.start_server()` |
| Slack bot | Started in FastAPI lifespan on port 9100 |
| Docker demo | Exposed on port 9100, scraped by Prometheus |
