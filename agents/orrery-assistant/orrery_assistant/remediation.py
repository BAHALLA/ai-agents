"""Closed-loop remediation nodes for the graph-based Workflow root.

Provides the remediation subgraph building blocks: an actor, a verifier, a
deterministic routing function, and a summarizer. The act -> verify -> retry
loop is wired by the Workflow graph in ``agent.py`` via a ``RoutingMap``:

    remediation_actor -> remediation_verifier -> verify_route
        -> {"retry": remediation_actor, "done": remediation_summarizer}

``verify_route`` replaces ADK 1.x's ``LoopAgent`` (max_iterations) + ``exit_loop``
(``actions.escalate``): the verifier signals success by calling
``mark_remediation_resolved``, and ``verify_route`` enforces the iteration cap.
See ADR-003.

RBAC: The remediation actor inherits the guardrails from the tools it calls
(@destructive, @confirm), so only operator/admin roles can trigger destructive
actions inside the loop.
"""

from google.adk.agents.context import Context
from google.adk.tools.tool_context import ToolContext

from k8s_health_agent.tools import (
    describe_pod,
    get_deployment_status,
    get_events,
    get_pod_logs,
    list_pods,
    restart_deployment,
    rollback_deployment,
    scale_deployment,
)
from kafka_health_agent.tools import get_consumer_lag, get_kafka_cluster_health
from ops_journal_agent.tools import log_operation
from orrery_core import create_agent, resolve_planner

# Maximum act -> verify cycles before giving up (replaces LoopAgent.max_iterations).
MAX_REMEDIATION_ITERATIONS = 3

# Reading the planner choice once: the actor benefits most (must reason about
# blast radius before each destructive call), the verifier is a straight
# diagnostic readout where planning adds latency without adding signal.
_planner = resolve_planner()

# ── Resolution signal tool ────────────────────────────────────────────


async def mark_remediation_resolved(
    reason: str,
    tool_context: ToolContext,
) -> dict:
    """Signal that remediation succeeded and the loop should stop.

    The graph's ``verify_route`` reads ``remediation_resolved`` from state on
    the next step and routes to the summarizer instead of retrying.

    Args:
        reason: Why remediation is considered complete.
        tool_context: ADK tool context (injected automatically).

    Returns:
        A dict confirming the resolution was recorded.
    """
    tool_context.state["remediation_resolved"] = True
    tool_context.state["remediation_resolution_reason"] = reason
    return {"status": "remediation_complete", "reason": reason}


# ── Remediation actor ─────────────────────────────────────────────────

remediation_actor = create_agent(
    name="remediation_actor",
    description="Takes remediation actions based on the triage diagnosis.",
    planner=_planner,
    instruction=(
        "You are a DevOps remediation agent. Read the triage report from "
        "session state (triage_report) and the previous verification result "
        "(verification_result) if available.\n\n"
        "Based on the diagnosis, take the SINGLE most appropriate remediation "
        "action using your tools. Choose from:\n"
        "- restart_deployment: For pods in CrashLoopBackOff or OOMKilled\n"
        "- scale_deployment: For high resource usage or consumer lag\n"
        "- rollback_deployment: For failed deployments after a bad release\n\n"
        "If a previous verification shows your last action didn't help, "
        "try a DIFFERENT approach. Do not repeat the same action.\n\n"
        "Record what you did in your output so the verifier can check it."
    ),
    tools=[
        restart_deployment,
        scale_deployment,
        rollback_deployment,
        log_operation,
    ],
    output_key="remediation_action",
)

# ── Remediation verifier ──────────────────────────────────────────────

remediation_verifier = create_agent(
    name="remediation_verifier",
    description="Verifies whether the last remediation action was successful.",
    instruction=(
        "You are a remediation verifier. Check whether the remediation action "
        "described in session state (remediation_action) was successful.\n\n"
        "Use your diagnostic tools to verify the current system state:\n"
        "- get_deployment_status: Check if replicas are ready and available\n"
        "- list_pods: Check if pods are Running and not crash-looping\n"
        "- describe_pod: Get details on specific problematic pods\n"
        "- get_pod_logs: Check logs for errors after the remediation\n"
        "- get_events: Look for new warnings or errors\n"
        "- get_consumer_lag: Check if Kafka lag is decreasing\n"
        "- get_kafka_cluster_health: Verify Kafka cluster status\n\n"
        "If the issue is RESOLVED, call mark_remediation_resolved with a reason "
        "explaining what was fixed.\n\n"
        "If the issue PERSISTS, describe what is still wrong in your output "
        "so the actor can try a different approach on the next iteration."
    ),
    tools=[
        get_deployment_status,
        list_pods,
        describe_pod,
        get_pod_logs,
        get_events,
        get_consumer_lag,
        get_kafka_cluster_health,
        mark_remediation_resolved,
    ],
    output_key="verification_result",
)


# ── Loop routing (replaces LoopAgent + exit_loop) ─────────────────────


def verify_route(ctx: Context) -> str:
    """Decide whether to retry remediation or finish.

    Bumps the iteration counter and routes ``"done"`` when the verifier marked
    the incident resolved or the iteration cap is reached, else ``"retry"``.

    Returns the chosen route (also set on ``ctx.route`` for the graph).
    """
    iteration = ctx.state.get("remediation_iteration", 0) + 1
    ctx.state["remediation_iteration"] = iteration
    resolved = bool(ctx.state.get("remediation_resolved"))
    route = "done" if resolved or iteration >= MAX_REMEDIATION_ITERATIONS else "retry"
    ctx.route = route
    return route


# ── Remediation summary ──────────────────────────────────────────────

remediation_summarizer = create_agent(
    name="remediation_summarizer",
    description="Summarizes the remediation outcome.",
    instruction=(
        "Read the session state: remediation_action and verification_result.\n\n"
        "Write a concise remediation summary including:\n"
        "1. What issue was found\n"
        "2. What actions were taken (and how many iterations)\n"
        "3. Final outcome: resolved or unresolved\n"
        "4. Recommended follow-up actions if unresolved\n\n"
        "Be concise and actionable."
    ),
    tools=[log_operation],
    output_key="remediation_summary",
)
