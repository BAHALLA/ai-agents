"""Maps a Slack thread participant to an ADK session.

A Slack thread is identified by (channel_id, thread_ts) — but a *session* is not.
ADK scopes every session by ``(app_name, user_id, session_id)``, so handing the
session created for the thread's first speaker to a second speaker does not join
them to that conversation: the lookup misses and ADK creates a fresh empty
session behind the same id. Threads are therefore per participant whether or not
the mapping admits it, and mapping ``(channel, thread_ts)`` alone only hid that —
while making it look as though history were shared.

The key is ``(channel_id, thread_ts, user_id)``: one session per person per
thread, which is what actually happens. Cross-participant continuity, if it is
ever wanted, needs a shared-history mechanism (a summary in ``app:``-scoped
state, or long-term memory), not a reused session id.
"""

from __future__ import annotations


class SessionMap:
    """In-memory mapping from a (thread, participant) pair to an ADK session ID."""

    def __init__(self) -> None:
        self._map: dict[tuple[str, str, str], str] = {}

    def get(self, channel: str, thread_ts: str, user_id: str) -> str | None:
        """Look up this participant's existing session ID for a thread."""
        return self._map.get((channel, thread_ts, user_id))

    def set(self, channel: str, thread_ts: str, user_id: str, session_id: str) -> None:
        """Store a session mapping for one participant in a thread."""
        self._map[(channel, thread_ts, user_id)] = session_id

    def remove(self, channel: str, thread_ts: str, user_id: str | None = None) -> None:
        """Forget mappings for a thread (e.g. on session expiry).

        Without *user_id*, every participant's mapping for the thread is dropped:
        expiry is a property of the thread, not of one speaker.
        """
        if user_id is not None:
            self._map.pop((channel, thread_ts, user_id), None)
            return
        for key in [k for k in self._map if k[0] == channel and k[1] == thread_ts]:
            del self._map[key]
