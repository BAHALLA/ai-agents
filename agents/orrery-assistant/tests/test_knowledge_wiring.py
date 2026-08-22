"""Structural guard: retrieval must reach the model through a real tool.

ADK offers two things that both look like "Vertex AI Search". Only one of them
is a tool:

- ``DiscoveryEngineSearchTool`` is a ``FunctionTool`` — it executes in the
  runner, so its result passes through ``SafetyScreenPlugin`` (indirect
  prompt-injection neutralization), ``PIIRedactionPlugin``,
  ``ToolOutputCapPlugin`` and ``AuditPlugin``.
- ``VertexAiSearchTool`` is *model built-in grounding*. Its
  ``process_llm_request`` appends a ``types.Retrieval`` to the LLM request
  config and the model retrieves server-side. There is no
  ``after_tool_callback``, so it bypasses **all four** and never appears in the
  audit log.

Retrieved documents are attacker-reachable text — a Confluence page or a
git-hosted runbook is editable by whoever has write access to the source. That
is the same threat model as a pod annotation, and it is exactly what the
after-tool chain exists to handle. Wiring the grounding variant would remove
the protection silently, with no failing test and no log line to notice.

Hence this guard, required by AEP-025.
"""

from __future__ import annotations

import pytest
from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.agent_tool import AgentTool
from google.adk.tools.base_tool import BaseTool
from google.adk.workflow import Workflow

from orrery_assistant.agent import orrery_chat_agent, orrery_triage_workflow
from orrery_core.knowledge import KnowledgeConfig, KnowledgeSearchTool, knowledge_tool

#: Tools that reach the model as grounding config rather than as a tool call.
#: Extend this list, never add an exception to the assertion.
FORBIDDEN_TOOL_NAMES = {
    "VertexAiSearchTool",
    "EnterpriseWebSearchTool",
    "GoogleSearchTool",
}


def _agents_under(root):
    """Yield every agent reachable from *root* — chat tree or workflow graph."""
    seen: set[int] = set()
    stack = [root]
    while stack:
        agent = stack.pop()
        if id(agent) in seen:
            continue
        seen.add(id(agent))
        if isinstance(agent, Workflow):
            stack.extend(agent.graph.nodes or [])
            continue
        if isinstance(agent, LlmAgent):
            yield agent
        for tool in getattr(agent, "tools", []) or []:
            if isinstance(tool, AgentTool):
                stack.append(tool.agent)
        stack.extend(getattr(agent, "sub_agents", []) or [])


@pytest.mark.parametrize("root", [orrery_chat_agent, orrery_triage_workflow])
def test_no_agent_wires_built_in_grounding(root):
    for agent in _agents_under(root):
        for tool in getattr(agent, "tools", []) or []:
            assert type(tool).__name__ not in FORBIDDEN_TOOL_NAMES, (
                f"{agent.name} wires {type(tool).__name__}, which retrieves inside the "
                "model and therefore bypasses safety screening, PII redaction, the "
                "output cap and audit. Use a KnowledgeRetriever backend behind "
                "search_knowledge instead — see AEP-025."
            )


def test_knowledge_tool_is_a_real_tool_when_configured():
    tool = knowledge_tool(KnowledgeConfig(orrery_knowledge_backend="elasticsearch"))
    assert isinstance(tool, KnowledgeSearchTool)
    # Being a BaseTool is what puts it on the after-tool chain.
    assert isinstance(tool, BaseTool)
    assert hasattr(tool, "run_async")


def test_no_search_tool_is_attached_without_a_configured_corpus():
    # The default deployment has no corpus. Advertising a search tool that can
    # only return nothing teaches the model to waste a call on every incident.
    assert knowledge_tool(KnowledgeConfig(orrery_knowledge_backend="none")) is None
    names = {type(t).__name__ for t in orrery_chat_agent.tools}
    assert "KnowledgeSearchTool" not in names


def test_knowledge_search_is_unguarded_so_rbac_lands_it_at_viewer():
    from orrery_core import is_guarded

    tool = knowledge_tool(KnowledgeConfig(orrery_knowledge_backend="elasticsearch"))
    # Retrieval is a read. A guard here would force an approval round-trip on
    # looking something up, which would train operators to approve reflexively.
    assert not is_guarded(tool)
