"""Authentication layer for HTTP entry points.

Provides JWT verification (HS256 + RS256/JWKS), a claim-to-role mapper, and
an ``AuthPlugin`` that reads a verified ``AuthContext`` from session state
and applies it via :func:`set_user_role` — completing the chain that makes
RBAC trustworthy.

The verification step is intentionally framework-agnostic: it returns an
``AuthContext`` (or raises ``AuthError``). HTTP wiring lives in
:mod:`orrery_core.server`; transports like Slack and Google Chat already
authenticate at their own layer and only need ``set_user_role`` directly.

Token verification depends on PyJWT (``pyjwt[crypto]``). It is installed
via the ``orrery-core[auth]`` extra and imported lazily so callers that
only need the plugin (e.g. transports that set ``_auth`` themselves) do
not need PyJWT on the path.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from google.adk.agents.base_agent import BaseAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.plugins.base_plugin import BasePlugin

from .rbac import set_user_role

logger = logging.getLogger("orrery.auth")

# Session-state key under which the verified payload is stashed by the
# HTTP layer before the agent is invoked. AuthPlugin reads from here.
AUTH_STATE_KEY = "_auth"

# Default JWT claim that carries roles. Override via the ``role_claim``
# argument on ``extract_role``.
DEFAULT_ROLE_CLAIM = "roles"

_ADMIN_ROLE_NAMES = frozenset({"admin", "orrery-admin", "orrery_admin"})
_OPERATOR_ROLE_NAMES = frozenset({"operator", "orrery-operator", "orrery_operator"})


# ── Errors / data ────────────────────────────────────────────────────


class AuthError(Exception):
    """Raised when token verification fails."""


@dataclass(frozen=True)
class AuthContext:
    """Result of a successful token verification.

    Stored in session state under :data:`AUTH_STATE_KEY` so the
    ``AuthPlugin`` can apply the verified role on the agent's first
    invocation.
    """

    subject: str
    role: str
    claims: dict[str, Any] = field(default_factory=dict)

    def as_state(self) -> dict[str, Any]:
        return {"subject": self.subject, "role": self.role, "claims": dict(self.claims)}


# ── Role mapping ─────────────────────────────────────────────────────


def _lookup_claim(claims: dict[str, Any], path: str) -> Any:
    """Read ``path`` from *claims*, following ``.`` into nested objects.

    A flat name is looked up directly, so existing configurations are
    unaffected. Dotted paths exist because the providers people actually deploy
    nest their roles: Keycloak puts realm roles under ``realm_access.roles`` and
    client roles under ``resource_access.<client>.roles``, neither of which a
    flat lookup can reach. A path that doesn't resolve returns ``None``, which
    ``extract_role`` treats as "no roles" — i.e. ``viewer``, failing closed.
    """
    if "." not in path:
        return claims.get(path)

    current: Any = claims
    for segment in path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(segment)
        if current is None:
            return None
    return current


def extract_role(
    claims: dict[str, Any],
    *,
    role_claim: str = DEFAULT_ROLE_CLAIM,
    admin_values: Iterable[str] = _ADMIN_ROLE_NAMES,
    operator_values: Iterable[str] = _OPERATOR_ROLE_NAMES,
) -> str:
    """Map JWT claims to a viewer/operator/admin role.

    The role claim is read from ``claims[role_claim]``, where ``role_claim`` may
    be a dotted path into nested claims (``realm_access.roles``). The value may
    be either a list of strings or a single space/comma-separated string. The
    first match against ``admin_values`` returns ``"admin"``; otherwise the
    first match against ``operator_values`` returns ``"operator"``; otherwise
    ``"viewer"``.

    Examples::

        extract_role({"roles": ["admin"]}) == "admin"
        extract_role({"roles": "operator,foo"}) == "operator"
        extract_role({"roles": "viewer"}) == "viewer"
        extract_role({}) == "viewer"

        # Keycloak's default shape:
        extract_role(
            {"realm_access": {"roles": ["admin"]}}, role_claim="realm_access.roles"
        ) == "admin"
    """
    raw = _lookup_claim(claims, role_claim)
    if raw is None:
        return "viewer"

    if isinstance(raw, str):
        # Tolerate both "admin operator" and "admin,operator".
        tokens = [t.strip().lower() for t in raw.replace(",", " ").split() if t.strip()]
    elif isinstance(raw, (list, tuple, set, frozenset)):
        tokens = [str(t).strip().lower() for t in raw if str(t).strip()]
    else:
        logger.warning("Unsupported type for %r claim: %s", role_claim, type(raw).__name__)
        return "viewer"

    admin_set = {v.lower() for v in admin_values}
    operator_set = {v.lower() for v in operator_values}

    if any(t in admin_set for t in tokens):
        return "admin"
    if any(t in operator_set for t in tokens):
        return "operator"
    return "viewer"


# ── Token verification ──────────────────────────────────────────────


@dataclass(frozen=True)
class JWTConfig:
    """JWT verification configuration.

    HS256 path: provide ``secret``.
    RS256/JWKS path: provide ``jwks_url`` (a JWKS endpoint).

    ``audience`` and ``issuer`` are optional but strongly recommended in
    production — they bind the token to this service and its trusted
    identity provider.
    """

    algorithm: str = "HS256"
    secret: str | None = None
    jwks_url: str | None = None
    audience: str | None = None
    issuer: str | None = None
    role_claim: str = DEFAULT_ROLE_CLAIM
    leeway_seconds: int = 30

    @classmethod
    def from_env(cls) -> JWTConfig:
        """Load configuration from ``JWT_*`` environment variables."""
        return cls(
            algorithm=os.getenv("JWT_ALGORITHM", "HS256").upper(),
            secret=os.getenv("JWT_SECRET") or None,
            jwks_url=os.getenv("JWT_JWKS_URL") or None,
            audience=os.getenv("JWT_AUDIENCE") or None,
            issuer=os.getenv("JWT_ISSUER") or None,
            role_claim=os.getenv("JWT_ROLE_CLAIM", DEFAULT_ROLE_CLAIM),
            leeway_seconds=int(os.getenv("JWT_LEEWAY_SECONDS", "30")),
        )

    def validate(self) -> None:
        """Raise ``AuthError`` if the configuration is unusable."""
        if self.algorithm == "HS256":
            if not self.secret:
                raise AuthError("JWT_SECRET is required when JWT_ALGORITHM=HS256")
        elif self.algorithm in ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512"):
            if not self.jwks_url:
                raise AuthError(f"JWT_JWKS_URL is required when JWT_ALGORITHM={self.algorithm}")
        else:
            raise AuthError(f"Unsupported JWT_ALGORITHM: {self.algorithm}")


# Module-level JWKS client cache. PyJWT's PyJWKClient holds an LRU of
# keys and benefits from being long-lived across requests.
_jwks_clients: dict[str, Any] = {}


def _get_jwks_client(jwks_url: str) -> Any:
    """Return a cached ``PyJWKClient`` for the given URL."""
    client = _jwks_clients.get(jwks_url)
    if client is not None:
        return client

    try:
        import jwt as _jwt
    except ImportError as exc:
        raise AuthError("PyJWT is not installed. Install with: uv sync --extra auth") from exc

    client = _jwt.PyJWKClient(jwks_url, cache_keys=True, lifespan=600)
    _jwks_clients[jwks_url] = client
    return client


def verify_token(token: str, config: JWTConfig) -> AuthContext:
    """Verify a JWT bearer token and return an ``AuthContext``.

    Raises :class:`AuthError` on any verification failure: bad signature,
    expired token, wrong audience/issuer, unknown algorithm, or missing
    key material.
    """
    if not token:
        raise AuthError("Empty bearer token")

    config.validate()

    try:
        import jwt as _jwt
        from jwt import InvalidTokenError
        from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
    except ImportError as exc:
        raise AuthError("PyJWT is not installed. Install with: uv sync --extra auth") from exc

    decode_kwargs: dict[str, Any] = {
        "algorithms": [config.algorithm],
        "leeway": config.leeway_seconds,
        "options": {"require": ["exp"]},
    }
    if config.audience:
        decode_kwargs["audience"] = config.audience
    if config.issuer:
        decode_kwargs["issuer"] = config.issuer

    try:
        if config.algorithm == "HS256":
            # config.validate() guarantees secret is set when algorithm is HS256.
            assert config.secret is not None  # noqa: S101 — invariant
            payload = _jwt.decode(token, config.secret, **decode_kwargs)
        else:
            # config.validate() guarantees jwks_url is set for asymmetric algorithms.
            assert config.jwks_url is not None  # noqa: S101 — invariant
            client = _get_jwks_client(config.jwks_url)
            try:
                signing_key = client.get_signing_key_from_jwt(token).key
            except PyJWKClientConnectionError:
                # The IdP is unreachable — not the caller's fault, and not
                # something a new token would fix. Let it surface as a server
                # error rather than telling the user their token is bad.
                raise
            except PyJWKClientError as exc:
                # No key matches the token's `kid` (or it has none). PyJWT does
                # NOT make this an InvalidTokenError, so without this it escapes
                # as a 500 with a traceback — meaning any forged or garbage
                # token became a server error instead of a clean 401.
                raise AuthError("Invalid or expired token") from exc
            payload = _jwt.decode(token, signing_key, **decode_kwargs)
    except InvalidTokenError as exc:
        # Never surface the raw exception message in user-facing responses —
        # PyJWT messages can leak validation strategy. Log it, return generic.
        logger.warning("JWT verification failed: %s", exc)
        raise AuthError("Invalid or expired token") from exc

    subject = payload.get("sub")
    if not subject:
        raise AuthError("Token missing 'sub' claim")

    role = extract_role(payload, role_claim=config.role_claim)
    return AuthContext(subject=str(subject), role=role, claims=payload)


# ── Plugin ───────────────────────────────────────────────────────────


class AuthPlugin(BasePlugin):
    """Applies a pre-verified ``AuthContext`` from session state to RBAC.

    Expects the HTTP front door (or any trusted transport) to have already
    verified the token and stored the payload at ``state[AUTH_STATE_KEY]``
    before the runner is invoked.

    With ``require_auth=True`` (default), sessions missing the ``_auth``
    payload have their role forced to ``viewer`` and a warning is logged.
    This ensures privilege escalation is impossible even if a future
    transport forgets to wire the auth dependency.
    """

    def __init__(self, *, require_auth: bool = True) -> None:
        super().__init__(name="auth")
        self._require_auth = require_auth

    async def before_agent_callback(
        self, *, agent: BaseAgent, callback_context: CallbackContext
    ) -> None:
        # callback_context.state is an ADK State (dict-like). set_user_role
        # only does dict mutations, so the runtime contract is satisfied —
        # we ignore the strict static type here, same pattern used elsewhere
        # in the codebase for State writes.
        state: Any = callback_context.state
        auth = state.get(AUTH_STATE_KEY)

        if auth is None:
            if self._require_auth:
                logger.warning(
                    "AuthPlugin: no %s payload on session — forcing viewer role",
                    AUTH_STATE_KEY,
                )
                set_user_role(state, "viewer")
            return None

        role = auth.get("role") if isinstance(auth, dict) else None
        if not isinstance(role, str):
            logger.warning(
                "AuthPlugin: malformed %s payload (role=%r) — forcing viewer",
                AUTH_STATE_KEY,
                role,
            )
            set_user_role(state, "viewer")
            return None

        set_user_role(state, role)
        return None
