"""End-to-end execution tests for the graph root's routing (ADR-003).

These run the *real* routing FunctionNodes (``triage_route``, ``verify_route``,
``final_report``) inside an actual ``Workflow`` driven by ``InMemoryRunner``,
with the LLM agent nodes replaced by deterministic stubs. This validates the
execution path — parallel fan-out/join, conditional routing, the bounded
remediation loop, and the missing-verdict fallback — without needing LLM
credentials. It closes the "structure looks right but was never run" gap that a
static review cannot.
"""

import pytest
from google.adk import Workflow
from google.adk.agents.context import Context
from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.adk.workflow import JoinNode
from google.genai import types

# Real routing logic under test.
from orrery_assistant.agent import final_report, triage_route
from orrery_assistant.remediation import MAX_REMEDIATION_ITERATIONS, verify_route

# ── Deterministic stand-ins for the LLM agent nodes ──────────────────


def _check_a(ctx: Context) -> str:
    ctx.state["kafka_status"] = "2 brokers online"
    return "a"


def _check_b(ctx: Context) -> str:
    # Honour a scenario-seeded problem signal so the inference fallback has
    # something to find when the triage stub emits no structured verdict.
    ctx.state["k8s_status"] = ctx.state.get("_seed_k8s_status", "all nodes ready")
    return "b"


def _triage(ctx: Context) -> str:
    scenario = ctx.state.get("_scenario", "healthy")
    if scenario == "missing_verdict":
        # Mimic an LLM that wrote prose but never called record_triage_verdict.
        return "triage prose, no verdict tool call"
    ctx.state["triage_report"] = f"report for {scenario}"
    ctx.state["incident_severity"] = "healthy" if scenario == "healthy" else "critical"
    return "triaged"


def _journal(ctx: Context) -> str:
    return "journaled"


def _actor(ctx: Context) -> str:
    ctx.state["remediation_action"] = "restarted deployment"
    return "acted"


def _verifier(ctx: Context) -> str:
    if ctx.state.get("_scenario") == "resolve_first":
        ctx.state["remediation_resolved"] = True
    return "verified"


def _summarizer(ctx: Context) -> str:
    ctx.state["remediation_summary"] = "remediation summary"
    return "summarized"


def _build_workflow() -> Workflow:
    """Mirror of the deterministic ``orrery_triage_workflow`` topology, with the
    LLM agent nodes replaced by deterministic stubs but the real routing nodes."""
    join = JoinNode(name="health_join")
    return Workflow(
        name="flow_test_root",
        edges=[
            ("START", (_check_a, _check_b)),
            ((_check_a, _check_b), join),
            (join, _triage, _journal, triage_route),
            (triage_route, {"remediate": _actor, "resolved": final_report}),
            (_actor, _verifier, verify_route),
            (verify_route, {"retry": _actor, "done": _summarizer}),
            (_summarizer, final_report),
        ],
    )


async def _run(scenario: str, **seed) -> dict:
    """Run the graph for a scenario and return the final session state."""
    app = App(name="flow", root_agent=_build_workflow(), plugins=[])
    runner = InMemoryRunner(app=app)
    state = {"_scenario": scenario, **seed}
    session = await runner.session_service.create_session(app_name="flow", user_id="u", state=state)
    msg = types.Content(role="user", parts=[types.Part(text="run a full triage")])
    async for _ in runner.run_async(user_id="u", session_id=session.id, new_message=msg):
        pass
    final = await runner.session_service.get_session(
        app_name="flow", user_id="u", session_id=session.id
    )
    assert final is not None
    return dict(final.state)


@pytest.mark.asyncio
async def test_healthy_path_skips_remediation():
    state = await _run("healthy")
    assert state["incident_severity"] == "healthy"
    # Remediation branch never ran.
    assert "remediation_iteration" not in state
    assert "remediation_summary" not in state


@pytest.mark.asyncio
async def test_critical_resolves_on_first_iteration():
    state = await _run("resolve_first")
    assert state["incident_severity"] == "critical"
    assert state["remediation_resolved"] is True
    assert state["remediation_iteration"] == 1
    assert state["remediation_summary"] == "remediation summary"


@pytest.mark.asyncio
async def test_critical_never_resolves_caps_iterations():
    state = await _run("never_resolve")
    # Loop is bounded — it must stop at the cap, not run forever.
    assert state["remediation_iteration"] == MAX_REMEDIATION_ITERATIONS
    assert not state.get("remediation_resolved")
    assert state["remediation_summary"] == "remediation summary"


@pytest.mark.asyncio
async def test_missing_verdict_with_problem_signal_remediates_and_flags():
    state = await _run("missing_verdict", _seed_k8s_status="pod web-1 in CrashLoopBackOff")
    assert state["triage_verdict_missing"] is True
    assert state["incident_severity"] == "degraded"
    # A real problem was inferred, so remediation ran rather than silently resolving.
    assert state["remediation_iteration"] == MAX_REMEDIATION_ITERATIONS
