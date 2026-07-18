"""Tests for orrery_core.serving.server (FastAPI HTTP front door)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest

# Import-time skip if FastAPI extras aren't installed.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from orrery_core.security.auth import AUTH_STATE_KEY, JWTConfig  # noqa: E402
from orrery_core.serving.server import ServerConfig, create_app  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────


_TEST_SECRET = "x" * 64  # 32+ bytes to silence PyJWT's HMAC length warning


def _hs256(claims: dict, secret: str = _TEST_SECRET) -> str:
    return pyjwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture
def mock_session():
    session = MagicMock()
    session.id = "sess-1"
    session.state = {}
    return session


@pytest.fixture
def patched_runner(mock_session):
    """Patch the ADK Runner + session service in server.create_app."""

    async def fake_run_async(*, user_id, session_id, new_message, state_delta=None):
        # Record the per-turn state_delta (where identity now travels), then
        # yield a single event echoing the input.
        session_service.last_state_delta = state_delta
        text = new_message.parts[0].text
        event = MagicMock()
        part = MagicMock()
        part.text = f"echo:{text}"
        part.thought = None
        event.content.parts = [part]
        yield event

    runner = MagicMock()
    runner.run_async = fake_run_async

    session_service = MagicMock()
    session_service.create_session = AsyncMock(return_value=mock_session)
    session_service.get_session = AsyncMock(return_value=None)
    session_service.last_state_delta = None

    with (
        patch("orrery_core.serving.gateway.Runner", return_value=runner),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.server.create_session_service", return_value=session_service),
    ):
        yield session_service


@pytest.fixture
def app_with_auth(patched_runner):
    config = ServerConfig(
        auth_enabled=True,
        jwt=JWTConfig(algorithm="HS256", secret=_TEST_SECRET, audience="orrery"),
    )
    return create_app(
        root_agent=MagicMock(name="root"),
        app_name="test",
        plugins=[],
        config=config,
    )


@pytest.fixture
def app_no_auth(patched_runner):
    config = ServerConfig(auth_enabled=False, jwt=JWTConfig(algorithm="HS256", secret="x"))
    return create_app(
        root_agent=MagicMock(name="root"),
        app_name="test",
        plugins=[],
        config=config,
    )


# ── /healthz, /readyz ───────────────────────────────────────────────


def test_healthz_does_not_require_auth(app_with_auth):
    client = TestClient(app_with_auth)
    assert client.get("/healthz").status_code == 200
    assert client.get("/readyz").status_code == 200


# ── /chat auth gates ────────────────────────────────────────────────


def test_chat_without_bearer_returns_401(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_chat_with_invalid_token_returns_401(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.post(
        "/chat",
        headers={"Authorization": "Bearer not-a-jwt"},
        json={"message": "hi"},
    )
    assert r.status_code == 401
    # The error detail must not leak verification internals.
    assert r.json()["detail"] == "Invalid token"


def test_chat_with_valid_token_dispatches_to_runner(app_with_auth, patched_runner, mock_session):
    token = _hs256(
        {
            "sub": "alice",
            "roles": ["operator"],
            "exp": int(time.time()) + 600,
            "aud": "orrery",
        }
    )
    client = TestClient(app_with_auth)
    r = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "hello"},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-1"
    assert body["response"] == "echo:hello"

    # Session was created under the JWT subject; identity is carried per-turn
    # in state_delta (applied before the agent runs) rather than seeded at create.
    patched_runner.create_session.assert_called_once()
    assert patched_runner.create_session.call_args.kwargs["user_id"] == "alice"
    delta = patched_runner.last_state_delta
    assert delta[AUTH_STATE_KEY]["subject"] == "alice"
    assert delta[AUTH_STATE_KEY]["role"] == "operator"


def test_chat_excludes_thinking_parts(mock_session):
    """Gemini planner thought parts must not appear in the /chat response."""

    async def fake_run_async(*, user_id, session_id, new_message, state_delta=None):
        event = MagicMock()
        thought = MagicMock()
        thought.text = "Let me reason about this..."
        thought.thought = True
        answer = MagicMock()
        answer.text = "The cluster is healthy."
        answer.thought = None
        event.content.parts = [thought, answer]
        yield event

    runner = MagicMock()
    runner.run_async = fake_run_async
    session_service = MagicMock()
    session_service.create_session = AsyncMock(return_value=mock_session)
    session_service.get_session = AsyncMock(return_value=None)

    with (
        patch("orrery_core.serving.gateway.Runner", return_value=runner),
        patch("orrery_core.serving.gateway.App", return_value=MagicMock()),
        patch("orrery_core.serving.server.create_session_service", return_value=session_service),
    ):
        config = ServerConfig(auth_enabled=False, jwt=JWTConfig(algorithm="HS256", secret="x"))
        app = create_app(root_agent=MagicMock(), app_name="test", plugins=[], config=config)
        client = TestClient(app)
        r = client.post("/chat", json={"message": "status?"})

    assert r.status_code == 200
    assert r.json()["response"] == "The cluster is healthy."


def test_chat_reuses_existing_session_and_restamps_auth(app_with_auth, patched_runner):
    existing = MagicMock()
    existing.id = "sess-existing"
    existing.state = {AUTH_STATE_KEY: {"subject": "alice", "role": "viewer", "claims": {}}}
    patched_runner.get_session.return_value = existing

    token = _hs256(
        {
            "sub": "alice",
            "roles": ["admin"],
            "exp": int(time.time()) + 600,
            "aud": "orrery",
        }
    )
    client = TestClient(app_with_auth)
    r = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "hi", "session_id": "sess-existing"},
    )

    assert r.status_code == 200
    # Auth context is re-stamped each turn via state_delta with the latest role.
    assert patched_runner.last_state_delta[AUTH_STATE_KEY]["role"] == "admin"
    patched_runner.create_session.assert_not_called()


def test_chat_with_expired_token_returns_401(app_with_auth):
    token = _hs256({"sub": "alice", "exp": int(time.time()) - 100})
    client = TestClient(app_with_auth)
    r = client.post(
        "/chat",
        headers={"Authorization": f"Bearer {token}"},
        json={"message": "hi"},
    )
    assert r.status_code == 401


# ── auth_enabled=False path ─────────────────────────────────────────


def test_chat_without_token_when_auth_disabled(app_no_auth, patched_runner):
    client = TestClient(app_no_auth)
    r = client.post("/chat", json={"message": "hi"})
    assert r.status_code == 200
    # Anonymous user is assigned viewer role, carried in state_delta.
    delta = patched_runner.last_state_delta
    assert delta[AUTH_STATE_KEY]["role"] == "viewer"
    assert delta[AUTH_STATE_KEY]["subject"].startswith("anonymous:")


# ── ServerConfig ─────────────────────────────────────────────────────


def test_server_config_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    monkeypatch.setenv("JWT_ALGORITHM", "HS256")
    monkeypatch.setenv("JWT_SECRET", "x")
    monkeypatch.setenv("ORRERY_CORS_ORIGINS", "https://a.example, https://b.example")
    cfg = ServerConfig.from_env()
    assert cfg.auth_enabled is False
    assert cfg.cors_origins == ("https://a.example", "https://b.example")


def test_auth_enabled_with_missing_secret_fails_fast(patched_runner):
    """create_app must fail at startup when auth_enabled=True but JWT is misconfigured."""
    from orrery_core.security.auth import AuthError

    config = ServerConfig(auth_enabled=True, jwt=JWTConfig(algorithm="HS256", secret=None))
    with pytest.raises(AuthError, match="JWT_SECRET"):
        create_app(
            root_agent=MagicMock(),
            app_name="test",
            plugins=[],
            config=config,
        )


# ── /chat validation ────────────────────────────────────────────────


def test_chat_empty_message_rejected(app_no_auth):
    client = TestClient(app_no_auth)
    r = client.post("/chat", json={"message": ""})
    assert r.status_code == 422


# ── Web console (AEP-019) ───────────────────────────────────────────


def _write_bundle(tmp_path):
    """Create a minimal built-bundle layout the mount will accept."""
    (tmp_path / "index.html").write_text("<!doctype html><title>Console</title>", encoding="utf-8")
    return tmp_path


def test_console_not_mounted_when_disabled(patched_runner):
    app = create_app(
        root_agent=MagicMock(),
        app_name="test",
        plugins=[],
        config=ServerConfig(auth_enabled=False, jwt=JWTConfig(secret="x")),
    )
    assert not any(getattr(r, "name", None) == "console" for r in app.routes)


def test_console_mounts_and_serves_index_when_bundle_present(patched_runner, tmp_path):
    with patch("orrery_core.serving.server._static_dir", return_value=_write_bundle(tmp_path)):
        app = create_app(
            root_agent=MagicMock(),
            app_name="test",
            plugins=[],
            config=ServerConfig(
                auth_enabled=False, jwt=JWTConfig(secret="x"), web_console_enabled=True
            ),
        )
    client = TestClient(app)
    r = client.get("/")
    assert r.status_code == 200
    assert "Console" in r.text


def test_console_enabled_but_missing_bundle_skips_without_crashing(patched_runner, tmp_path):
    # Point at an empty dir (no index.html): warn-and-skip, API still serves.
    with patch("orrery_core.serving.server._static_dir", return_value=tmp_path):
        app = create_app(
            root_agent=MagicMock(),
            app_name="test",
            plugins=[],
            config=ServerConfig(
                auth_enabled=False, jwt=JWTConfig(secret="x"), web_console_enabled=True
            ),
        )
    assert not any(getattr(r, "name", None) == "console" for r in app.routes)
    assert TestClient(app).get("/healthz").status_code == 200


def test_console_mount_does_not_shadow_api_routes(patched_runner, tmp_path):
    """The static catch-all at / must not intercept explicit API routes."""
    with patch("orrery_core.serving.server._static_dir", return_value=_write_bundle(tmp_path)):
        app = create_app(
            root_agent=MagicMock(),
            app_name="test",
            plugins=[],
            config=ServerConfig(
                auth_enabled=False, jwt=JWTConfig(secret="x"), web_console_enabled=True
            ),
        )
    client = TestClient(app)
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.post("/chat", json={"message": "hi"}).status_code == 200


def test_web_console_enabled_from_env(monkeypatch):
    monkeypatch.setenv("ORRERY_WEB_CONSOLE_ENABLED", "true")
    assert ServerConfig.from_env().web_console_enabled is True
    monkeypatch.setenv("ORRERY_WEB_CONSOLE_ENABLED", "false")
    assert ServerConfig.from_env().web_console_enabled is False


# ── GET /session/{id}/activity (AEP-019) ────────────────────────────


def _alice_token() -> str:
    return _hs256(
        {"sub": "alice", "roles": ["operator"], "exp": int(time.time()) + 600, "aud": "orrery"}
    )


def test_activity_requires_auth(app_with_auth):
    client = TestClient(app_with_auth)
    assert client.get("/session/sess-1/activity").status_code == 401


def test_activity_unknown_session_is_404(app_with_auth, patched_runner):
    patched_runner.get_session = AsyncMock(return_value=None)
    client = TestClient(app_with_auth)
    r = client.get("/session/nope/activity", headers={"Authorization": f"Bearer {_alice_token()}"})
    assert r.status_code == 404


def test_activity_lookup_is_pinned_to_the_verified_subject(app_with_auth, patched_runner):
    """The user_id in the session lookup comes from the JWT, not the request."""
    patched_runner.get_session = AsyncMock(return_value=None)
    client = TestClient(app_with_auth)
    client.get("/session/sess-1/activity", headers={"Authorization": f"Bearer {_alice_token()}"})
    assert patched_runner.get_session.call_args.kwargs["user_id"] == "alice"


def test_activity_returns_session_log_entries(app_with_auth, patched_runner, mock_session):
    mock_session.state = {
        "session_log": [
            {
                "operation": "check_cluster_health",
                "details": "[kafka] (no args) → success",
                "timestamp": "2026-07-19T00:00:00+00:00",
            },
            "not-a-dict-is-filtered",
        ]
    }
    patched_runner.get_session = AsyncMock(return_value=mock_session)
    client = TestClient(app_with_auth)
    r = client.get(
        "/session/sess-1/activity", headers={"Authorization": f"Bearer {_alice_token()}"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "sess-1"
    assert body["entries"] == [
        {
            "operation": "check_cluster_health",
            "details": "[kafka] (no args) → success",
            "timestamp": "2026-07-19T00:00:00+00:00",
        }
    ]


def test_activity_empty_log(app_with_auth, patched_runner, mock_session):
    mock_session.state = {}
    patched_runner.get_session = AsyncMock(return_value=mock_session)
    client = TestClient(app_with_auth)
    r = client.get(
        "/session/sess-1/activity", headers={"Authorization": f"Bearer {_alice_token()}"}
    )
    assert r.status_code == 200
    assert r.json()["entries"] == []


# ── GET /confirmations/pending (AEP-019) ────────────────────────────


@pytest.fixture
def pending_store():
    """Fresh in-memory confirmation store installed for the test."""
    from orrery_core.security.confirmation_store import ConfirmationStore
    from orrery_core.security.guardrails import _pending_confirmations

    store = ConfirmationStore()
    _pending_confirmations.configure(store)
    yield store
    _pending_confirmations.configure(None)


def test_pending_requires_auth(app_with_auth, pending_store):
    client = TestClient(app_with_auth)
    assert client.get("/confirmations/pending").status_code == 401


def test_pending_none(app_with_auth, pending_store):
    client = TestClient(app_with_auth)
    r = client.get("/confirmations/pending", headers={"Authorization": f"Bearer {_alice_token()}"})
    assert r.status_code == 200
    assert r.json() == {"pending": None}


def test_pending_returns_own_action_only(app_with_auth, pending_store):
    from orrery_core.security.confirmation_store import PendingConfirmation

    pending_store.add(
        PendingConfirmation(
            action_id="a1",
            tool_name="restart_deployment",
            requester="alice",
            scope_key="alice",
            level="destructive",
            args={"name": "payment-api", "namespace": "prod"},
        )
    )
    pending_store.add(
        PendingConfirmation(
            action_id="b1",
            tool_name="delete_topic",
            requester="bob",
            scope_key="bob",
            level="destructive",
            args={"topic": "orders"},
        )
    )
    client = TestClient(app_with_auth)
    r = client.get("/confirmations/pending", headers={"Authorization": f"Bearer {_alice_token()}"})
    assert r.status_code == 200
    pending = r.json()["pending"]
    assert pending["tool_name"] == "restart_deployment"
    assert pending["level"] == "destructive"
    assert pending["args"] == {"name": "payment-api", "namespace": "prod"}
