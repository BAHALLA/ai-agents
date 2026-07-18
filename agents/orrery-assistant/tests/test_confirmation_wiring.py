"""Structural guard: every chat-reachable agent with guarded tools must wire
the confirmation gate.

The GuardrailsPlugin enforces RBAC only; human-in-the-loop confirmation for
``@confirm``/``@destructive`` tools comes from each agent's
``before_tool_callback=require_confirmation()``. The docker specialist shipped
without it, so on the HTTP/console path (where no transport-level tree walker
runs) a destructive ``remove_image`` executed with no approval. This test
walks the chat root's ``AgentTool`` tree and fails if any agent exposes a
guarded tool without a before-tool gate — so that class of gap cannot ship
again.
"""

from google.adk.tools.agent_tool import AgentTool

from orrery_assistant.agent import orrery_chat_agent
from orrery_core import is_guarded


def _agents_under(root):
    """Yield root plus every agent reachable via AgentTool / sub_agents."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        agent = stack.pop()
        if id(agent) in seen:
            continue
        seen.add(id(agent))
        yield agent
        for tool in getattr(agent, "tools", []) or []:
            if isinstance(tool, AgentTool):
                stack.append(tool.agent)
        stack.extend(getattr(agent, "sub_agents", []) or [])


def _guarded_tools(agent):
    return [
        getattr(tool, "func", tool)
        for tool in getattr(agent, "tools", []) or []
        if not isinstance(tool, AgentTool) and is_guarded(getattr(tool, "func", tool))
    ]


def test_every_agent_with_guarded_tools_wires_a_before_tool_gate():
    unguarded_agents = []
    for agent in _agents_under(orrery_chat_agent):
        guarded = _guarded_tools(agent)
        if guarded and not getattr(agent, "before_tool_callback", None):
            unguarded_agents.append(
                (agent.name, sorted(getattr(t, "__name__", str(t)) for t in guarded))
            )
    assert not unguarded_agents, (
        "Agents exposing @confirm/@destructive tools without a "
        f"before_tool_callback confirmation gate: {unguarded_agents}. "
        "Wire before_tool_callback=require_confirmation() in their create_agent()."
    )


def test_docker_specialist_is_gated():
    """The specific regression: docker's destructive tools ran unconfirmed."""
    from docker_agent.agent import root_agent as docker_root

    assert docker_root.before_tool_callback is not None
    assert any(getattr(t, "__name__", "") == "remove_image" for t in _guarded_tools(docker_root))
