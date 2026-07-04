"""Run orrery-assistant with persistent sessions and long-term memory.

Both the session store and the long-term memory store share the same backend:
in-memory by default, or PostgreSQL when ``DATABASE_URL`` is set (required for
multi-replica deployments). With PostgreSQL, conversation state *and*
cross-session recall survive restarts. ``run_persistent`` builds the memory
service automatically.

Usage:
    uv run python run_persistent.py
"""

import asyncio

from orrery_assistant.agent import root_agent
from orrery_core import default_plugins, run_persistent
from orrery_core.serving.runner import create_context_cache_config

if __name__ == "__main__":
    asyncio.run(
        run_persistent(
            root_agent,
            app_name="orrery_assistant",
            plugins=default_plugins(enable_memory=True),
            context_cache_config=create_context_cache_config(),
        )
    )
