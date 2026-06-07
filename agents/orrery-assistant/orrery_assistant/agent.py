"""Root orchestrator for the orrery assistant (ADK 2.0).

The interactive root is ``orrery_chat_agent`` — a conversational chat-mode
``LlmAgent`` that routes free-form queries to specialist agents via ``AgentTool``
and delegates broad "run a triage" requests to ``incident_triage_agent``. This is
the standard ADK 2.0 coordinator pattern; a chat-mode agent must be the root (ADK
forbids it as a routed node inside a graph).

The deterministic, parallel, bounded-loop pipeline lives in
``orrery_triage_workflow`` (a graph ``Workflow``) as a separate batch/scheduled
entrypoint — `make run-triage` — because a ``Workflow`` cannot be a sub-agent or
``AgentTool`` of the chat coordinator. Both reuse the same node agents. See ADR-003.

    orrery_chat_agent (chat-mode LlmAgent, ROOT)
      ├─ AgentTool: kafka / k8s / observability / elasticsearch / docker / ops_journal
      ├─ AgentTool: incident_triage_agent (single-turn full health sweep)
      └─ PreloadMemoryTool

    orrery_triage_workflow (Workflow, separate entrypoint)
      START ─▶ [parallel] 5 health checkers ─▶ health_join (JoinNode)
            ─▶ triage_summarizer ─▶ journal_writer ─▶ triage_route
                  ├─("remediate")▶ remediation_actor ⇄ remediation_verifier
                  │                    └▶ verify_route ─("done")▶ summarizer ─▶ final_report
                  └─("resolved")▶ final_report
"""

from typing import Any

from google.adk import Workflow
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.adk.tools.tool_context import ToolContext
from google.adk.workflow import JoinNode

from docker_agent.agent import root_agent as docker_agent_root
from docker_agent.tools import (
    docker_compose_status,
    get_container_stats,
    list_containers,
)
from elasticsearch_agent.agent import root_agent as elasticsearch_agent
from elasticsearch_agent.eck import list_eck_clusters
from elasticsearch_agent.tools import (
    get_cluster_health as es_get_cluster_health,
)
from elasticsearch_agent.tools import (
    get_shard_allocation as es_get_shard_allocation,
)
from elasticsearch_agent.tools import (
    list_indices as es_list_indices,
)
from k8s_health_agent.agent import root_agent as k8s_agent
from k8s_health_agent.tools import (
    get_cluster_info,
    get_events,
    get_nodes,
    list_pods,
)
from kafka_health_agent.agent import root_agent as kafka_agent
from kafka_health_agent.tools import (
    get_consumer_lag,
    get_kafka_cluster_health,
    list_consumer_groups,
    list_kafka_topics,
)
from observability_agent.agent import root_agent as observability_agent
from observability_agent.tools import (
    get_active_alerts,
    get_prometheus_alerts,
    get_prometheus_targets,
    query_prometheus,
)
from ops_journal_agent.agent import root_agent as journal_agent
from ops_journal_agent.tools import log_operation, save_note
from orrery_assistant.remediation import (
    remediation_actor,
    remediation_summarizer,
    remediation_verifier,
    verify_route,
)
from orrery_core import (
    AgentTool,
    create_agent,
    create_context_cache_config,
    default_plugins,
    load_agent_env,
    resolve_planner,
)

load_agent_env(__file__)

# Resolve once at import time so triage_summarizer + remediation share an instance.
_planner = resolve_planner()

# ── Parallel health checkers (graph nodes) ────────────────────────────

kafka_health_checker = create_agent(
    name="kafka_health_checker",
    description="Checks Kafka cluster health and reports status.",
    instruction=(
        "Check the Kafka cluster health, list topics, and check consumer group lag. "
        "Provide a brief status summary of your findings."
    ),
    tools=[get_kafka_cluster_health, list_kafka_topics, list_consumer_groups, get_consumer_lag],
    output_key="kafka_status",
)

k8s_health_checker = create_agent(
    name="k8s_health_checker",
    description="Checks Kubernetes cluster health and reports status.",
    instruction=(
        "Check Kubernetes cluster health: cluster info, node status, recent events, "
        "and any failing pods. Provide a brief status summary of your findings."
    ),
    tools=[get_cluster_info, get_nodes, get_events, list_pods],
    output_key="k8s_status",
)

docker_health_checker = create_agent(
    name="docker_health_checker",
    description="Checks Docker container status and reports findings.",
    instruction=(
        "List running containers and check their stats. "
        "Report any unhealthy or stopped containers. Provide a brief status summary."
    ),
    tools=[list_containers, get_container_stats, docker_compose_status],
    output_key="docker_status",
)

observability_health_checker = create_agent(
    name="observability_health_checker",
    description="Checks Prometheus targets, firing alerts, and Alertmanager status.",
    instruction=(
        "Check Prometheus target health, list firing alerts from Prometheus rules, "
        "and check active Alertmanager alerts. Provide a brief status summary."
    ),
    tools=[get_prometheus_targets, get_prometheus_alerts, get_active_alerts, query_prometheus],
    output_key="observability_status",
)

elasticsearch_health_checker = create_agent(
    name="elasticsearch_health_checker",
    description="Checks Elasticsearch cluster health, indices, and ECK CRs.",
    instruction=(
        "Check Elasticsearch cluster health (green/yellow/red), list indices, and "
        "scan for unassigned shards. If running on Kubernetes, also list ECK "
        "Elasticsearch CRs to cross-check declarative state. Provide a brief "
        "status summary; call out any red/yellow health, unassigned shards, or "
        "ECK clusters not in the Ready phase."
    ),
    tools=[es_get_cluster_health, es_list_indices, es_get_shard_allocation, list_eck_clusters],
    output_key="elasticsearch_status",
)

# Fan-out/fan-in tuple: all five run in parallel, JoinNode waits for all.
HEALTH_CHECKERS = (
    kafka_health_checker,
    k8s_health_checker,
    docker_health_checker,
    observability_health_checker,
    elasticsearch_health_checker,
)

health_join = JoinNode(name="health_join")

# ── Triage verdict tool + summarizer ──────────────────────────────────


async def record_triage_verdict(
    overall_status: str,
    report: str,
    tool_context: ToolContext,
) -> dict:
    """Record the triage report and a machine-readable severity for routing.

    Args:
        overall_status: One of "healthy", "degraded", or "critical".
        report: The full incident triage report text.
        tool_context: ADK tool context (injected automatically).

    Returns:
        A dict confirming the normalized severity that was recorded.
    """
    status = overall_status.strip().lower()
    if status not in ("healthy", "degraded", "critical"):
        # Unknown verdicts are treated as actionable so we don't skip remediation.
        status = "degraded"
    tool_context.state["incident_severity"] = status
    tool_context.state["triage_report"] = report
    return {"status": "recorded", "overall_status": status}


triage_summarizer = create_agent(
    name="triage_summarizer",
    description="Synthesizes health check results into an incident triage report.",
    planner=_planner,
    instruction=(
        "You receive health check results from five systems stored in session state: "
        "kafka_status, k8s_status, docker_status, observability_status, and "
        "elasticsearch_status.\n\n"
        "Synthesize these into a single incident triage report with:\n"
        "1. Overall system status (healthy / degraded / critical)\n"
        "2. Issues found per system\n"
        "3. Recommended next actions\n\n"
        "Then call record_triage_verdict EXACTLY ONCE with overall_status set to "
        "'healthy', 'degraded', or 'critical' and report set to your full report "
        "text. Be concise and actionable."
    ),
    tools=[record_triage_verdict],
)

# ── Journal writer ────────────────────────────────────────────────────

journal_writer = create_agent(
    name="journal_writer",
    description="Saves the triage report as a journal note.",
    instruction=(
        "Read the triage report from session state (triage_report). "
        "Save it as a note using save_note with the tag 'incident-triage'. "
        "Also log this operation using log_operation."
    ),
    tools=[save_note, log_operation],
)


# ── Conversational triage sub-agent ───────────────────────────────────

# A single-turn agent the coordinator delegates to for "run a triage". It sweeps
# every subsystem with the same health-check tools the deterministic graph uses,
# then records a structured verdict. (The deterministic, parallel, bounded-loop
# version lives in `orrery_triage_workflow` below for batch/scheduled runs.)
incident_triage_agent = create_agent(
    name="incident_triage_agent",
    description="Runs a full health sweep across all systems and returns a triage report.",
    planner=_planner,
    instruction=(
        "Run a full incident triage. Check the health of Kafka, Kubernetes, Docker, "
        "Observability (Prometheus/Alertmanager), and Elasticsearch using your tools. "
        "Then synthesize a single triage report (overall status: healthy/degraded/critical, "
        "issues per system, recommended next actions) and call record_triage_verdict ONCE "
        "with overall_status and the full report text. Be concise and actionable."
    ),
    tools=[
        get_kafka_cluster_health,
        list_kafka_topics,
        list_consumer_groups,
        get_consumer_lag,
        get_cluster_info,
        get_nodes,
        get_events,
        list_pods,
        list_containers,
        get_container_stats,
        docker_compose_status,
        get_prometheus_targets,
        get_prometheus_alerts,
        get_active_alerts,
        query_prometheus,
        es_get_cluster_health,
        es_list_indices,
        es_get_shard_allocation,
        list_eck_clusters,
        record_triage_verdict,
    ],
)


# ── Conversational root coordinator ───────────────────────────────────

# The interactive root. A chat-mode LlmAgent (default for a root agent) that holds
# real conversational history and delegates to specialists via AgentTool. It must
# be the root — ADK 2.0 forbids a chat-mode agent as a routed node inside a graph.
orrery_chat_agent = create_agent(
    name="orrery_chat_agent",
    description="Conversational DevOps orchestrator that routes queries to specialist agents.",
    planner=_planner,
    instruction=(
        "You are a DevOps assistant that coordinates specialist agents. Delegate based "
        "on the user's intent:\n"
        "- **kafka_health_agent**: Kafka cluster health, topics, consumer groups, lag.\n"
        "- **k8s_health_agent**: Kubernetes cluster info, nodes, pods, deployments, logs, "
        "events, scaling, restarts, and rollbacks.\n"
        "- **observability_agent**: Prometheus metrics/alerts, Loki logs, Alertmanager.\n"
        "- **elasticsearch_agent**: Elasticsearch health, indices, shards, ILM, snapshots, "
        "and ECK Kubernetes CRs.\n"
        "- **docker_agent**: Docker containers, logs, stats, compose status.\n"
        "- **ops_journal_agent**: Notes, past findings, activity, preferences, bookmarks.\n"
        "- **incident_triage_agent**: a broad health sweep across ALL systems — use it when "
        "the user asks 'is everything healthy?', 'run a triage', or 'check all systems'.\n\n"
        "Use individual specialists for targeted questions; use incident_triage_agent for "
        "broad sweeps. Remediation (restart/scale/rollback) is available via k8s_health_agent "
        "and is guarded — it requires human approval.\n\n"
        "After a significant investigation, proactively offer to save findings via "
        "ops_journal_agent. Relevant context from past sessions is loaded automatically — "
        "use it to correlate with similar past incidents."
    ),
    tools=[
        AgentTool(agent=kafka_agent),
        AgentTool(agent=k8s_agent),
        AgentTool(agent=observability_agent),
        AgentTool(agent=elasticsearch_agent),
        AgentTool(agent=docker_agent_root),
        AgentTool(agent=journal_agent),
        AgentTool(agent=incident_triage_agent),
        PreloadMemoryTool(),
    ],
)


# ── Deterministic routing nodes (used by orrery_triage_workflow) ──────

# Strong, problem-only signals scanned in the per-system status reports as a
# fallback when the LLM fails to emit a structured verdict. Kept conservative
# (phrases rarely used in a "no X" healthy sentence) to limit false positives.
_PROBLEM_SIGNALS = (
    "crashloop",
    "oomkill",
    "backoff",
    "imagepull",
    "unassigned shard",
    "status: red",
    "red status",
    "not ready",
    "notready",
    "degraded",
    "critical",
    "evicted",
    "unavailable",
    "firing",
)

_STATUS_KEYS = (
    "kafka_status",
    "k8s_status",
    "docker_status",
    "observability_status",
    "elasticsearch_status",
)


def _infer_severity_from_status(state: Any) -> str:
    """Best-effort severity from the per-system status reports.

    Used only when ``record_triage_verdict`` was not called. Returns
    ``"degraded"`` if any strong problem signal appears, else ``"healthy"``.
    """
    blob = " ".join(str(state.get(k, "")) for k in _STATUS_KEYS).lower()
    return "degraded" if any(sig in blob for sig in _PROBLEM_SIGNALS) else "healthy"


def triage_route(ctx: Context) -> str:
    """Route to remediation when triage found issues, else finish.

    Prefers the structured ``incident_severity`` written by
    ``record_triage_verdict``. If the LLM produced no verdict, it falls back to
    scanning the per-system status reports and flags ``triage_verdict_missing``
    — so a degraded system is never *silently* routed to ``resolved`` on a
    fail-open default. Initializes the remediation loop counters on entry.
    """
    explicit = ctx.state.get("incident_severity")
    if explicit is None:
        # No structured verdict — fall back to inference and record that the
        # routing decision was made without a confirmed triage verdict.
        ctx.state["triage_verdict_missing"] = True
        severity = _infer_severity_from_status(ctx.state)
        ctx.state["incident_severity"] = severity
    else:
        severity = str(explicit).lower()

    if severity in ("degraded", "critical"):
        ctx.state["remediation_iteration"] = 0
        ctx.state["remediation_resolved"] = False
        route = "remediate"
    else:
        route = "resolved"
    ctx.route = route
    return route


def final_report(ctx: Context) -> str:
    """Terminal node: emit the closing message for either branch."""
    caveat = (
        " ⚠️ Triage ran without a structured verdict; severity was inferred — "
        "manual review recommended."
        if ctx.state.get("triage_verdict_missing")
        else ""
    )
    summary = ctx.state.get("remediation_summary")
    if summary:
        return f"{summary}{caveat}"
    severity = ctx.state.get("incident_severity", "healthy")
    return f"Triage complete — system status: {severity}. No remediation required.{caveat}"


# ── Deterministic triage Workflow (batch / scheduled, not the chat root) ──

# The graph-native, parallel, bounded-loop pipeline. A `Workflow` can't be a
# sub-agent or AgentTool of the chat coordinator (it isn't a BaseAgent), and a
# chat agent can't be a routed node — so the deterministic graph is a standalone
# entrypoint (see run_triage.py / `make run-triage`) that reuses the same nodes.
orrery_triage_workflow = Workflow(
    name="orrery_triage",
    description=(
        "Deterministic incident-response pipeline: parallel health checks across "
        "Kafka, Kubernetes, Docker, Observability, and Elasticsearch → triage → "
        "journaling → conditional closed-loop remediation. Run via `make run-triage`."
    ),
    edges=[
        # Parallel health checks → barrier → triage → journal → route
        ("START", HEALTH_CHECKERS),
        (HEALTH_CHECKERS, health_join),
        (health_join, triage_summarizer, journal_writer, triage_route),
        (triage_route, {"remediate": remediation_actor, "resolved": final_report}),
        # Remediation loop (act → verify → retry, bounded by verify_route)
        (remediation_actor, remediation_verifier, verify_route),
        (verify_route, {"retry": remediation_actor, "done": remediation_summarizer}),
        (remediation_summarizer, final_report),
    ],
)

# The interactive root is the conversational coordinator (ADK web / app.py / CLI).
root_agent = orrery_chat_agent

# ADK web/api_server picks up `app` (with context caching) over bare `root_agent`.
app = App(
    name="orrery_assistant",
    root_agent=orrery_chat_agent,
    plugins=default_plugins(enable_memory=True),
    context_cache_config=create_context_cache_config(),
)
