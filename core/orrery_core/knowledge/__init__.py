"""Knowledge retrieval: pluggable sources and backends (AEP-025).

Two seams rather than one "RAG provider", because a managed vendor owns both
halves of the problem and a self-hosted store owns neither. See
:mod:`orrery_core.knowledge.protocols` for the reasoning.

Backend implementations are imported lazily by
:func:`~orrery_core.knowledge.factory.resolve_retriever`, so the optional
dependency of an unconfigured backend is never a hard requirement.
"""

from .chunking import chunk_document as chunk_document
from .config import KNOWLEDGE_BACKENDS as KNOWLEDGE_BACKENDS
from .config import KnowledgeConfig as KnowledgeConfig
from .factory import KnowledgeConfigError as KnowledgeConfigError
from .factory import knowledge_tool as knowledge_tool
from .factory import resolve_index as resolve_index
from .factory import resolve_retriever as resolve_retriever
from .models import Chunk as Chunk
from .models import Document as Document
from .models import Passage as Passage
from .protocols import KnowledgeIndex as KnowledgeIndex
from .protocols import KnowledgeRetriever as KnowledgeRetriever
from .protocols import KnowledgeSource as KnowledgeSource
from .sync import SyncReport as SyncReport
from .sync import sync_sources as sync_sources
from .tool import TOOL_NAME as TOOL_NAME
from .tool import KnowledgeSearchTool as KnowledgeSearchTool
