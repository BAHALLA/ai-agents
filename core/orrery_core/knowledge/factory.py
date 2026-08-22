"""Build the configured knowledge backend and tool from the environment.

Mirrors ``resolve_model()``: one env var picks a backend, and no agent knows
which one it got. The failure policy is deliberately split in two, because the
two failures deserve different answers:

- **Misconfiguration fails fast at startup.** An unknown backend name means a
  typo in a deployment manifest, and a pod that comes up "healthy" while
  silently serving no corpus is worse than one that refuses to start. Same
  posture as ``create_session_service()``.
- **An unreachable backend at query time does not.** The search cluster being
  down should degrade retrieval to a tool error the agent can report and work
  around, not take down every other capability. Knowledge is an augmentation;
  the platform diagnosed incidents without it before this existed.
"""

from __future__ import annotations

import logging

from .config import KNOWLEDGE_BACKENDS, KnowledgeConfig
from .protocols import KnowledgeIndex, KnowledgeRetriever
from .tool import KnowledgeSearchTool

logger = logging.getLogger("orrery.knowledge")


class KnowledgeConfigError(ValueError):
    """The knowledge layer is misconfigured — raised at startup, not at query time."""


def _backend(config: KnowledgeConfig) -> object | None:
    name = (config.orrery_knowledge_backend or "none").strip().lower()
    if name in ("", "none", "off", "disabled"):
        return None
    if name == "elasticsearch":
        try:
            from .backends.elasticsearch import ElasticsearchKnowledgeBackend
        except ImportError as exc:  # pragma: no cover — depends on install shape
            # The backend module is imported lazily so an unconfigured
            # deployment never needs its dependency. When it *is* configured,
            # say which extra is missing rather than surfacing a bare
            # ModuleNotFoundError from three frames down.
            raise KnowledgeConfigError(
                "ORRERY_KNOWLEDGE_BACKEND=elasticsearch requires the 'knowledge' "
                "extra: install orrery-core[knowledge]."
            ) from exc

        return ElasticsearchKnowledgeBackend(config)
    raise KnowledgeConfigError(
        f"Unknown ORRERY_KNOWLEDGE_BACKEND={name!r}. Supported: {', '.join(KNOWLEDGE_BACKENDS)}."
    )


def resolve_retriever(config: KnowledgeConfig | None = None) -> KnowledgeRetriever | None:
    """Return the configured retriever, or ``None`` when no corpus is configured."""
    backend = _backend(config or KnowledgeConfig())
    if backend is None:
        return None
    if not isinstance(backend, KnowledgeRetriever):
        raise KnowledgeConfigError(
            f"{type(backend).__name__} does not implement KnowledgeRetriever"
        )
    return backend


def resolve_index(config: KnowledgeConfig | None = None) -> KnowledgeIndex | None:
    """Return the write side, or ``None`` when there isn't one.

    ``None`` is a normal answer, not an error: a managed backend ingests
    through its vendor's own connectors, so ``knowledge-sync`` skips it rather
    than failing.
    """
    backend = _backend(config or KnowledgeConfig())
    if backend is None:
        return None
    return backend if isinstance(backend, KnowledgeIndex) else None


def knowledge_tool(config: KnowledgeConfig | None = None) -> KnowledgeSearchTool | None:
    """Build ``search_knowledge``, or ``None`` when no corpus is configured.

    Agents attach the result conditionally. An agent that advertises a search
    tool with nothing behind it teaches the model to call it and get nothing,
    which is worse than not having the tool at all.
    """
    cfg = config or KnowledgeConfig()
    retriever = resolve_retriever(cfg)
    if retriever is None:
        return None
    logger.info(
        "knowledge retrieval enabled",
        extra={"backend": cfg.orrery_knowledge_backend, "top_k": cfg.orrery_knowledge_top_k},
    )
    return KnowledgeSearchTool(retriever, top_k=cfg.orrery_knowledge_top_k)
