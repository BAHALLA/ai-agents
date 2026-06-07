"""Tests for ``apply_chat_confirmation`` tree-walking."""

from __future__ import annotations

from typing import Any

from google_chat_bot.confirmation import ConfirmationStore, apply_chat_confirmation


class _FakeTool:
    """Minimal ADK AgentTool stand-in — carries a wrapped agent."""

    def __init__(self, agent: Any | None = None) -> None:
        self.agent = agent


class _FakeLlmAgent:
    """LlmAgent stand-in — identified by having a ``tools`` attribute."""

    def __init__(
        self,
        name: str,
        tools: list[Any] | None = None,
        sub_agents: list[Any] | None = None,
    ) -> None:
        self.name = name
        self.tools = tools if tools is not None else []
        self.sub_agents = sub_agents or []
        self.before_tool_callback: Any = None


class _FakeWorkflowAgent:
    """SequentialAgent/ParallelAgent/LoopAgent stand-in — no ``tools``."""

    def __init__(self, name: str, sub_agents: list[Any] | None = None) -> None:
        self.name = name
        self.sub_agents = sub_agents or []


def test_walks_sub_agents_and_agent_tools():
    """Every LlmAgent reachable via sub_agents or AgentTool gets wired."""
    # Root → SequentialAgent → two leaf LlmAgents
    leaf_a = _FakeLlmAgent("leaf_a")
    leaf_b = _FakeLlmAgent("leaf_b")
    seq = _FakeWorkflowAgent("seq", sub_agents=[leaf_a, leaf_b])

    # Root also exposes two AgentTool-wrapped specialists
    specialist_x = _FakeLlmAgent("specialist_x")
    specialist_y = _FakeLlmAgent("specialist_y")
    root = _FakeLlmAgent(
        "root",
        tools=[_FakeTool(specialist_x), _FakeTool(specialist_y), object()],
        sub_agents=[seq],
    )

    store = ConfirmationStore()
    wired = apply_chat_confirmation(root, store)

    # 1 root + 2 leaves + 2 specialists = 5 LlmAgents; workflow agent skipped.
    assert wired == 5
    for agent in (root, leaf_a, leaf_b, specialist_x, specialist_y):
        assert callable(agent.before_tool_callback), f"{agent.name} not wired"

    # Non-agent tool entries (``object()``) are tolerated.


def test_skips_workflow_agents():
    """Workflow agents have no tools attribute and should be skipped."""
    workflow = _FakeWorkflowAgent("workflow_only")
    store = ConfirmationStore()
    wired = apply_chat_confirmation(workflow, store)
    assert wired == 0
    assert not hasattr(workflow, "before_tool_callback") or (
        workflow.__dict__.get("before_tool_callback") is None
    )


def test_cycle_safe():
    """A child that points back to its parent must not loop forever."""
    a = _FakeLlmAgent("a")
    b = _FakeLlmAgent("b", sub_agents=[a])
    a.sub_agents.append(b)  # cycle
    store = ConfirmationStore()
    wired = apply_chat_confirmation(a, store)
    assert wired == 2  # each visited exactly once


def test_idempotent_rewire():
    """Calling twice is safe — second pass overwrites with same callback."""
    leaf = _FakeLlmAgent("leaf")
    root = _FakeLlmAgent("root", sub_agents=[leaf])
    store = ConfirmationStore()
    apply_chat_confirmation(root, store)
    first = root.before_tool_callback
    apply_chat_confirmation(root, store)
    # The callback is recreated each call, but the wiring doesn't error
    # and remains callable.
    assert callable(root.before_tool_callback)
    assert first is not root.before_tool_callback  # fresh closure on re-wire


def test_real_orrery_assistant_tree():
    """End-to-end: apply the walker to the actual root agent graph.

    This is the test that would have caught the original regression
    (sub-agent tools falling back to CLI-style text confirmation).
    """
    from orrery_assistant.agent import orrery_triage_workflow, root_agent

    store = ConfirmationStore()
    
    # 1. The interactive root (orrery_chat_agent) has 6 specialist AgentTools + incident_triage_agent.
    wired_chat = apply_chat_confirmation(root_agent, store)
    assert wired_chat >= 7, f"expected ≥7 LlmAgents wired, got {wired_chat}"
    
    # 2. The deterministic graph (orrery_triage_workflow) exposes its tool-calling LlmAgents
    # as graph nodes.
    wired_graph = apply_chat_confirmation(orrery_triage_workflow, store)
    assert wired_graph >= 8, f"expected ≥8 LlmAgents wired in graph, got {wired_graph}"

    # Spot-check a known graph node with destructive tools: the remediation
    # actor must carry the Chat callback so restart/scale/rollback fire an
    # interactive Card instead of plain-text confirmation.
    graph = orrery_triage_workflow.graph
    nodes = graph.nodes if graph is not None else ()
    actor = next((n for n in nodes if getattr(n, "name", "") == "remediation_actor"), None)
    assert actor is not None, "remediation_actor node not found in graph"
    assert callable(getattr(actor, "before_tool_callback", None))
