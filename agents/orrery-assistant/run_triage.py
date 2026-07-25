"""Run the deterministic incident-triage Workflow once (batch / scheduled).

Unlike the interactive ``orrery_chat_agent`` root, this drives the graph-native
``orrery_triage_workflow`` end-to-end: parallel health checks → triage → journal
→ conditional closed-loop remediation. Intended for cron / CI / on-call sweeps.

Usage:
    uv run python run_triage.py

Destructive remediation tools stay gated by the guardrail plugins: without an
interactive confirmation transport they return ``confirmation_required`` rather
than executing, so an unattended run never auto-applies a destructive action.
"""

import asyncio

from google.adk.apps import App
from google.adk.runners import InMemoryRunner
from google.genai import types

from orrery_assistant.agent import orrery_triage_workflow
from orrery_core import (
    create_events_compaction_config,
    default_plugins,
    load_agent_env,
    set_user_role,
)

load_agent_env(__file__)

APP_NAME = "orrery_triage"


async def main() -> None:
    app = App(
        name=APP_NAME,
        root_agent=orrery_triage_workflow,
        plugins=default_plugins(enable_memory=True),
        # A full sweep fans out to five specialists and can loop through
        # remediation three times, so this root produces the longest transcripts
        # of any surface. The explicit summarizer in the factory is required
        # here: the root is a Workflow, and ADK raises rather than deriving a
        # summarizer model from a non-LlmAgent root.
        events_compaction_config=create_events_compaction_config(),
    )
    runner = InMemoryRunner(app=app)

    # Operator role so read-only checks run; destructive remediation is still
    # gated by the confirmation guardrail (no transport here → not executed).
    state: dict[str, object] = {}
    set_user_role(state, "operator")
    session = await runner.session_service.create_session(
        app_name=APP_NAME, user_id="batch", state=state
    )

    msg = types.Content(role="user", parts=[types.Part(text="run a full triage")])
    async for event in runner.run_async(user_id="batch", session_id=session.id, new_message=msg):
        output = getattr(event, "output", None)
        if output is not None:
            print(output)


if __name__ == "__main__":
    asyncio.run(main())
