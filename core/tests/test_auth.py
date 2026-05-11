"""Tests for orrery_core.auth."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from orrery_core.auth import (
    AUTH_STATE_KEY,
    AuthContext,
    AuthError,
    AuthPlugin,
    JWTConfig,
    extract_role,
    verify_token,
)
from orrery_core.rbac import USER_ROLE_STATE_KEY

# ── extract_role ─────────────────────────────────────────────────────


class TestExtractRole:
    def test_no_role_claim_defaults_to_viewer(self):
        assert extract_role({}) == "viewer"
        assert extract_role({"sub": "alice"}) == "viewer"

    def test_admin_list_claim(self):
        assert extract_role({"roles": ["admin"]}) == "admin"

    def test_admin_string_claim(self):
        assert extract_role({"roles": "admin"}) == "admin"

    def test_admin_wins_over_operator(self):
        assert extract_role({"roles": ["operator", "admin"]}) == "admin"
        assert extract_role({"roles": "operator,admin"}) == "admin"

    def test_operator_when_admin_absent(self):
        assert extract_role({"roles": ["operator", "viewer"]}) == "operator"

    def test_unknown_roles_default_to_viewer(self):
        assert extract_role({"roles": ["guest", "anon"]}) == "viewer"

    def test_namespace_aliases_accepted(self):
        assert extract_role({"roles": ["orrery-admin"]}) == "admin"
        assert extract_role({"roles": ["orrery_operator"]}) == "operator"

    def test_custom_role_claim(self):
        assert extract_role({"groups": ["admin"]}, role_claim="groups") == "admin"

    def test_case_insensitive(self):
        assert extract_role({"roles": ["ADMIN"]}) == "admin"
        assert extract_role({"roles": "Operator"}) == "operator"

    def test_unsupported_type_logs_and_returns_viewer(self, caplog):
        assert extract_role({"roles": 123}) == "viewer"
        assert "Unsupported type" in caplog.text

    def test_custom_admin_values(self):
        assert (
            extract_role({"roles": ["sre"]}, admin_values={"sre"}, operator_values=set()) == "admin"
        )


# ── JWTConfig ────────────────────────────────────────────────────────


class TestJWTConfig:
    def test_validate_hs256_requires_secret(self):
        cfg = JWTConfig(algorithm="HS256", secret=None)
        with pytest.raises(AuthError, match="JWT_SECRET is required"):
            cfg.validate()

    def test_validate_hs256_with_secret_ok(self):
        JWTConfig(algorithm="HS256", secret="x").validate()

    def test_validate_rs256_requires_jwks(self):
        cfg = JWTConfig(algorithm="RS256", jwks_url=None)
        with pytest.raises(AuthError, match="JWT_JWKS_URL is required"):
            cfg.validate()

    def test_validate_rejects_unknown_algorithm(self):
        cfg = JWTConfig(algorithm="MD5", secret="x")
        with pytest.raises(AuthError, match="Unsupported JWT_ALGORITHM"):
            cfg.validate()

    def test_from_env(self, monkeypatch):
        monkeypatch.setenv("JWT_ALGORITHM", "rs256")
        monkeypatch.setenv("JWT_JWKS_URL", "https://idp/jwks")
        monkeypatch.setenv("JWT_AUDIENCE", "orrery")
        monkeypatch.setenv("JWT_ISSUER", "https://idp")
        monkeypatch.setenv("JWT_LEEWAY_SECONDS", "60")
        cfg = JWTConfig.from_env()
        assert cfg.algorithm == "RS256"
        assert cfg.jwks_url == "https://idp/jwks"
        assert cfg.audience == "orrery"
        assert cfg.issuer == "https://idp"
        assert cfg.leeway_seconds == 60


# ── verify_token (HS256) ─────────────────────────────────────────────


_TEST_SECRET = "x" * 64  # 32+ bytes to satisfy PyJWT HMAC length recommendation


def _hs256_token(claims: dict, secret: str = _TEST_SECRET) -> str:
    return pyjwt.encode(claims, secret, algorithm="HS256")


class TestVerifyTokenHS256:
    def test_round_trip(self):
        claims = {
            "sub": "alice",
            "roles": ["operator"],
            "exp": int(time.time()) + 600,
            "aud": "orrery",
            "iss": "https://idp",
        }
        token = _hs256_token(claims)
        ctx = verify_token(
            token,
            JWTConfig(
                algorithm="HS256",
                secret=_TEST_SECRET,
                audience="orrery",
                issuer="https://idp",
            ),
        )
        assert isinstance(ctx, AuthContext)
        assert ctx.subject == "alice"
        assert ctx.role == "operator"
        assert ctx.claims["sub"] == "alice"

    def test_empty_token_rejected(self):
        with pytest.raises(AuthError, match="Empty bearer"):
            verify_token("", JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_bad_signature_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) + 60}, secret="y" * 64)
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_expired_token_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) - 600})
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_missing_sub_rejected(self):
        token = _hs256_token({"exp": int(time.time()) + 60})
        with pytest.raises(AuthError, match="'sub' claim"):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_wrong_audience_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) + 60, "aud": "other"})
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(
                token,
                JWTConfig(algorithm="HS256", secret=_TEST_SECRET, audience="orrery"),
            )

    def test_wrong_issuer_rejected(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) + 60, "iss": "https://other"})
        with pytest.raises(AuthError, match="Invalid or expired"):
            verify_token(
                token,
                JWTConfig(algorithm="HS256", secret=_TEST_SECRET, issuer="https://idp"),
            )

    def test_missing_exp_rejected(self):
        # Even with all other claims, missing exp must fail (no immortal tokens).
        token = _hs256_token({"sub": "a"})
        with pytest.raises(AuthError):
            verify_token(token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET))

    def test_leeway_allows_recently_expired(self):
        token = _hs256_token({"sub": "a", "exp": int(time.time()) - 10})
        ctx = verify_token(
            token, JWTConfig(algorithm="HS256", secret=_TEST_SECRET, leeway_seconds=30)
        )
        assert ctx.subject == "a"


# ── verify_token (RS256/JWKS) ────────────────────────────────────────


@pytest.fixture
def rsa_keypair():
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return private_pem, public_pem


class TestVerifyTokenRS256:
    def test_round_trip_via_mocked_jwks(self, rsa_keypair, monkeypatch):
        private_pem, public_pem = rsa_keypair
        token = pyjwt.encode(
            {
                "sub": "bob",
                "roles": ["admin"],
                "exp": int(time.time()) + 600,
            },
            private_pem,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )

        # Clear the module-level JWKS client cache so this test is isolated.
        from orrery_core import auth

        auth._jwks_clients.clear()

        with patch.object(auth, "_get_jwks_client") as get_client:
            fake_signing_key = MagicMock()
            fake_signing_key.key = public_pem
            client = MagicMock()
            client.get_signing_key_from_jwt.return_value = fake_signing_key
            get_client.return_value = client

            ctx = verify_token(
                token,
                JWTConfig(algorithm="RS256", jwks_url="https://idp/jwks"),
            )

        assert ctx.subject == "bob"
        assert ctx.role == "admin"

    def test_jwks_url_required_for_rs256(self):
        with pytest.raises(AuthError, match="JWT_JWKS_URL"):
            verify_token("ignored", JWTConfig(algorithm="RS256"))


# ── AuthPlugin ──────────────────────────────────────────────────────


def _make_callback_ctx(state: dict) -> MagicMock:
    ctx = MagicMock()
    ctx.state = state
    return ctx


@pytest.mark.asyncio
class TestAuthPlugin:
    async def test_applies_role_from_auth_state(self):
        plugin = AuthPlugin()
        state: dict = {AUTH_STATE_KEY: {"subject": "alice", "role": "admin", "claims": {}}}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "admin"

    async def test_missing_auth_forces_viewer_when_required(self, caplog):
        plugin = AuthPlugin(require_auth=True)
        state: dict = {}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "viewer"
        assert "no _auth payload" in caplog.text

    async def test_missing_auth_does_nothing_when_optional(self):
        plugin = AuthPlugin(require_auth=False)
        state: dict = {}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert USER_ROLE_STATE_KEY not in state

    async def test_malformed_auth_payload_forces_viewer(self, caplog):
        plugin = AuthPlugin()
        state: dict = {AUTH_STATE_KEY: {"subject": "alice"}}  # no role
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "viewer"
        assert "malformed" in caplog.text

    async def test_invalid_role_value_falls_back_to_viewer(self):
        # set_user_role normalises unknown role names to "viewer".
        plugin = AuthPlugin()
        state: dict = {AUTH_STATE_KEY: {"subject": "alice", "role": "superuser"}}
        ctx = _make_callback_ctx(state)

        await plugin.before_agent_callback(agent=MagicMock(), callback_context=ctx)

        assert state[USER_ROLE_STATE_KEY] == "viewer"


# ── AuthContext ─────────────────────────────────────────────────────


def test_auth_context_as_state():
    ctx = AuthContext(subject="alice", role="admin", claims={"sub": "alice", "exp": 1})
    state = ctx.as_state()
    assert state == {"subject": "alice", "role": "admin", "claims": {"sub": "alice", "exp": 1}}
    # as_state returns a copy of the claims, not a reference
    state["claims"]["sub"] = "mallory"
    assert ctx.claims["sub"] == "alice"
