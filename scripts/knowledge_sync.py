#!/usr/bin/env python
"""Index the documentation corpus into the configured knowledge backend.

    make knowledge-sync                    # index docs/runbooks + docs/adr
    make knowledge-sync ROOT=docs          # index a different tree
    make knowledge-sync PRUNE=0            # keep documents no source produced

Run from CI on merge to ``main`` so the index tracks the repository, and on
cadence for external sources. Never called from the request path — see
``orrery_core.knowledge.sync`` for why.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from orrery_core import setup_logging
from orrery_core.knowledge import KnowledgeConfig, resolve_index, sync_sources
from orrery_core.knowledge.sources import FilesystemSource, GitSource

#: Indexed by default: the on-call runbooks AEP-017 produces, and the ADRs,
#: which are the closest thing the repo has to "why is it built this way".
DEFAULT_ROOTS = ("docs/runbooks", "docs/adr")

logger = logging.getLogger("orrery.knowledge.cli")


def _build_sources(repo_root: Path, roots: list[str], use_git: bool) -> list:
    sources = []
    for rel in roots:
        path = repo_root / rel
        if not path.is_dir():
            logger.warning("skipping missing knowledge root", extra={"path": str(path)})
            continue
        name = Path(rel).name
        if use_git:
            sources.append(GitSource(repo_root, name=name, subdir=rel, labels={"collection": name}))
        else:
            sources.append(FilesystemSource(path, name=name, labels={"collection": name}))
    return sources


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        action="append",
        dest="roots",
        help=f"Repo-relative directory to index (repeatable). Default: {', '.join(DEFAULT_ROOTS)}",
    )
    parser.add_argument(
        "--no-git",
        action="store_true",
        help="Use filesystem mtimes as revisions instead of commit shas",
    )
    parser.add_argument(
        "--no-prune",
        action="store_true",
        help="Keep indexed documents that no source produced this run",
    )
    args = parser.parse_args()

    setup_logging()
    config = KnowledgeConfig()

    index = resolve_index(config)
    if index is None:
        backend = config.orrery_knowledge_backend
        if backend in ("none", "", None):
            print(
                "ORRERY_KNOWLEDGE_BACKEND is not set — nothing to sync.\n"
                "Set ORRERY_KNOWLEDGE_BACKEND=elasticsearch to enable retrieval.",
                file=sys.stderr,
            )
            return 0
        # A configured backend with no write side is a managed vendor that
        # ingests through its own connectors. Not an error.
        print(f"Backend {backend!r} manages its own ingestion — nothing to sync.")
        return 0

    repo_root = Path(__file__).resolve().parent.parent
    sources = _build_sources(repo_root, args.roots or list(DEFAULT_ROOTS), not args.no_git)
    if not sources:
        print("No knowledge sources found — nothing to sync.", file=sys.stderr)
        return 0

    report = await sync_sources(sources, index, config=config, prune_deleted=not args.no_prune)
    print(report.summary())
    for error in report.errors:
        print(f"  error: {error}", file=sys.stderr)
    # A partial index is a correctness problem, not a warning: CI must not go
    # green having quietly dropped half the runbooks.
    return 1 if report.errors else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
