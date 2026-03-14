import os
from pathlib import Path
from typing import Any, Callable, Sequence

from dotenv import load_dotenv
from google.adk.agents import Agent


def load_agent_env(agent_file: str) -> None:
    """Load the .env file located next to the given agent module file.

    Usage in an agent's agent.py:
        load_agent_env(__file__)
    """
    env_path = Path(agent_file).parent / ".env"
    load_dotenv(dotenv_path=env_path)


def create_agent(
    *,
    name: str,
    description: str,
    instruction: str,
    tools: Sequence[Callable[..., Any]],
    model: str | None = None,
    sub_agents: Sequence[Agent] | None = None,
) -> Agent:
    """Create an ADK Agent with sensible defaults.

    The model defaults to the GEMINI_MODEL_VERSION env var, falling back
    to 'gemini-2.0-flash'.
    """
    resolved_model = model or os.getenv("GEMINI_MODEL_VERSION", "gemini-2.0-flash")

    kwargs: dict[str, Any] = {
        "name": name,
        "model": resolved_model,
        "description": description,
        "instruction": instruction,
        "tools": list(tools),
    }

    if sub_agents:
        kwargs["sub_agents"] = list(sub_agents)

    return Agent(**kwargs)
