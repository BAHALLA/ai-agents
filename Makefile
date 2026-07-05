.PHONY: help install test eval lint type-check ty fmt check clean \
       infra-up infra-down infra-reset tracing-up tracing-down \
       docs-serve docs-build docs-deploy \
       run-assistant run-assistant-api run-assistant-cli run-assistant-persistent run-triage \
       _ensure-dev-jwt-secret dev-token dev-token-viewer dev-token-operator dev-token-admin \
       rotate-dev-jwt-secret print-dev-jwt-config \
       run-slack-bot run-slack-bot-socket run-google-chat run-google-chat-pubsub

# ── Local dev auth configuration ───────────────────────
# A persistent JWT secret for local-only dev work. Stored outside the
# repo so it can't be accidentally committed. Both `run-assistant-api`
# (which boots the authenticated server) and `dev-token` (which mints
# test tokens) read from this file, so a token minted in one terminal
# always validates against the server running in another.
DEV_JWT_SECRET_FILE := $(HOME)/.cache/orrery/jwt-secret
DEV_JWT_AUDIENCE    := orrery-local
DEV_JWT_ISSUER      := https://dev.local

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-24s\033[0m %s\n", $$1, $$2}'

# ── Setup & quality gate ───────────────────────────────

install: ## Install all workspace packages
	uv sync --all-extras

test: ## Run all tests (excludes evals)
	uv run pytest -v

eval: ## Run agent evaluation tests (requires LLM access)
	uv run pytest -m eval -v

lint: ## Run linter checks (ruff check + format check)
	uv run ruff check .
	uv run ruff format --check .

# Derived from the workspace members (core + every agents/* with a pyproject),
# so a newly added agent is picked up automatically — no manual edit needed.
TY_SEARCH_PATHS := $(addprefix --extra-search-path ,core $(patsubst %/,%,$(dir $(wildcard agents/*/pyproject.toml))))

type-check: ## Run type checks (ty)
	uv run ty check $(TY_SEARCH_PATHS) .

ty: type-check ## Alias for type-check

fmt: ## Auto-fix lint and format issues
	uv run ruff check --fix .
	uv run ruff format .

check: lint ty test ## Run the full quality gate, verify-only (lint + ty + test — mirrors CI; run `make fmt` first to auto-fix)

clean: ## Remove Python caches, tool caches, and build artifacts (keeps .venv)
	@echo "▶ Cleaning build artifacts and caches…"
	find . -type d -name '__pycache__' -not -path './.venv/*' -prune -exec rm -rf {} +
	find . -type d -name '*.egg-info' -not -path './.venv/*' -prune -exec rm -rf {} +
	find . -type d -name 'build' -not -path './.venv/*' -prune -exec rm -rf {} +
	find . -type d -name '.adk' -not -path './.venv/*' -prune -exec rm -rf {} +
	find . -type f -name '*.py[co]' -not -path './.venv/*' -delete
	rm -rf .pytest_cache .ruff_cache .hypothesis .mypy_cache .coverage htmlcov dist site
	@echo "▶ Clean. (.venv preserved — use 'rm -rf .venv' for a full reset.)"

# ── Infrastructure ─────────────────────────────────────

infra-up: ## Start shared infrastructure (Kafka, Postgres, Prometheus, Loki, Alertmanager)
	docker compose up -d

infra-down: ## Stop shared infrastructure
	docker compose down

infra-reset: ## Stop infrastructure and wipe volumes
	docker compose down -v

tracing-up: ## Start the tracing stack (Tempo + Grafana on :3001)
	docker compose --profile tracing up -d tempo grafana

tracing-down: ## Stop the tracing stack
	docker compose --profile tracing down

# ── Documentation ──────────────────────────────────────

docs-serve: ## Serve documentation locally
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs serve

docs-build: ## Build documentation site
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs build

docs-deploy: ## Deploy documentation to GitHub Pages
	DISABLE_MKDOCS_2_WARNING=true uv run mkdocs gh-deploy --force

# ── Orchestrator (orrery-assistant) ────────────────────
# The orchestrator composes every specialist agent (kafka / k8s /
# observability / elasticsearch / docker / ops-journal), so run it
# directly rather than each agent standalone.

run-assistant: ## Launch orrery-assistant in ADK Dev UI
	cd agents/orrery-assistant && ENABLE_METRICS_SERVER=true uv run adk web

run-assistant-cli: ## Run orrery-assistant in terminal
	cd agents/orrery-assistant && ENABLE_METRICS_SERVER=true uv run adk run orrery_assistant

run-assistant-persistent: ## Run orrery-assistant with a persistent store (Postgres via DATABASE_URL, else in-memory)
	cd agents/orrery-assistant && ENABLE_METRICS_SERVER=true uv run python run_persistent.py

run-triage: ## Run the deterministic triage Workflow once (batch / scheduled)
	cd agents/orrery-assistant && uv run python run_triage.py

_ensure-dev-jwt-secret:
	@if [ ! -f $(DEV_JWT_SECRET_FILE) ]; then \
		mkdir -p $$(dirname $(DEV_JWT_SECRET_FILE)) && \
		openssl rand -hex 32 > $(DEV_JWT_SECRET_FILE) && \
		chmod 600 $(DEV_JWT_SECRET_FILE) && \
		echo "▶ Generated dev JWT secret at $(DEV_JWT_SECRET_FILE)"; \
	fi

run-assistant-api: _ensure-dev-jwt-secret ## Run orrery-assistant FastAPI front door (auth ON, dev secret)
	@echo "▶ Auth enabled — mint a token in another terminal with: make dev-token"
	cd agents/orrery-assistant && \
		AUTH_ENABLED=true \
		JWT_ALGORITHM=HS256 \
		JWT_SECRET=$$(cat $(DEV_JWT_SECRET_FILE)) \
		JWT_AUDIENCE=$(DEV_JWT_AUDIENCE) \
		JWT_ISSUER=$(DEV_JWT_ISSUER) \
		ENABLE_METRICS_SERVER=true \
		uv run uvicorn orrery_assistant.app:api --host 0.0.0.0 --port 8000

dev-token: _ensure-dev-jwt-secret ## Mint a JWT for local testing (ROLE=viewer|operator|admin, default admin)
	@JWT_AUDIENCE=$(DEV_JWT_AUDIENCE) \
	JWT_ISSUER=$(DEV_JWT_ISSUER) \
		uv run python scripts/dev_token.py \
			--role $(or $(ROLE),admin) \
			--secret-file $(DEV_JWT_SECRET_FILE)

dev-token-viewer: _ensure-dev-jwt-secret ## Mint a viewer-role JWT
	@$(MAKE) --no-print-directory dev-token ROLE=viewer

dev-token-operator: _ensure-dev-jwt-secret ## Mint an operator-role JWT
	@$(MAKE) --no-print-directory dev-token ROLE=operator

dev-token-admin: _ensure-dev-jwt-secret ## Mint an admin-role JWT
	@$(MAKE) --no-print-directory dev-token ROLE=admin

print-dev-jwt-config: _ensure-dev-jwt-secret ## Show the dev auth config (secret path, audience, issuer)
	@echo "Secret file: $(DEV_JWT_SECRET_FILE)"
	@echo "Audience:    $(DEV_JWT_AUDIENCE)"
	@echo "Issuer:      $(DEV_JWT_ISSUER)"

rotate-dev-jwt-secret: ## Regenerate the dev JWT secret (invalidates existing tokens)
	@rm -f $(DEV_JWT_SECRET_FILE)
	@$(MAKE) --no-print-directory _ensure-dev-jwt-secret

# ── Chat transports (orchestrator over Slack / Google Chat) ──

run-slack-bot: ## Run the Slack bot (FastAPI + slack-bolt on :3000)
	cd agents/slack-bot && uv run uvicorn slack_bot.app:api --host 0.0.0.0 --port 3000

run-slack-bot-socket: ## Run the Slack bot in Socket Mode (no public URL needed)
	cd agents/slack-bot && uv run python -m slack_bot

run-google-chat: ## Run the Google Chat bot (FastAPI on :3001)
	cd agents/google-chat-bot && uv run uvicorn google_chat_bot.app:api --host 0.0.0.0 --port 3001

run-google-chat-pubsub: ## Run the Google Chat bot in Pub/Sub mode (private GKE friendly)
	cd agents/google-chat-bot && uv run python -m google_chat_bot.pubsub_worker
