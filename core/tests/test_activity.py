"""Unit tests for the activity_tracker after_tool_callback."""

from unittest.mock import MagicMock

from orrery_core.observability.activity import activity_tracker


def _make_tool_context(state=None):
    """Build a fake ToolContext with the given state dict."""
    ctx = MagicMock()
    ctx.state = state if state is not None else {}
    ctx.agent_name = "test_agent"
    return ctx


def _make_tool(name="my_tool"):
    tool = MagicMock()
    tool.name = name
    return tool


def test_activity_tracker_creates_session_log():
    tracker = activity_tracker()
    ctx = _make_tool_context()

    result = tracker(
        tool=_make_tool("list_pods"),
        args={"namespace": "default"},
        tool_context=ctx,
        tool_response={"status": "success", "count": 5},
    )

    assert result is None  # should not modify tool response
    assert len(ctx.state["session_log"]) == 1
    entry = ctx.state["session_log"][0]
    assert entry["operation"] == "list_pods"
    assert "test_agent" in entry["details"]
    assert "namespace=default" in entry["details"]
    assert "success" in entry["details"]
    assert "timestamp" in entry


def test_activity_tracker_appends_to_existing_log():
    tracker = activity_tracker()
    existing_entry = {"operation": "old_op", "details": "old", "timestamp": "t0"}
    ctx = _make_tool_context(state={"session_log": [existing_entry]})

    tracker(
        tool=_make_tool("new_tool"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success"},
    )

    assert len(ctx.state["session_log"]) == 2
    assert ctx.state["session_log"][0]["operation"] == "old_op"
    assert ctx.state["session_log"][1]["operation"] == "new_tool"


def test_activity_tracker_handles_non_dict_response():
    tracker = activity_tracker()
    ctx = _make_tool_context()

    tracker(
        tool=_make_tool("some_tool"),
        args={"key": "val"},
        tool_context=ctx,
        tool_response="plain string",
    )

    entry = ctx.state["session_log"][0]
    assert "ok" in entry["details"]


def test_activity_tracker_excludes_ctx_from_args():
    tracker = activity_tracker()
    ctx = _make_tool_context()

    tracker(
        tool=_make_tool("tool_with_ctx"),
        args={"ctx": "<ToolContext>", "query": "up"},
        tool_context=ctx,
        tool_response={"status": "success"},
    )

    entry = ctx.state["session_log"][0]
    assert "ctx" not in entry["details"]
    assert "query=up" in entry["details"]


def test_activity_tracker_handles_empty_args():
    tracker = activity_tracker()
    ctx = _make_tool_context()

    tracker(
        tool=_make_tool("no_args_tool"),
        args={},
        tool_context=ctx,
        tool_response={"status": "success"},
    )

    entry = ctx.state["session_log"][0]
    assert "(no args)" in entry["details"]


def test_activity_tracker_handles_missing_agent_name():
    tracker = activity_tracker()
    ctx = MagicMock(spec=[])  # no agent_name attribute
    ctx.state = {}

    tracker(
        tool=_make_tool("tool"),
        args={"a": 1},
        tool_context=ctx,
        tool_response={"status": "error"},
    )

    entry = ctx.state["session_log"][0]
    assert "unknown" in entry["details"]
    assert "error" in entry["details"]


def test_session_log_is_bounded(fake_tool, fake_ctx):
    """The log is rewritten whole on every call and ADK copies the assigned
    value into the event's state delta, so an unbounded list makes a session's
    write volume grow with the square of its length."""
    callback = activity_tracker(max_entries=5)
    tool = fake_tool(name="list_pods")
    ctx = fake_ctx()

    for i in range(20):
        callback(tool=tool, args={"i": i}, tool_context=ctx, tool_response={"status": "ok"})

    log = ctx.state["session_log"]
    assert len(log) == 5
    # The most recent survive — the log is read for what just happened.
    assert [entry["details"].split("i=")[1].split(" ")[0] for entry in log] == [
        "15",
        "16",
        "17",
        "18",
        "19",
    ]


def test_default_bound_is_applied(fake_tool, fake_ctx):
    from orrery_core.observability.activity import MAX_SESSION_LOG_ENTRIES

    callback = activity_tracker()
    tool = fake_tool(name="list_pods")
    ctx = fake_ctx()

    for i in range(MAX_SESSION_LOG_ENTRIES + 25):
        callback(tool=tool, args={"i": i}, tool_context=ctx, tool_response={"status": "ok"})

    assert len(ctx.state["session_log"]) == MAX_SESSION_LOG_ENTRIES
