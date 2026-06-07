# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Graph-Based Workflow Root (ADR-003)**: The orchestrator root is now a deterministic ADK 2.0 `Workflow` graph. An `intent_router` dynamically dispatches turns to either a conversational LLM orchestrator (`orrery_chat_agent`) or a deterministic incident triage pipeline.
- **Remediation Subgraph**: The self-healing loop is now expressed via explicit graph edges (`remediation_actor` → `remediation_verifier` → `verify_route`), strictly bounded by a state counter instead of the legacy `LoopAgent`.

### Removed
- **Deprecated Agent Factories**: Removed `create_sequential_agent`, `create_parallel_agent`, and `create_loop_agent` from `orrery_core.base` as they are deprecated in ADK 2.0 in favor of the `Workflow` API.
- **Legacy Routing Evals**: Removed `planner_routing` eval which asserted the old `AgentTool` LLM routing that is now handled by the graph's `intent_router`.

### Security
- **CVE remediation across the dependency graph**: Bumped `litellm` `1.82.6 → 1.83.14` to address 7 advisories — 2 CRITICAL (CVE-2026-35030 OIDC auth bypass / privilege escalation, CVE-2026-42208 SQL injection) and 5 HIGH (CVE-2026-35029 RCE via unrestricted proxy config, CVE-2026-40217 arbitrary code execution via bytecode rewriting, CVE-2026-42203 SSTI in `/prompts/test`, CVE-2026-42271 authenticated command execution via MCP stdio test endpoints, GHSA-69x8-hrgq-fjj8 password hash exposure / pass-the-hash bypass). Five transitive HIGH-severity bumps came along for free: `mako 1.3.10 → 1.3.12` (CVE-2026-44307 Windows path traversal via backslash URI), `pyasn1 0.6.2 → 0.6.3` (CVE-2026-30922 unbounded-recursion DoS), `pyopenssl 25.3.0 → 26.2.0` (CVE-2026-27459 DTLS cookie callback buffer overflow), `python-multipart 0.0.22 → 0.0.28` (CVE-2026-42561 unbounded multipart-header DoS), and `urllib3 2.6.3 → 2.7.0` (CVE-2026-44431 cross-origin sensitive-header forwarding, CVE-2026-44432 decompression-bomb safeguard bypass).
- **Trivy filesystem scan in CI** (`.github/workflows/ci.yml` — security job): New `aquasecurity/trivy-action@0.28.0` step runs `scan-type: fs` with `scanners: vuln,misconfig,secret` against the workspace, gating on `HIGH,CRITICAL` with `ignore-unfixed: true` and `exit-code: 1`. Catches the vulnerable-dependency class that Bandit (a Python static analyser) doesn't see, plus IaC misconfig in `Dockerfile` / `deploy/terraform/`, plus committed secrets.
- **Bandit now scans every file**: Pinned the `uvx` invocation to `--python 3.14` so the AST parser matches our `requires-python` floor. Before this, GitHub's runner Python (3.12) silently skipped `core/orrery_core/rbac.py`, `core/orrery_core/runner.py`, and `agents/google-chat-bot/google_chat_bot/pubsub_worker.py` with "syntax error while parsing AST" — they parse Python 3.14 syntax — meaning those modules weren't being security-scanned at all. The skip is now caught by the report (0 files skipped) and the modules participate in the scan.
- **Hardcoded `/tmp` removed from the test suite** (`core/tests/test_secrets.py:117`): `test_filebackend_satisfies_protocol` now takes pytest's `tmp_path` fixture instead of passing `"/tmp"` to `FileBackend`, resolving the Bandit B108 (`hardcoded_tmp_directory`) Medium finding that was breaking the `bandit -ll` gate.

### Changed
- **Workspace pinned to Linux + macOS resolution environments** (`pyproject.toml` — new `[tool.uv]` block with `environments = ["sys_platform == 'linux'", "sys_platform == 'darwin'"]`): The project depends on Docker daemon, Kubernetes client, and confluent-kafka — none of which are supported on Windows in the form we consume them. Restricting the lock-file environments lets uv resolve a coherent dependency graph instead of failing on phantom Windows-only conflicts (the litellm bump above surfaced one). No runtime change; this only affects what platform combinations uv considers when locking.
- **Relaxed two version floors to satisfy litellm's strict transitive pins**: `pydantic>=2.13.3 → pydantic>=2.12.5` (litellm 1.83.14 hard-pins `pydantic==2.12.5`) and `aiohttp>=3.13.5 → aiohttp>=3.13.3` on `agents/slack-bot` (litellm 1.83.10–13 pin `aiohttp==3.13.3`, 1.83.14 pins `==3.13.4`). Resolver picks the highest version that satisfies every constraint; in practice the lock holds at the litellm-mandated floors. Tests at 693/693 after the changes — if you adopt pydantic 2.13-specific behavior later, this floor should be raised back.

### Added
- **Authentication layer for the HTTP front door (AEP-013, first slice)** (`core/orrery_core/auth.py`, `core/orrery_core/server.py`, `core/orrery_core/secrets.py`): New `verify_token()` supports both HS256 (shared secret, for dev/gateway-fronted deployments) and RS256/ES256 via JWKS (production IdPs — Auth0, Keycloak, Okta, Google IAP, GitHub OIDC). `JWTConfig.from_env()` reads `JWT_ALGORITHM` / `JWT_SECRET` / `JWT_JWKS_URL` / `JWT_AUDIENCE` / `JWT_ISSUER` / `JWT_ROLE_CLAIM` / `JWT_LEEWAY_SECONDS`. `extract_role()` maps claim lists or space/comma-separated strings to `viewer` / `operator` / `admin` with namespace aliases (`orrery-admin`, `orrery_operator`). New `AuthPlugin` reads the verified `_auth` payload from session state and calls `set_user_role()` so the existing RBAC enforcement becomes trust-rooted. Opt-in via `default_plugins(enable_auth=True)` — zero behavior change for existing deployments.
- **Authenticated FastAPI runner** (`core/orrery_core/server.py`): New `create_app()` mounts the ADK `Runner` behind a `HTTPBearer` dependency. `POST /chat` requires a valid JWT, seeds new sessions with the verified `_auth` payload, and re-stamps long-lived sessions so a re-minted token with a downgraded role applies immediately. `GET /healthz` and `GET /readyz` are auth-free. `auth_enabled=False` is supported for local dev with an explicit warning and pins anonymous callers to `viewer` so RBAC still gates writes. Eager `cfg.jwt.validate()` at startup fails fast on misconfiguration rather than at first request.
- **Pluggable secrets manager** (`core/orrery_core/secrets.py`): `SecretsManager` resolves in priority order — explicit backends → `FileBackend(ORRERY_SECRETS_DIR)` → env vars → caller default. Auto-installs the file backend when the env var points at a real directory, so Kubernetes Secret volumes work with no code changes. `SecretsBackend` Protocol leaves Vault / GCP Secret Manager / AWS Secrets Manager adapters as a future PR.
- **Tests**: 60 new cases across `core/tests/test_auth.py` (extract_role matrix, JWTConfig.validate, HS256 round-trip + signature / audience / issuer / expiry / leeway / missing-`sub` / missing-`exp` failure modes, RS256/JWKS happy path with a mocked `PyJWKClient`, AuthPlugin role-application and forced-viewer fallback), `core/tests/test_secrets.py` (env fallback, file backend trailing-newline handling, backend ordering, misbehaving-backend isolation, auto-registration via `ORRERY_SECRETS_DIR`), and `core/tests/test_server.py` (FastAPI TestClient — `/healthz` is auth-free, missing/invalid/expired tokens return 401 with `WWW-Authenticate: Bearer`, valid tokens dispatch to the runner with the JWT subject as `user_id` and the `_auth` payload seeded, existing sessions re-stamp `_auth` on every request, anonymous-mode pins to `viewer`, eager misconfiguration fails fast). Suite at **693 unit tests** (was 633), lint + format + ty clean.
- **Deps**: New optional extras on `orrery-core` — `[auth]` (PyJWT 2.10+ with `crypto` for RS256/ES256) and `[server]` (auth extras plus FastAPI + Uvicorn). Both surface at the workspace root as `orrery[auth]` / `orrery[server]`. Core itself adds no new required dependencies — anyone using only the Slack / Google Chat transports stays on the existing footprint.

### Documentation
- **`docs/config/security.md`** — new page with the threat model, HS256 vs. RS256/JWKS setup recipes, JWKS endpoints for Auth0 / Keycloak / Okta / Google IAP / GitHub OIDC, role-mapping rules, and the `ORRERY_SECRETS_DIR` Kubernetes Secret-volume pattern. Wired into the mkdocs nav under *Getting Started → Configuration*.
- **`.env.example` — Security / Auth block** filled in with the full variable matrix (`AUTH_ENABLED`, `JWT_ALGORITHM`, `JWT_SECRET`, `JWT_JWKS_URL`, `JWT_AUDIENCE`, `JWT_ISSUER`, `JWT_ROLE_CLAIM`, `JWT_LEEWAY_SECONDS`, `ORRERY_CORS_ORIGINS`, `ORRERY_SECRETS_DIR`) replacing the previous "coming soon" stub.
- **`README.md` — Safety & Governance** bullets added for JWT authentication and the secrets manager.

### Added (previously)
- **Opt-in ADK planners on reasoning-heavy agents** (`core/orrery_core/base.py`, `agents/orrery-assistant`): New `resolve_planner()` helper reads `ORRERY_PLANNER` (`none` | `plan_react` | `builtin`) and `create_agent()` accepts a `planner=` kwarg. Three agents in `orrery-assistant` opt in — the root orchestrator, the `triage_summarizer`, and the `remediation_actor` — sharing a single planner instance resolved once at import time. `plan_react` works across Gemini/Claude/OpenAI/Ollama via the existing LiteLLM integration; `builtin` consumes Gemini's native thinking tokens (`ORRERY_PLANNER_THINKING_BUDGET`, `ORRERY_PLANNER_INCLUDE_THOUGHTS`) and falls back to no planner with a warning when `MODEL_PROVIDER != gemini`. Default `ORRERY_PLANNER=none` is a no-op — zero behavior change for existing deployments. Per-system health checkers, the remediation verifier, and the journal writer intentionally skip the planner; they execute one short tool sequence per turn so an extra reasoning pass would only add latency. Tool-leaf agents (e.g., `kafka_health_checker`, `k8s_health_checker`) remain planner-free.
- **Tests**: 10 new cases in `core/tests/test_base.py::TestResolvePlanner` covering default-off, both planner choices, the Gemini-only fallback for `builtin`, the `ORRERY_PLANNER_THINKING_BUDGET` / `ORRERY_PLANNER_INCLUDE_THOUGHTS` knobs, unknown-value warning, and case-insensitivity. Plus 5 deterministic wiring tests in `agents/orrery-assistant/tests/test_planner_wiring.py` that reload the agent module across env-var permutations to assert (a) which three agents pick up the planner under `plan_react` / `builtin`, (b) that all seven tool-leaf agents — the five health checkers, the journal writer, and the remediation verifier — stay planner-free, and (c) that `builtin` falls back to no planner under non-Gemini providers. Suite at **633 unit tests** (was 618), lint + format clean.
- **Agent-level eval scaffolding for the planner-enabled root** (`agents/orrery-assistant/tests/evals/planner_routing.test.json`, `agents/orrery-assistant/tests/test_orrery_eval.py`): Two routing scenarios — narrow Kafka query and narrow Elasticsearch query — that exercise the full `ORRERY_PLANNER=plan_react` → root → `AgentTool` → mocked client path against a real LLM. Gated behind `make eval`; skips when no Gemini credentials are present. Mocks `kafka_health_agent.tools._get_admin_client` and `elasticsearch_agent.tools._get_session` at the same layer the per-specialist evals use. The accompanying `test_config.json` ships with smoke-test thresholds (0.0) because the AgentTool injects an LLM-generated `request` arg that defeats strict `tool_trajectory_avg_score` matching — the eval still proves the agent loads with the planner, the LLM picks a specialist, and the full path runs without errors. Tighten thresholds once an arg-matching strategy is adopted.

### Changed
- **Type hints / lint hygiene** to clear `make ty` after the `ty>=0.0.34` bump: `agents/elasticsearch/elasticsearch_agent/tools.py` adds a `# ty: ignore[invalid-assignment]` on `requests.Session.verify` (the type stub narrows it to `bool`, but at runtime the attribute accepts `bool | str | None` — a string is treated as a CA bundle path); `core/tests/test_operators.py` annotates the `FakeStrimzi` test double with explicit `tuple[str, ...]` / `tuple[CRDRef, ...]` types so it satisfies the `OperatorDetector` protocol; `core/tests/test_base.py::TestResolvePlanner.test_builtin_include_thoughts_false` narrows via `isinstance(result, BuiltInPlanner)` before reading `thinking_config`.

### Documentation
- **`docs/config/general.md` — Planning section**: Env-var matrix and decision guide for `plan_react` vs. `builtin`, including the latency tradeoff and the rationale for keeping tool-leaf agents planner-free.
- **`docs/agent-design-patterns.md` — Planning paragraph** under *Iterative & Feedback Patterns* with the list of opted-in agents and a link to the config reference.
- **`core/README.md` — `resolve_planner()` reference** alongside the existing `create_agent()` table; `planner=` is now a documented kwarg.
- **`README.md` — feature bullet** in *Intelligence & Orchestration* announcing opt-in planning with the env-var on-ramp.
- **`.env.example` — Planning block** documenting `ORRERY_PLANNER`, `ORRERY_PLANNER_THINKING_BUDGET`, `ORRERY_PLANNER_INCLUDE_THOUGHTS` with inline guidance on when to use each value.

## [0.1.9] - 2026-04-26

### Added
- **Progressive progress cards in Google Chat** (`agents/google-chat-bot`): The async response path now posts a live "🔍 Investigating…" Card v2 immediately and PATCHes it in place as the agent run progresses. Operators see the currently executing sub-agent (friendly label — *Checking Kafka*, *Synthesizing findings*, …), a tool-call breadcrumb, subsystem health chips (✅/⚠️/❌/⏳) derived from `state_delta` writes on `kafka_status` / `k8s_status` / `docker_status` / `observability_status` / `elasticsearch_status`, an optional remediation panel populated by `remediation_action` / `verification_result` / `remediation_summary`, and an elapsed-seconds footer. Updates are debounced at 800ms and force-flushed on status transitions.
- **Structured triage result card**: When an `incident_triage_agent` run completes, the progress card is replaced with a structured **Triage Report** — severity badge (🟢 healthy / 🟡 degraded / 🔴 critical), one section per subsystem, the `triage_summarizer` output, and a role-gated **Run Remediation** instruction (operator/admin only, only shown when overall severity is not healthy) inviting the user to send the **Remediate** Quick Command, which dispatches the `remediation_pipeline` in the same session so it reuses the triage report already in state.
- **`ChatClient.update_message()`**: New async PATCH helper on the Chat REST client with dynamic `updateMask`. Tolerates 404/410 (message deleted) by returning `None` so the progressive-card loop can stop updating silently.
- **`progress.ProgressTracker`**: New module that consumes ADK runner events (`author`, `state_delta`, function calls) and drives a debounced async update callback. Threaded through `_run_agent` via an optional `tracker=` kwarg so the sync path is unchanged.
- **Quick Command-based confirmation flow** (`agents/google-chat-bot`): Confirmation cards no longer embed inline `confirm_action` / `deny_action` buttons. Operators send the **Approve** or **Deny** Quick Commands (`appCommandId=1` / `appCommandId=2`) configured in the Chat API console; the handler resolves the latest pending confirmation in the thread/space and routes accordingly. Quick Commands ride the standard MESSAGE delivery path, sidestepping the inconsistent `invokedFunction`-button delivery seen in some Workspace Add-on configurations.
- **Tests**: +6 `test_chat_client.py` (create/update mask variants, 404 tolerance, 500 raises), +16 cases in `test_cards.py` (status classification, progress-card shape, remediation panel, result-card severity, role-gated instruction), +7 cases in `test_handler.py` (progress posted then updated, final card is triage result when chips landed, role-gated remediation instruction, update failure falls back to new message, runtime error replaces progress with error card, run_remediation click dispatches a new run), +5 cases covering the new approve handshake (parent session id reconstructed from state, store-based approved-pending consumption, stale-approval rejection beyond the validity window, unapproved-pending blocks LLM auto-retry, args embedded in synthetic prompt). All **618 unit tests** + lint + format clean.

### Changed
- **`handler._handle_message_async` rewired**: Posts the initial progress card before invoking the runner, attaches a tracker that PATCHes the same message on each significant event, and on completion replaces it with either the structured triage card (when chips landed) or the original reply. Non-triage queries retain the previous text + buffered-confirmation-card behavior.
- **`_handle_app_command_async` and `_handle_card_click_async` rewired**: Both Approve/Deny dispatch paths now post a progress card before re-running the runner and replace it with the final reply on completion, so operators get live feedback during the post-approve LLM run instead of a silent ~30s wait.
- **`_post_async_error`** now replaces an existing progress card with an error card (`build_error_card`) instead of appending a new message, so a crashed run never leaves operators staring at a stuck "Investigating…" frame.
- **Card-click dispatch** recognizes a third `invokedFunction: "run_remediation"` alongside `confirm_action` / `deny_action`. The remediation branch bypasses the pending-confirmation store and spawns a new `_run_agent` turn with a remediation prompt.
- **Approve handshake is now `ConfirmationStore`-driven** (`agents/google-chat-bot/google_chat_bot/confirmation.py`): The previous design wrote a `_gchat_pending_<tool>` fingerprint into `tool_context.state` on the first call and consumed it on the LLM's retry. That state lives on whatever ADK session ran the tool, which for tools invoked through an `AgentTool` is the sub-agent's ephemeral session — it doesn't propagate back to the gchat parent session. The callback now consults the store directly, matching `(thread_or_space_key, tool_name, args_hash)` against entries the click handler has marked `approved=True`. `mark_latest_approved_for_thread` flips the flag without popping; `consume_approved` pops on the LLM's retry. A 120s validity window after Approve prevents a stale approval from auto-executing a fresh request later.
- **Synthetic Approve prompt embeds the original arguments verbatim**: Replaces the previous `"Yes, proceed with <tool>."` (which forced the LLM to reconstruct args from chat history — fragile when the call originated inside a sub-agent whose args the parent session never saw) with `"The operator (<name>) approved the previous <tool> request. Re-issue the same call now with arguments <k=v, …> and report the result."` `PendingConfirmation` now carries the original `args` dict, an `args_hash`, `created_at`, `approved`, and `approved_at` fields.

### Fixed
- **`patch_deployment` (and any other guarded tool on a sub-agent) detoured through `remediation_pipeline` after Approve** (`agents/google-chat-bot`): When a guarded tool fired from inside an `AgentTool` (e.g., `k8s_health_agent.patch_deployment`), the confirmation callback captured `tool_context.session.id` — which for an AgentTool is the ephemeral inner session, not the gchat parent session keyed `gchat:<thread-or-space>`. On Approve, the handler re-entered the runner with that ephemeral id and the LLM, finding no conversation history and only the synthetic `"Yes, proceed with patch_deployment."`, hallucinated a remediation against an unrelated deployment (`default/frontend` was the most-frequent miss). The callback now reconstructs the parent session id from `state["gchat_thread"]` / `state["gchat_space"]` (which the handler writes at the start of every turn) so Approve always re-enters the conversation the operator was in. Verified end-to-end with `restart_deployment` and `patch_deployment` against `gatekeeper-system/gatekeeper-audit`.
- **Re-prompting loop on every Approve retry** (`agents/google-chat-bot`): The per-context `_gchat_pending_<tool>` flag the callback set on the first call lived on the AgentTool's child session and was invisible to subsequent calls (each AgentTool invocation gets a fresh sub-session). On retry the callback never saw a matching pending and posted *another* approval card — the operator could be stuck approving forever. Resolved by moving the handshake into `ConfirmationStore` (see *Changed* above).
- **APP_COMMAND payloads with empty `message: {}` resolved to the wrong pending in shared spaces**: Quick Command events don't carry a thread name, so `_extract_thread_name` returns `None` and the handler falls back to matching by space name. The handler now logs a warning when this fallback fires so operators can see when concurrent destructive actions in a shared space could collide on the resolution.

## [0.1.8] - 2026-04-23

### Added
- **Elasticsearch Agent** (`agents/elasticsearch`): New specialist agent for Elasticsearch cluster operations, exposing **19 read-only REST tools** — cluster health/stats/nodes/pending tasks/settings, indices listing/stats/mappings/settings, shard allocation + `explain` diagnostics, `search` + `count`, index templates, aliases, ILM policies + `explain_ilm_status`, and snapshot repositories/snapshots. HTTP session is pooled as a module-level singleton with API-key / basic-auth / CA bundle support via `ELASTICSEARCH_*` env vars.
- **ECK Operator Tools**: Five Kubernetes control-plane tools complementing the REST surface — `list_eck_clusters`, `describe_eck_cluster`, `list_kibana_instances`, `describe_kibana`, `get_eck_operator_events`. Wired through the shared `orrery_core.default_registry.ECKDetector` for interpreted `healthy` / `phase` / `warnings` on each CR.
- **Orrery-assistant integration**: `elasticsearch_agent` is now a sibling `AgentTool` on the root orchestrator, and a new `elasticsearch_health_checker` joins `health_check_agent` as the fifth parallel branch of the incident triage pipeline (writes `elasticsearch_status` to session state for the `triage_summarizer`).
- **Compose profile + Makefile targets**: `docker-compose.yml` gains an `elastic` profile with single-node Elasticsearch 8.13.4 + Kibana (security disabled for dev); `make run-elasticsearch` / `make run-elasticsearch-cli` launch the agent standalone.
- **Tests & evals**: 36 new unit tests (25 REST + 11 ECK) with mocked `requests.Session` / `CustomObjectsApi` / `CoreV1Api`, plus 6 new eval scenarios (`cluster_and_indices.test.json`, `eck.test.json`) — bringing the suite to **608 tests / 28 eval scenarios**.

## [0.1.7] - 2026-04-19

### Added
- **Zero-clone Docker quick-start**: The README and `docs/getting-started.md` now lead with `docker pull ghcr.io/bahalla/orrery:latest` + `docker run` for a 30-second single-container test drive, followed by a `curl`-the-compose-file path for the full Kafka/Postgres/Prometheus stack — no repository clone required.
- **`ORRERY_IMAGE` override**: `docker-compose.yml` honours `ORRERY_IMAGE` so users can pin a specific release tag (e.g. `ORRERY_IMAGE=ghcr.io/bahalla/orrery:v0.1.7 docker compose --profile demo up -d`).
- **Best-practices & scaling guide**: `docs/agent-design-patterns.md` gains three new sections — tool/agent sizing budgets (sweet 5–15 tools, 3–7 direct children, depth ≤3), framework/model-specific limits (Gemini/Claude/OpenAI/Ollama caps, ADK `LoopAgent.max_iterations` discipline, context-window budgeting), and a decision guide for when to reach for the A2A protocol (referencing [AEP-005](docs/enhancements/aep-005-a2a-protocol.md)) with a four-stage scaling playbook.

### Changed
- **Python 3.14 upgrade**: Bumped `requires-python` from `>=3.11` to `>=3.14` across the root and all nine workspace packages (`core`, `docker-agent`, `google-chat-bot`, `k8s-health`, `kafka-health`, `observability`, `ops-journal`, `orrery-assistant`, `slack-bot`). Ruff `target-version` updated to `py314`. Dockerfile base images switched to `ghcr.io/astral-sh/uv:python3.14-bookworm-slim` (builder) and `python:3.14-slim-bookworm` (runtime). CI and release workflows now install Python 3.14. `uv.lock` regenerated — all C-extension wheels (confluent-kafka, psycopg2-binary, asyncpg, pydantic-core, numpy, tiktoken) resolved to prebuilt 3.14 wheels with no source-build fallbacks. Full test suite (572 tests) passes on 3.14.
- **Single production Dockerfile**: Merged `Dockerfile.prod` into `Dockerfile` so the repository ships one production-ready image. The consolidated Dockerfile adds `UV_COMPILE_BYTECODE=1`, `UV_LINK_MODE=copy`, `PYTHONUNBUFFERED=1`, `PYTHONDONTWRITEBYTECODE=1`, a BuildKit `--mount=type=cache` for uv, `--extra postgres` by default, and folds ownership into `COPY --chown=` to drop a redundant `chown -R` layer.
- **Compose services pull by default**: `orrery-assistant` and `slack-bot` in `docker-compose.yml` now use `image: ${ORRERY_IMAGE:-ghcr.io/bahalla/orrery:latest}` with `build: .` retained as a local-dev fallback — first-run users no longer wait for a local build.
- **CI/release type-check coverage**: `.github/workflows/ci.yml` and `.github/workflows/release.yml` now include `--extra-search-path agents/google-chat-bot` in the `ty` invocation so that agent is type-checked alongside the others (it was already shipping in the image and in runtime CI).
- **Documentation refresh**: `SECURITY.md`, `docs/deployment.md`, `docs/troubleshooting.md`, `docs/enhancements/aep-011-deployment-hardening.md`, and `docs/enhancements/aep-014-supply-chain-security.md` updated to reference the single `Dockerfile` after the consolidation.

### Fixed
- **Broken `HEALTHCHECK` on the default image**: The Dockerfile `HEALTHCHECK` and the `orrery-assistant` compose healthcheck both probed `http://localhost:8080/healthz`, but `HealthServer` is only started by `run_persistent()` and the Pub/Sub worker — not by the default `adk web` CMD. Probes are now owned by the orchestrator (docker-compose / Helm values in `deploy/helm/orrery-assistant/values.yaml`) rather than baked into the image.
- **Latent port conflict in compose**: `orrery-assistant` and `kafka-ui` both bound host port `8080`. The unused `8080:8080` mapping on `orrery-assistant` was removed (the healthz server doesn't run for that CMD anyway).

### Removed
- **`Dockerfile.prod`**: replaced by the consolidated, production-ready `Dockerfile`. The `release.yml` workflow now builds from `./Dockerfile`.

## [0.1.6] - 2026-04-19

### Added
- **Operator Registry (`orrery_core.operators`)**: Pluggable registry for Kubernetes operator detection and CR status interpretation. Ships with built-in detectors for **Strimzi** (`kafka.strimzi.io` — 9 kinds incl. `Kafka`, `KafkaTopic`, `KafkaConnector`, `KafkaRebalance`) and **ECK** (`*.k8s.elastic.co` — 7 kinds incl. `Elasticsearch`, `Kibana`, `ApmServer`, `Beat`). New detectors can be registered via `default_registry.register()`.
- **Structured Tool Results (`orrery_core.ToolResult`)**: Pydantic model with `ok()` / `error()` / `partial()` factories and `remediation_hints` for cross-agent composition. Flattens to a backward-compatible dict via `.to_dict()`, so adoption is gradual and existing tests/tools keep working.
- **k8s-health Operator-Aware Tools**: Six new tools on the `k8s-health` agent — `detect_operators`, `list_custom_resources`, `describe_custom_resource`, `get_owner_chain`, `describe_workload`, `get_operator_events`. `describe_workload` walks `ownerReferences` from a Pod up to its root CR (e.g., Pod → StatefulSet → `Kafka`) and returns the operator's interpreted status (healthy/phase/warnings) instead of raw pod info.
- **kafka-health Strimzi Tools**: Ten new tools that complement the Kafka-protocol tools with a view into the Strimzi control plane — `list_strimzi_clusters`, `describe_strimzi_cluster`, `list_strimzi_topics`, `list_kafka_users`, `list_kafka_connectors`, `get_kafka_connect_status`, `get_mirrormaker2_status`, `get_kafka_rebalance_status`, plus the guarded `approve_kafka_rebalance` (patches `strimzi.io/rebalance: approve`) and `restart_kafka_connector` (patches `strimzi.io/restart: true`). Uses the shared operator registry for status interpretation.
- **Property-Based Guardrail Tests**: Integrated `hypothesis` and added exhaustive tests for tool argument hashing in `core/tests/test_guardrails.py`, ensuring deterministic and order-invariant hashes for stable confirmation matching.
- **Pub/Sub Worker Health Probes**: The worker now exposes `/healthz` and `/readyz` via the shared `HealthServer`. Readiness flips to 503 if the streaming-pull future dies, so kubelet restarts the pod automatically.

### Changed
- **ADK Upgrade**: Upgraded `google-adk` to **v1.31.0** across the workspace.
- **Experimental Warning Suppression**: pytest is now configured to suppress all experimental feature warnings from `google.adk.features`, keeping test output clean and focused.
- **Helm: Liveness/Readiness + PDB**: `pubsubWorker` deployment now configures liveness, readiness, health port, and an optional `PodDisruptionBudget`.
- **Terraform: DLQ Triage Access**: New `dlq_subscribers` variable grants `roles/pubsub.subscriber` on the DLQ subscription to configured SRE/on-call groups, plus a `dead_letter_subscription_name` output.
- **Docs**: `docs/integrations/google-chat-pubsub.md` now documents every Terraform variable (`chat_publisher_email`, `enable_vertex_ai`, `vertex_ai_project_id`, tuning knobs) and the timeout-alignment rule for Pub/Sub ack deadlines.
- **AEP-018**: Proposal for Pub/Sub idempotency (dedup store on `eventId`) and HPA-on-backlog for the `pubsubWorker` to remove the single-replica SPOF during incidents.

## [0.1.5] - 2026-04-18

### Added
- **Pub/Sub Diagnostics**: Added verbose trace logging for message receipt, parsing, and agent execution to simplify troubleshooting.
- **Heartbeat Monitor**: Implemented a 60-second background heartbeat log in the Pub/Sub worker to provide "proof of life" in container logs.
- **Cross-Project Support**: Explicitly documented and enabled support for subscriptions living in different GCP projects than the agent runner via `GOOGLE_CHAT_PUBSUB_PROJECT`.

### Fixed
- **Cleanup**: Ensured proper cancellation of background heartbeat tasks during worker shutdown.

## [0.1.4] - 2026-04-18

### Added
- **Google Chat Pub/Sub Transport**: Added support for private GKE clusters via Pub/Sub connection type.
- **Terraform Module**: New module in `deploy/terraform/google-chat-bot` for automated GCP infrastructure setup.
- **Helm Expansion**: Added `pubsubWorker` deployment and Workload Identity support to the Helm chart.
- **Overridable Publisher IAM**: Added `chat_publisher_email` variable to handle Domain Restricted Sharing (GCP Org Policy) for Workspace Add-ons.

### Changed
- **Unified Handler**: Refactored Google Chat bot to use a transport-agnostic handler shared between HTTP and Pub/Sub.
- **GKE Deployment Story**: Removed legacy Kustomize manifests in favor of a unified Helm + Terraform production pattern.

### Fixed
- **Poison Message Handling**: Implemented robust `ack`/`nack` logic in the Pub/Sub worker to prevent infinite redelivery of malformed payloads.

## [0.1.3] - 2026-04-13

### Changed
- **Rebranding**: Project renamed from "AI Agents for DevOps" to **Orrery**.
- **Package Renaming**: `ai-agents-core` is now `orrery-core`.
- **Agent Renaming**: `devops-assistant` is now `orrery-assistant`.
- **Infrastructure**: Updated Kubernetes manifests, Helm charts, and Docker images to use the `orrery` namespace and naming.
- **Observability**: Prometheus metrics renamed from `ai_agents_*` to `orrery_*`.

## [0.1.2] - 2026-04-12

### Added

- **Google Chat Async Mode**: Added `GOOGLE_CHAT_ASYNC_RESPONSE` and `GOOGLE_CHAT_SERVICE_ACCOUNT_FILE` to `.env.example` for long-running agent support.
- **Enhanced Documentation**: Added Google Chat to the main README and expanded the integration guide with troubleshooting for ADC, scopes, and 401/404/403 errors.

### Changed

- **Google Chat Roadmap**: Moved Google Chat from "Upcoming" to "Current Integrations" in the documentation.
- **API Reference**: Registered `google-chat-bot` for automatic API documentation generation in `mkdocs.yml`.

### Fixed

- **Google Chat Event Parsing**: Implemented robust multi-path parsing for space names, emails, and thread names to prevent 404 errors during asynchronous replies.
- **Async Auth Scope Guidance**: Added detailed troubleshooting and configuration for `403 Forbidden` errors caused by missing `chat.bot` scopes when using Application Default Credentials (ADC).
- **ADC-First Auth Pattern**: Updated Google Chat bot to prioritize Application Default Credentials, enabling seamless Workload Identity support on GKE and Cloud Run.

## [0.1.1] - 2026-04-11

### Added

- **Google Chat Bot Integration**: A new integration bringing autonomous DevOps agents to Google Workspace with support for thread-based sessions and interactive Card v2.
- **Workspace Add-ons Compatibility**: Implemented strict `hostAppDataAction` (DataActions) schema support, enabling the bot to run behind the Google Workspace Add-ons pipeline.
- **Dual-Path Event Detection**: Added logic to seamlessly handle interaction events from both standard Google Chat API and the nested Workspace Add-ons event structure.
- **Interactive Guardrails for Chat**: Wired `@confirm` and `@destructive` tools to post interactive Cards v2 with Approve/Deny buttons, allowing operators to authorize dangerous actions directly from the chat.
- **Configurable Identities**: Added `GOOGLE_CHAT_IDENTITIES` to allow dynamic verification of multiple signing service accounts (e.g., standard Chat vs Add-ons service agents).
- **Kafka KRaft Migration**: Removed Zookeeper dependency. Kafka now runs in KRaft mode for improved startup reliability and simplified architecture.
- **PostgreSQL Service**: Added a dedicated PostgreSQL container to `docker-compose.yml` for persistent session storage.
- **Centralized Configuration**: Merged per-agent `.env` files into a single root `.env` file. Updated core library to prioritize the root configuration while maintaining legacy override support.
- **Cross-Session Memory**: Enabled `MemoryPlugin` in the `orrery-assistant` agent, allowing it to remember past interactions and save session highlights to the persistent store.
- **Kafka Partition Scaling**: Added `update_kafka_partitions` tool to the Kafka health agent with full unit test coverage.
- **Production deployment hardening** (AEP-011) — complete Kubernetes deployment story
  - Kustomize manifests under `deploy/k8s/` (Deployment, Service, HPA, PDB, NetworkPolicy, ServiceAccount with scoped ClusterRoles)
  - Helm chart under `deploy/helm/orrery-assistant/` with configurable values, NOTES, and `existingSecret` support for out-of-band secret management
  - GHCR CD pipeline (`.github/workflows/docker-publish.yml`) publishing multi-arch (amd64/arm64) images with SBOM and provenance attestation on merges to `main` and `v*.*.*` tags
  - PostgreSQL session store support — `runner.py` honors `DATABASE_URL` (async driver `postgresql+asyncpg://…`) for multi-instance deployments; `core[postgres]` extra adds `asyncpg` and `psycopg2-binary`
  - Rate limiting on the Slack bot `/slack/events` webhook via `slowapi` (configurable via `SLACK_RATE_LIMIT`, default `60/minute`)
  - Root-level `.env.example` documenting every required and optional variable across agents and deployment manifests
  - `docs/deployment.md` — end-to-end production deployment guide (Postgres setup, Helm install, rolling updates, troubleshooting)


### Changed

- `SlackBotConfig.resolve_db_url()` prefers `DATABASE_URL` env var over the legacy `slack_db_url` default — enables sharing Postgres between the Slack bot and the ADK web UI workloads
- `load_agent_env()` and `load_config()` now search for a `.env` file at the project root by default.

### Fixed

- **OIDC Token Verification**: Fixed a bug where tokens from Google's migrated OIDC flow (`iss=accounts.google.com`) were rejected. Added proper identity verification against the `email` claim.
- **Dynamic Session Handling**: Fixed `SessionNotFoundError` in the ADK Runner by enabling `auto_create_session=True` for thread-based chat integrations.
- **JSON Schema Validation**: Resolved "Failed to parse JSON" errors in Google Chat by ensuring all responses (including errors and added-to-space events) strictly follow the Add-ons response schema.
- **Metrics Callback Signature**: Fixed a `TypeError` in `MetricsPlugin` where incorrect keyword arguments were passed to the internal callback.
- **Kafka Tool Imports**: Fixed a `NameError` in `kafka_health_agent/tools.py` caused by using decorators before they were imported.
- **Database URL Masking**: Ensured `DATABASE_URL` is masked in all log outputs and console prints to prevent credential leaks.

## [0.1.0] - 2026-04-09

First public release of the AI Agents for DevOps & SRE platform.

### Added

- **Multi-agent orchestrator** — `orrery-assistant` root agent delegates to 5 specialist agents via `AgentTool` and deterministic sub-agent workflows ([ADR-002](docs/adr/002-agent-tool-vs-sub-agents.md))
- **Specialist agents** — Kafka health, K8s health, Observability (Prometheus/Loki/Alertmanager), Docker, and Ops Journal
- **Slack bot** — Thread-based sessions with interactive Approve/Deny buttons for guarded operations
- **Incident triage pipeline** — `SequentialAgent` + `ParallelAgent` for parallel health checks across all systems, triage summary, and journal recording
- **Closed-loop remediation** (AEP-004) — `LoopAgent`-based pipeline: act (restart/scale/rollback) → verify → retry up to 3 iterations, with `exit_loop` tool for early termination
- **Context caching** (AEP-007) — ADK `ContextCacheConfig` for Gemini models, reducing token usage for repeated requests. Configurable via `CONTEXT_CACHE_MIN_TOKENS`, `CONTEXT_CACHE_TTL_SECONDS`, `CONTEXT_CACHE_INTERVALS` env vars
- **Cross-session memory** (AEP-003) — `SecureMemoryService` with automatic PII redaction and size limits
- **Agent evaluation framework** (AEP-002) — 22 eval scenarios across 4 agents verifying correct tool routing via ADK's `AgentEvaluator`. Run with `make eval`
- **RBAC** — 3-role hierarchy (viewer/operator/admin) enforced globally via `GuardrailsPlugin` ([ADR-001](docs/adr/001-rbac.md))
- **Safety guardrails** — `@destructive` and `@confirm` decorators gate dangerous operations with args-hash + TTL confirmation tracking (AEP-001)
- **Authentication enforcement** — `set_user_role()` marks server-trusted roles; `ensure_default_role()` forces `viewer` for unset roles
- **Input validation** — 5 reusable validators (`validate_string`, `validate_positive_int`, `validate_url`, `validate_path`, `validate_list`) applied across 30+ tool functions
- **ADK Plugins** — cross-cutting concerns as `BasePlugin` subclasses: `GuardrailsPlugin`, `ResiliencePlugin`, `MetricsPlugin`, `AuditPlugin`, `ActivityPlugin`, `ErrorHandlerPlugin`, `MemoryPlugin`
- **Prometheus metrics** — tool call counts, latency histograms, error rates, circuit breaker state, LLM tokens, and context cache events on `/metrics`
- **Resilience** — per-tool circuit breaker via `ResiliencePlugin`, `@with_retry` decorator with exponential backoff and jitter
- **Structured JSON logging** — `setup_logging()` with `JSONFormatter`, audit trail via `AuditPlugin`, activity tracking via `ActivityPlugin`
- **Multi-provider LLM** — Gemini (default), Claude, OpenAI, Ollama via `resolve_model()` + LiteLLM
- **Persistent runner** — `run_persistent()` with SQLite-backed sessions, health probes, graceful shutdown
- **Agent factory functions** — `create_agent()`, `create_sequential_agent()`, `create_parallel_agent()`, `create_loop_agent()`
- **Docker deployment** — multi-stage builds, non-root user, `docker-compose.yml` with demo/slack profiles
- **468 unit tests** — all async, all mocked, no running infrastructure required
- **CI pipeline** — lint (ruff), type check (ty), security scan (bandit), tests, evals

### Security

- Input validation at tool boundaries prevents injection attacks
- Path traversal prevention in Docker and K8s tools
- URL scheme allowlisting rejects `javascript:`, `data:`, `file:` URIs
- Docker container inspection redacts sensitive environment variables
- Guardrail confirmation bypass fixed with args-hash + TTL tracking
- Server-side role enforcement prevents privilege escalation

[Unreleased]: https://github.com/BAHALLA/orrery/compare/v0.1.9...HEAD


[0.1.9]: https://github.com/BAHALLA/orrery/compare/v0.1.8...v0.1.9
[0.1.8]: https://github.com/BAHALLA/orrery/compare/v0.1.7...v0.1.8
[0.1.7]: https://github.com/BAHALLA/orrery/compare/v0.1.6...v0.1.7
[0.1.6]: https://github.com/BAHALLA/orrery/compare/v0.1.5...v0.1.6
[0.1.5]: https://github.com/BAHALLA/orrery/compare/v0.1.4...v0.1.5
[0.1.4]: https://github.com/BAHALLA/orrery/compare/v0.1.3...v0.1.4
[0.1.3]: https://github.com/BAHALLA/orrery/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/BAHALLA/orrery/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/BAHALLA/orrery/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/BAHALLA/orrery/releases/tag/v0.1.0
