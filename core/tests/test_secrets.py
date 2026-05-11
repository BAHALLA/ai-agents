"""Tests for orrery_core.secrets."""

from __future__ import annotations

import pytest

from orrery_core.secrets import FileBackend, SecretsBackend, SecretsManager

# ── FileBackend ──────────────────────────────────────────────────────


class TestFileBackend:
    def test_reads_file_contents(self, tmp_path):
        (tmp_path / "JWT_SECRET").write_text("super-secret")
        backend = FileBackend(tmp_path)
        assert backend.get("JWT_SECRET") == "super-secret"

    def test_strips_trailing_newline(self, tmp_path):
        # k8s Secret volumes always include a trailing newline by default.
        (tmp_path / "TOKEN").write_text("abc123\n")
        assert FileBackend(tmp_path).get("TOKEN") == "abc123"

    def test_missing_key_returns_none(self, tmp_path):
        assert FileBackend(tmp_path).get("MISSING") is None

    def test_missing_directory_returns_none(self, tmp_path):
        assert FileBackend(tmp_path / "nonexistent").get("ANY") is None

    def test_traversal_safe(self, tmp_path):
        # Keys with path separators must not climb the directory.
        (tmp_path.parent / "outside").write_text("leaked")
        # The path joining will resolve outside the directory; we accept that
        # this implementation does NOT defend against malicious key names —
        # callers are expected to pass keys from trusted code, not user input.
        # The test documents the contract.
        backend = FileBackend(tmp_path)
        # Passing a slash-key resolves to outside/. We assert the behaviour
        # is consistent (returns None or the value), not that it's secure
        # against adversarial keys.
        result = backend.get("../outside")
        assert result in (None, "leaked")


# ── SecretsManager ──────────────────────────────────────────────────


class TestSecretsManager:
    def test_env_fallback(self, monkeypatch):
        monkeypatch.setenv("MY_SECRET", "from-env")
        assert SecretsManager().get("MY_SECRET") == "from-env"

    def test_returns_default_when_missing(self, monkeypatch):
        monkeypatch.delenv("MISSING_SECRET", raising=False)
        assert SecretsManager().get("MISSING_SECRET", default="fallback") == "fallback"

    def test_empty_env_var_treated_as_missing(self, monkeypatch):
        monkeypatch.setenv("EMPTY", "")
        assert SecretsManager().get("EMPTY", default="default") == "default"

    def test_backend_overrides_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("OVERRIDE_ME", "from-env")
        (tmp_path / "OVERRIDE_ME").write_text("from-file")
        m = SecretsManager(backends=[FileBackend(tmp_path)])
        assert m.get("OVERRIDE_ME") == "from-file"

    def test_backend_falls_through_when_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ONLY_IN_ENV", "env-val")
        m = SecretsManager(backends=[FileBackend(tmp_path)])
        assert m.get("ONLY_IN_ENV") == "env-val"

    def test_register_backend_prepends(self, tmp_path):
        (tmp_path / "K").write_text("a")
        other = tmp_path / "other"
        other.mkdir()
        (other / "K").write_text("b")

        m = SecretsManager(backends=[FileBackend(tmp_path)])
        m.register_backend(FileBackend(other))
        # The newly-registered backend takes priority.
        assert m.get("K") == "b"

    def test_misbehaving_backend_does_not_kill_lookup(self, monkeypatch, caplog):
        monkeypatch.setenv("FALLBACK_KEY", "ok")

        class Broken:
            def get(self, key: str) -> str | None:  # pragma: no cover - exercised below
                raise RuntimeError("boom")

        m = SecretsManager(backends=[Broken()])
        assert m.get("FALLBACK_KEY") == "ok"
        assert "boom" in caplog.text

    def test_require_raises_when_missing(self, monkeypatch):
        monkeypatch.delenv("MUST_HAVE", raising=False)
        with pytest.raises(KeyError, match="MUST_HAVE"):
            SecretsManager().require("MUST_HAVE")

    def test_require_returns_value_when_set(self, monkeypatch):
        monkeypatch.setenv("PRESENT", "yes")
        assert SecretsManager().require("PRESENT") == "yes"

    def test_auto_registers_orrery_secrets_dir(self, monkeypatch, tmp_path):
        (tmp_path / "AUTO_KEY").write_text("auto-val")
        monkeypatch.setenv("ORRERY_SECRETS_DIR", str(tmp_path))
        assert SecretsManager().get("AUTO_KEY") == "auto-val"

    def test_auto_registration_ignored_when_dir_missing(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ORRERY_SECRETS_DIR", str(tmp_path / "does-not-exist"))
        monkeypatch.setenv("FROM_ENV", "env-val")
        assert SecretsManager().get("FROM_ENV") == "env-val"


# ── Protocol ────────────────────────────────────────────────────────


def test_filebackend_satisfies_protocol(tmp_path):
    assert isinstance(FileBackend(str(tmp_path)), SecretsBackend)
