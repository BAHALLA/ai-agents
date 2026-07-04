"""Tests for orrery_core.server (FastAPI HTTP front door)."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import jwt as pyjwt
import pytest

# Import-time skip if FastAPI extras aren't installed.
pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from orrery_core.auth import AUTH_STATE_KEY, JWTConfig  # noqa: E402
from orrery_core.server import ServerConfig, create_app  # noqa: E402

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
        patch("orrery_core.gateway.Runner", return_value=runner),
        patch("orrery_core.gateway.App", return_value=MagicMock()),
        patch("orrery_core.server.create_session_service", return_value=session_service),
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
        patch("orrery_core.gateway.Runner", return_value=runner),
        patch("orrery_core.gateway.App", return_value=MagicMock()),
        patch("orrery_core.server.create_session_service", return_value=session_service),
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
    from orrery_core.auth import AuthError

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
