"""Run ops-journal-agent with persistent sessions and long-term memory.

Sessions and long-term memory share the same backend: in-memory by default, or
PostgreSQL when ``DATABASE_URL`` is set. With PostgreSQL both survive restarts.
``run_persistent`` builds the memory service automatically.

Usage:
    uv run python run_persistent.py
"""

import asyncio

from ops_journal_agent.agent import root_agent
from orrery_core import default_plugins, run_persistent

if __name__ == "__main__":
    asyncio.run(
        run_persistent(
            root_agent,
            app_name="ops_journal",
            plugins=default_plugins(enable_memory=True),
        )
    )
