"""Tests for PIIRedactionPlugin (AEP-013)."""

from unittest.mock import MagicMock

import pytest

from orrery_core.plugins import PIIRedactionPlugin, default_plugins
from orrery_core.plugins.pii_plugin import REDACTED, REDACTED_IP, redact_structure

# ── redact_structure: key-based ──────────────────────────────────────


class TestKeyBasedRedaction:
    def test_credential_keys_redacted(self):
        data = {
            "password": "hunter2",
            "access_token": "abc123",
            "api_key": "xyz",
            "client_secret": "shh",
            "PRIVATE_KEY": "----",
        }
        count = redact_structure(data)
        assert count == 5
        assert all(v == REDACTED for v in data.values())

    def test_non_credential_keys_untouched(self):
        data = {"status": "ok", "broker_count": 3, "token_count": 512, "tokens_used": 90}
        assert redact_structure(data) == 0
        assert data["token_count"] == 512

    def test_pagination_cursors_allowlisted(self):
        data = {"next_page_token": "opaque-cursor", "page_token": "abc"}
        assert redact_structure(data) == 0
        assert data["next_page_token"] == "opaque-cursor"

    def test_camelcase_and_pascalcase_keys_redacted(self):
        data = {
            "dbPassword": "pw",
            "accessToken": "tok",
            "AccessToken": "tok",
            "clientSecret": "shh",
            "APIKey": "k",
        }
        count = redact_structure(data)
        assert count == 5
        assert all(v == REDACTED for v in data.values())

    def test_camelcase_pagination_cursor_allowlisted(self):
        """camelCase cursors normalize onto the snake_case allowlist."""
        data = {"nextPageToken": "opaque-cursor", "pageToken": "abc"}
        assert redact_structure(data) == 0
        assert data["nextPageToken"] == "opaque-cursor"

    def test_camelcase_non_credentials_untouched(self):
        data = {"tokenCount": 5, "monKey": "x"}
        assert redact_structure(data) == 0

    def test_llm_usage_counts_untouched(self):
        """Plural token endings are usage counts, not credentials."""
        data = {"maxTokens": 100, "prompt_tokens": 10, "total_tokens": 15}
        assert redact_structure(data) == 0
        assert data == {"maxTokens": 100, "prompt_tokens": 10, "total_tokens": 15}

    def test_nested_structures(self):
        data = {"pods": [{"env": {"DB_PASSWORD": "pw", "DB_HOST": "postgres"}}]}
        assert redact_structure(data) == 1
        assert data["pods"][0]["env"]["DB_PASSWORD"] == REDACTED
        assert data["pods"][0]["env"]["DB_HOST"] == "postgres"

    def test_empty_values_left_alone(self):
        data = {"password": "", "token": None}
        assert redact_structure(data) == 0


# ── redact_structure: value patterns ─────────────────────────────────


class TestValuePatternRedaction:
    def test_kv_pair_in_log_line(self):
        data = {"logs": "2026-01-01 conn ok password=hunter2 retry=3"}
        assert redact_structure(data) == 1
        assert "hunter2" not in data["logs"]
        assert "retry=3" in data["logs"]

    def test_pem_block(self):
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----"
        data = {"config": f"cert follows: {pem}"}
        redact_structure(data)
        assert "MIIEow" not in data["config"]

    def test_provider_token_shapes(self):
        data = {
            "lines": [
                "aws key AKIAIOSFODNN7EXAMPLE found",
                "gh token ghp_abcdefghijklmnopqrstuvwxyz012345 in env",
                "jwt eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpM",
            ]
        }
        assert redact_structure(data) == 3
        assert all(REDACTED in line for line in data["lines"])

    def test_plain_text_untouched(self):
        data = {"summary": "3 brokers healthy, ISR stable, no under-replicated partitions"}
        assert redact_structure(data) == 0


# ── IP redaction (opt-in) ────────────────────────────────────────────


class TestIPRedaction:
    def test_ips_kept_by_default(self):
        data = {"endpoint": "broker at 10.0.0.12:9092"}
        redact_structure(data)
        assert "10.0.0.12" in data["endpoint"]

    def test_ips_redacted_when_opted_in(self):
        data = {"endpoint": "broker at 10.0.0.12:9092"}
        redact_structure(data, redact_ips=True)
        assert REDACTED_IP in data["endpoint"]
        assert "10.0.0.12" not in data["endpoint"]


# ── Plugin behavior ──────────────────────────────────────────────────


class TestPIIRedactionPlugin:
    @pytest.mark.asyncio
    async def test_mutates_in_place_and_returns_none(self):
        """Non-None would early-exit ADK's after-tool chain and skip observers."""
        plugin = PIIRedactionPlugin()
        result = {"password": "hunter2", "status": "ok"}
        returned = await plugin.after_tool_callback(
            tool=MagicMock(name="tool"), tool_args={}, tool_context=MagicMock(), result=result
        )
        assert returned is None
        assert result["password"] == REDACTED
        assert result["status"] == "ok"

    @pytest.mark.asyncio
    async def test_non_dict_result_is_safe(self):
        plugin = PIIRedactionPlugin()
        returned = await plugin.after_tool_callback(
            tool=MagicMock(), tool_args={}, tool_context=MagicMock(), result="plain string"
        )
        assert returned is None


# ── default_plugins wiring ───────────────────────────────────────────


class TestDefaultPluginsWiring:
    def test_registered_by_default(self, monkeypatch):
        monkeypatch.delenv("ORRERY_PII_REDACTION", raising=False)
        plugins = default_plugins()
        assert any(isinstance(p, PIIRedactionPlugin) for p in plugins)

    def test_env_flag_disables(self, monkeypatch):
        monkeypatch.setenv("ORRERY_PII_REDACTION", "false")
        plugins = default_plugins()
        assert not any(isinstance(p, PIIRedactionPlugin) for p in plugins)

    def test_registered_before_audit(self, monkeypatch):
        """Audit must observe redacted results, so PII comes first."""
        from orrery_core.plugins import AuditPlugin

        monkeypatch.delenv("ORRERY_PII_REDACTION", raising=False)
        plugins = default_plugins()
        names = [type(p).__name__ for p in plugins]
        assert names.index("PIIRedactionPlugin") < names.index(AuditPlugin.__name__)
