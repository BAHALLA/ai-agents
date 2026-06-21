# ── Build stage ───────────────────────────────────────────────────────
FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

WORKDIR /app

# Production-oriented uv settings: compile bytecode for faster cold starts,
# and copy (rather than hardlink) so the venv is portable across layers.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Install workspace dependencies (cached layer).
# Copy all pyproject files first so uv can resolve the workspace
# independently of source code changes.
COPY pyproject.toml uv.lock ./
COPY core/pyproject.toml core/pyproject.toml
COPY agents/docker-agent/pyproject.toml agents/docker-agent/pyproject.toml
COPY agents/elasticsearch/pyproject.toml agents/elasticsearch/pyproject.toml
COPY agents/kafka-health/pyproject.toml agents/kafka-health/pyproject.toml
COPY agents/k8s-health/pyproject.toml agents/k8s-health/pyproject.toml
COPY agents/orrery-assistant/pyproject.toml agents/orrery-assistant/pyproject.toml
COPY agents/observability/pyproject.toml agents/observability/pyproject.toml
COPY agents/ops-journal/pyproject.toml agents/ops-journal/pyproject.toml
COPY agents/slack-bot/pyproject.toml agents/slack-bot/pyproject.toml
COPY agents/google-chat-bot/pyproject.toml agents/google-chat-bot/pyproject.toml

# Placeholder packages so `uv sync` can resolve the workspace before
# real source is copied in.
RUN mkdir -p core/orrery_core && touch core/orrery_core/__init__.py && \
    mkdir -p agents/docker-agent/docker_agent && touch agents/docker-agent/docker_agent/__init__.py && \
    mkdir -p agents/elasticsearch/elasticsearch_agent && touch agents/elasticsearch/elasticsearch_agent/__init__.py && \
    mkdir -p agents/kafka-health/kafka_health_agent && touch agents/kafka-health/kafka_health_agent/__init__.py && \
    mkdir -p agents/k8s-health/k8s_health_agent && touch agents/k8s-health/k8s_health_agent/__init__.py && \
    mkdir -p agents/orrery-assistant/orrery_assistant && touch agents/orrery-assistant/orrery_assistant/__init__.py && \
    mkdir -p agents/observability/observability_agent && touch agents/observability/observability_agent/__init__.py && \
    mkdir -p agents/ops-journal/ops_journal_agent && touch agents/ops-journal/ops_journal_agent/__init__.py && \
    mkdir -p agents/slack-bot/slack_bot && touch agents/slack-bot/slack_bot/__init__.py && \
    mkdir -p agents/google-chat-bot/google_chat_bot && touch agents/google-chat-bot/google_chat_bot/__init__.py

# Cache mount keeps repeated builds fast without bloating the final image.
# `--extra postgres` pulls in the Postgres driver used by the session store.
# `--extra server` pulls in FastAPI, Uvicorn, and PyJWT for the authenticated HTTP front door.
# `--extra otel` pulls in OpenTelemetry so OTEL_TRACING_ENABLED works in the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen --extra postgres --extra server --extra otel

# Copy actual source code
COPY core/ core/
COPY agents/ agents/

# Reinstall workspace packages with real source
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --no-dev --frozen --extra postgres --extra server --extra otel

# ── Runtime stage ─────────────────────────────────────────────────────
FROM python:3.14-slim-bookworm

RUN groupadd --gid 1000 appuser && \
    useradd --uid 1000 --gid appuser --create-home appuser

WORKDIR /app

# Docker CLI for container monitoring (used by docker-agent tools).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker

# Copy the virtual environment and source from builder with correct ownership
# in a single layer (avoids a separate `chown -R` that would double disk use).
COPY --from=builder --chown=appuser:appuser /app/.venv /app/.venv
COPY --from=builder --chown=appuser:appuser /app/core/ /app/core/
COPY --from=builder --chown=appuser:appuser /app/agents/ /app/agents/

USER appuser

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default: run the orrery-assistant orchestrator with web UI.
# Individual services (Slack bot, Google Chat bot, Pub/Sub worker) override
# this via `command:` in docker-compose or the Kubernetes manifest.
# Health/readiness probes are owned by the orchestrator (docker-compose,
# Helm values in deploy/helm/orrery-assistant/values.yaml) rather than
# baked in here, because the /healthz server only runs for services that
# start HealthServer (run_persistent, Pub/Sub worker) — not `adk web`.
EXPOSE 8000 8080
CMD ["adk", "web", "--host", "0.0.0.0", "--port", "8000", "agents/orrery-assistant"]
