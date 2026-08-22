# AEP-025: Pluggable Knowledge Retrieval

| Field | Value |
|-------|-------|
| **Status** | <span class="badge badge--amber">proposed</span> |
| **Priority** | <span class="badge badge--amber">P1</span> |
| **Effort** | High (2-3 weeks, phased) |
| **Impact** | High |
| **Dependencies** | AEP-017 (runbooks — supplies the first corpus); AEP-013 (safety chain) — completed |

## Gap Analysis

### Current Implementation

The platform can read live infrastructure through roughly 70 tools, and it can
recall its own past sessions. It cannot read anything a human wrote.

- **No knowledge corpus exists.** `find -iname "*runbook*"` matches only
  AEP-017, which proposes writing them. There are no postmortems, no indexed
  ADRs, no operational documentation reachable by the agent.
- **Long-term memory is session recall, not knowledge.**
  `DatabaseMemoryService.search_memory` matches *any single word* of the query
  against tokenized event text, orders by recency and caps at 200 rows. On a
  DevOps corpus a query containing "pod" or "error" matches nearly everything,
  so the practical result is "the 200 most recent events sharing a common word."
  It also only ever contains what the agent itself said or did.
- **No retrieval abstraction.** Nothing in `core/orrery_core/` models a
  document, a chunk, a passage or a search backend.

Every incident therefore starts from zero institutional knowledge. The agent can
tell you the ISR count is shrinking; it cannot tell you that the team saw this
in March and the cause was a sidecar OOM.

### Why an abstraction rather than "just add pgvector"

Two forces pull in opposite directions:

1. **Sources are heterogeneous and will keep arriving.** Runbooks in the repo,
   postmortems in Confluence, ADRs in git, design docs in Drive, tickets in Jira.
   Each has its own auth, its own change feed, its own idea of a "revision."
2. **Retrieval backends are a live vendor market.** Vertex AI Search /
   Discovery Engine, Elasticsearch, Azure AI Search, OpenSearch, pgvector. Each
   deployment of Orrery will already have opinions — a GCP shop wants Vertex, an
   on-prem shop wants the Elasticsearch it already runs, a laptop wants neither.

Hard-wiring either axis repeats the mistake `resolve_model()` was written to
avoid. The platform's multi-provider LLM support is deliberate and load-bearing;
a GCP-only knowledge layer would quietly make the Ollama and Claude deployments
second-class in exactly the same way.

### The constraint that decides the design

**A managed vendor owns both halves.** Vertex AI Search ingests from its own
connectors *and* serves queries; pgvector does neither until you write both. So
the abstraction cannot be a single "RAG provider" interface — it must be two
seams, with backends free to implement one or both.

## Proposed Solution

Two independent protocols, an ADK-native tool at the read end, and a factory
mirroring `resolve_model()`.

```
                     ┌── ingestion seam ──┐        ┌── retrieval seam ──┐

 KnowledgeSource ──►  Document ──► chunk ──► KnowledgeIndex
   filesystem                                (write side, optional)
   git                                              │
   confluence                                       ▼
   …                                        KnowledgeRetriever ──► Passage[]
                                             pgvector                  │
 (managed vendors ingest via their own       elasticsearch             ▼
  connectors and skip the left seam)         discovery_engine    knowledge_search
                                                                   (BaseRetrievalTool)
                                                                        │
                                                          the full plugin chain
```

### Step 1: The data model

Three records in `core/orrery_core/knowledge/models.py`, deliberately small:

```python
@dataclass(frozen=True)
class Document:
    uri: str            # stable identity: "git://repo@path", "confluence://SPACE/12345"
    title: str
    text: str
    revision: str       # commit sha, Confluence version, file mtime+size
    updated_at: datetime
    labels: Mapping[str, str]   # space, repo, severity, system…

@dataclass(frozen=True)
class Passage:
    text: str
    uri: str
    title: str
    section: str | None
    revision: str
    updated_at: datetime
    score: float
```

`Passage` carries provenance as a **required** field, not an optional extra.
Without it an operator at 03:00 cannot distinguish a retrieved fact from a
hallucinated one, and that is the failure mode that makes teams stop trusting
retrieval entirely.

### Step 2: The two protocols

```python
class KnowledgeSource(Protocol):
    name: str
    def documents(self, since: str | None = None) -> AsyncIterator[Document]: ...

class KnowledgeRetriever(Protocol):
    async def retrieve(
        self, query: str, *, top_k: int, labels: Mapping[str, str] | None = None
    ) -> list[Passage]: ...

class KnowledgeIndex(Protocol):        # optional — self-hosted backends only
    async def upsert(self, chunks: Sequence[Chunk]) -> None: ...
    async def delete_by_uri(self, uri: str) -> None: ...
```

`since` is the incremental-sync token — a commit sha for git, a timestamp for
Confluence. A source that cannot do incremental sync returns everything and says
so; correctness never depends on it.

A backend advertising only `KnowledgeRetriever` is a managed vendor: ingestion
is somebody else's job and `make knowledge-sync` skips it.

### Step 3: The read end is a real tool — non-negotiable

`knowledge_search` subclasses ADK's `BaseRetrievalTool` (a `BaseTool` whose
declared signature is `{query: string}`) and delegates to the configured
retriever. Being a real tool is what earns the existing safety chain:

- `SafetyScreenPlugin` neutralizes injected spans in the result. **A Confluence
  page or a git-hosted runbook is attacker-reachable text** the moment anyone
  outside the on-call team can edit it — the same threat model as a pod
  annotation, and already handled on the tool path.
- `PIIRedactionPlugin` scrubs credentials. Postmortems contain pasted tokens.
- `ToolOutputCapPlugin` bounds a chatty retrieval.
- `AuditPlugin` records the query. Retrieval is a read, so no guardrail
  decorator and RBAC lands it at `viewer`.

> **Do not wire `VertexAiSearchTool`.** Despite the name it is *model built-in
> grounding*: `process_llm_request` appends a `types.Retrieval` to the LLM
> request config and the model performs retrieval server-side. There is no
> `after_tool_callback`, so it bypasses **all four** protections above and never
> appears in the audit log. Use `DiscoveryEngineSearchTool` (a real
> `FunctionTool`) or a thin adapter over the Discovery Engine API instead. This
> is the single sharpest edge in the whole design: the convenient import is the
> one that silently removes the safety chain.

### Step 4: `resolve_retriever()`, mirroring `resolve_model()`

```
ORRERY_KNOWLEDGE_BACKEND = none | pgvector | elasticsearch | discovery_engine
ORRERY_KNOWLEDGE_TOP_K   = 8
```

`none` is the default and the tool is simply not attached — a deployment with no
corpus should not advertise a search tool it cannot answer with. Unknown backend
raises at startup, matching the fail-fast posture of `create_session_service()`.

**Ship Elasticsearch first, not pgvector.** `make up` already starts an
Elasticsearch container and the platform already ships an Elasticsearch agent
with a working client — so the Elasticsearch retriever needs no new
infrastructure, no image change and no embedding provider (BM25 alone is a large
improvement over "any word matches"). pgvector requires swapping
`postgres:16-alpine` for `pgvector/pgvector:pg16` in compose *and* the Helm
chart, plus an embedding provider. Sequencing it second keeps the first
increment cheap and proves the seams before paying for infrastructure.

### Step 5: Embeddings get their own seam

Only backends that embed locally need this, and it repeats the provider lesson:

```python
def resolve_embedder() -> Embedder:   # EMBEDDING_PROVIDER / EMBEDDING_MODEL
```

with Gemini, OpenAI and Ollama implementations. A GCP-only embedder would
re-import the lock-in through the back door.

### Step 6: Sources and sync

Three connectors to start, each ~100 lines:

| Source | Identity | Revision | Auth |
|--------|----------|----------|------|
| `FilesystemSource` | `file://<path>` | mtime+size | none |
| `GitSource` | `git://<remote>@<path>` | commit sha | existing clone / token |
| `ConfluenceSource` | `confluence://<space>/<id>` | page version | API token via `security/secrets.py` |

`make knowledge-sync` walks every configured source and upserts. It runs from
CI on merge to `main` for repo-backed sources, and on cadence for external ones.
Indexing is a **build-time** action, never lazy at request time — a first query
that silently triggers a full Confluence crawl is an outage waiting for its
moment.

Chunking is heading-aware markdown splitting with a token budget and overlap;
`section` comes from the enclosing heading, which is what makes a citation
useful ("§ Recovery" beats "chunk 47").

### Step 7: ACLs — state the limit, don't fake it

Confluence spaces and Drive folders have per-user permissions. Orrery's
retrieval is `viewer`-level and **not** ACL-aware.

The rule is therefore explicit and fail-closed: **only index sources whose
contents every `viewer` may read.** Restricted spaces are excluded by
configuration, and `make knowledge-sync` refuses a source that cannot enumerate
its own visibility rather than indexing it optimistically. Per-principal
filtering is deferred to a later AEP if a deployment needs it; pretending to
enforce ACLs we do not enforce would be worse than not indexing.

### Step 8: Evaluation

The 33 existing scenarios score `tool_trajectory_avg_score` only, which is blind
to retrieval quality — a scenario can call `knowledge_search` in exactly the
right place and get garbage back and still pass. A small golden set of
question → expected-source-uri pairs, scored on whether the right document was
retrieved, is the acceptance gate for this AEP and the first use of a non-
trajectory criterion in the repo.

## Affected Files

| File | Change |
|------|--------|
| `core/orrery_core/knowledge/models.py` | New — `Document`, `Chunk`, `Passage` |
| `core/orrery_core/knowledge/protocols.py` | New — `KnowledgeSource`, `KnowledgeRetriever`, `KnowledgeIndex` |
| `core/orrery_core/knowledge/backends/` | New — `elasticsearch.py`, `pgvector.py`, `discovery_engine.py` |
| `core/orrery_core/knowledge/sources/` | New — `filesystem.py`, `git.py`, `confluence.py` |
| `core/orrery_core/knowledge/embedding.py` | New — `resolve_embedder()` (Gemini / OpenAI / Ollama) |
| `core/orrery_core/knowledge/tool.py` | New — `knowledge_search` over `BaseRetrievalTool` |
| `core/orrery_core/knowledge/factory.py` | New — `resolve_retriever()` from env |
| `agents/orrery-assistant/orrery_assistant/agent.py` | Attach `knowledge_search` to the chat root when a backend is configured |
| `Makefile` | `knowledge-sync` target |
| `docker-compose.yml`, `deploy/helm/` | pgvector image (phase 2 only) |
| `docs/knowledge.md` | New — sources, backends, sync, the built-in-grounding warning |
| `core/tests/test_knowledge_*.py` | Protocol conformance per backend; chunking; provenance; staleness |

## Acceptance Criteria

- [ ] `KnowledgeSource` / `KnowledgeRetriever` / `KnowledgeIndex` defined, with a shared conformance test suite every backend must pass
- [ ] `knowledge_search` is a `BaseRetrievalTool` and is observed by safety screening, PII redaction, output cap and audit — asserted by test, not assumed
- [ ] `VertexAiSearchTool` (built-in grounding) is never wired; a test fails the build if it is imported into an agent
- [ ] Elasticsearch backend works against the container `make up` already starts, with no new infrastructure
- [ ] pgvector backend behind the same protocol, with `resolve_embedder()` provider-agnostic
- [ ] Discovery Engine backend implements retrieval only; sync skips it cleanly
- [ ] `ORRERY_KNOWLEDGE_BACKEND=none` (default) attaches no tool
- [ ] Every `Passage` carries uri, title, section, revision and `updated_at`; the tool result surfaces document age
- [ ] `make knowledge-sync` is incremental where the source supports it and idempotent where it does not
- [ ] Restricted sources are excluded by configuration; a source that cannot report visibility is refused
- [ ] Retrieval-quality eval set with source-match scoring

## Notes

- **Runbooks may not belong here at all.** ADK ships native Skills
  (`SKILL.md` progressive disclosure, `SkillToolset`). For *procedural*
  knowledge with a known trigger — "ISR shrink on cluster X → these six steps" —
  a skill is deterministic, reviewable in a pull request and cannot retrieve the
  wrong chunk. Retrieval earns its keep on *unstructured* recall across
  postmortems and past incidents, where the trigger is not known in advance. The
  recommended split is runbooks as skills (AEP-008), everything else here — and
  that split lets the runbook half ship with no vector store at all.
- **Staleness is a correctness property.** A runbook indexed six months ago and
  edited since is worse than no runbook. `revision` is required on `Document`
  for exactly this reason, and re-sync compares it rather than re-embedding
  blindly.
- The two seams are what make this AEP worth its size. A deployment that adds
  Notion writes one `KnowledgeSource`; a deployment that standardizes on Azure
  AI Search writes one `KnowledgeRetriever`. Neither touches an agent.
