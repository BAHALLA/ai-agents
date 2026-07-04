"""Pluggable secrets manager.

Default behaviour reads from environment variables — same as the rest of
the project. The :class:`SecretsBackend` protocol lets deployments swap in
Vault, GCP Secret Manager, AWS Secrets Manager, or any other store
without touching call sites.

Resolution order, in priority:

1. Explicit backend installed via :func:`register_backend` (e.g. Vault).
2. ``SECRETS_FILE`` / mounted-secret directory (kubernetes-style files).
3. Environment variables.
4. Configured default (or ``None``).

The reason for centralising this isn't abstraction for its own sake — it
gives a single seam to swap in a real secrets store without grepping the
codebase for ``os.getenv`` calls each release.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Protocol, runtime_checkable

logger = logging.getLogger("orrery.secrets")


@runtime_checkable
class SecretsBackend(Protocol):
    """Read-only secrets backend.

    Implementations must be cheap to call repeatedly — caching is the
    backend's responsibility, not the caller's.
    """

    def get(self, key: str) -> str | None:
        """Return the secret value for *key*, or ``None`` if not found.

        Implementations must never raise on missing keys; raise only on
        backend failures (network down, auth denied) that the caller
        should treat as fatal.
        """


# ── Mounted-file backend ────────────────────────────────────────────


class FileBackend:
    """Reads secrets from a directory of mounted files.

    This is the conventional pattern for Kubernetes ``Secret`` volumes:
    each key in the Secret becomes a file named after the key, with the
    value as the file body.

    Example: ``FileBackend("/var/run/secrets/orrery")`` looks up
    ``JWT_SECRET`` by reading ``/var/run/secrets/orrery/JWT_SECRET``.
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)

    def get(self, key: str) -> str | None:
        path = self._dir / key
        if not path.is_file():
            return None
        try:
            return path.read_text(encoding="utf-8").rstrip("\n")
        except OSError as exc:
            logger.warning("FileBackend: failed to read %s: %s", path, exc)
            return None


# ── Manager ─────────────────────────────────────────────────────────


class SecretsManager:
    """Resolves secrets in a fixed priority order.

    Each :meth:`get` call checks each backend in registration order; the
    first non-``None`` value wins. Environment variables are always the
    final fallback so existing deployments continue to work unchanged.
    """

    _DEFAULT_SECRETS_DIR_ENV = "ORRERY_SECRETS_DIR"

    def __init__(self, backends: list[SecretsBackend] | None = None) -> None:
        self._backends: list[SecretsBackend] = list(backends or [])

        # Auto-register a FileBackend if ORRERY_SECRETS_DIR is set. This
        # is the lightest-weight integration with Kubernetes Secrets — no
        # code change required, just mount the volume and set the env var.
        if (secrets_dir := os.getenv(self._DEFAULT_SECRETS_DIR_ENV)) and Path(secrets_dir).is_dir():
            self._backends.append(FileBackend(secrets_dir))

    def register_backend(self, backend: SecretsBackend) -> None:
        """Install a backend at the front of the resolution chain.

        Called once at startup before any :meth:`get` invocations.
        """
        self._backends.insert(0, backend)

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the secret value for *key*, or *default* if not found."""
        for backend in self._backends:
            try:
                value = backend.get(key)
            except Exception as exc:
                # A misbehaving backend must not take down the process —
                # log and fall through to the next backend.
                logger.warning(
                    "Secrets backend %s raised for key %r: %s",
                    type(backend).__name__,
                    key,
                    exc,
                )
                continue
            if value is not None:
                return value

        env_value = os.getenv(key)
        if env_value is not None and env_value != "":
            return env_value
        return default

    def require(self, key: str) -> str:
        """Return the secret value for *key*, raising if absent.

        Use at startup for secrets that have no safe default — failing
        fast is better than discovering the gap on the first request.
        """
        value = self.get(key)
        if value is None or value == "":
            raise KeyError(f"Required secret {key!r} is not set")
        return value


# Module-level default instance. Most callers should use this directly.
default_secrets = SecretsManager()
