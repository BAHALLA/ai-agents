"""Tests for orrery_core.plugins.output_cap_plugin."""

from __future__ import annotations

import json

import pytest

from orrery_core.plugins.output_cap_plugin import (
    DEFAULT_MAX_TOOL_RESULT_BYTES,
    ToolOutputCapPlugin,
    cap_result,
)

# ── cap_result ─────────────────────────────────────────────────────────


def test_cap_result_passes_small_untouched():
    small = {"status": "ok", "stdout": "short"}
    assert cap_result(small, 1000) is None  # under budget → unchanged


def test_cap_result_disabled_when_budget_non_positive():
    assert cap_result({"stdout": "Z" * 10_000}, 0) is None
    assert cap_result({"stdout": "Z" * 10_000}, -1) is None


def test_cap_result_truncates_largest_string_field_preserving_structure():
    result = {"status": "ok", "stdout": "A" * 100_000, "stderr": ""}
    capped = cap_result(result, 4096)

    assert capped is not None
    assert capped["status"] == "ok"  # small fields survive
    assert "_truncated" in capped
    assert len(capped["stdout"]) < 100_000
    assert len(json.dumps(capped, ensure_ascii=False).encode()) <= 4096


def test_cap_result_trims_long_list_field_in_dict():
    rows = [{"i": i, "payload": "x" * 100} for i in range(500)]
    result = {"status": "ok", "rows": rows}
    capped = cap_result(result, 4096)

    assert capped is not None
    assert capped["status"] == "ok"
    assert 0 < len(capped["rows"]) < 500
    assert "_list_truncated" in capped
    # kept elements are whole records, not truncated strings
    assert all(isinstance(r, dict) and "payload" in r for r in capped["rows"])
    assert len(json.dumps(capped, ensure_ascii=False).encode()) <= 4096


def test_cap_result_truncates_top_level_list_keeping_records():
    rows = [{"i": i, "payload": "x" * 100} for i in range(500)]
    capped = cap_result(rows, 4096)

    assert capped is not None
    assert capped["status"] == "truncated"
    assert capped["total_items"] == 500
    assert capped["returned_items"] == len(capped["items"]) > 0
    assert len(json.dumps(capped, ensure_ascii=False).encode()) <= 4096


def test_cap_result_generic_fallback_for_huge_string():
    capped = cap_result("X" * 100_000, 2048)

    assert capped is not None
    assert capped["status"] == "truncated"
    assert "output" in capped
    assert len(json.dumps(capped, ensure_ascii=False).encode()) <= 2048


def test_cap_result_list_of_huge_records_falls_back():
    # A single record alone overflows → generic string-head fallback.
    capped = cap_result([{"blob": "Z" * 10_000}], 512)

    assert capped is not None
    assert capped["status"] == "truncated"
    assert "output" in capped
    assert len(json.dumps(capped, ensure_ascii=False).encode()) <= 512


def test_cap_result_multibyte_safe():
    # Truncation must not split a UTF-8 codepoint.
    capped = cap_result({"stdout": "é" * 50_000}, 2048)

    assert capped is not None
    json.dumps(capped, ensure_ascii=False).encode("utf-8")  # must not raise


# ── plugin ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_output_cap_plugin_replaces_only_oversized(fake_tool, fake_ctx):
    plugin = ToolOutputCapPlugin(max_bytes=1024)
    tool = fake_tool(name="get_logs")
    ctx = fake_ctx()

    small = {"status": "ok"}
    assert (
        await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=small)
        is None
    )

    big = {"status": "ok", "logs": "L" * 10_000}
    capped = await plugin.after_tool_callback(tool=tool, tool_args={}, tool_context=ctx, result=big)
    assert capped is not None
    assert capped["status"] == "ok"
    assert "_truncated" in capped


def test_default_cap_is_4_mib():
    assert DEFAULT_MAX_TOOL_RESULT_BYTES == 4 * 1024 * 1024
