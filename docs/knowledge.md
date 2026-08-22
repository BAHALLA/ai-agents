# Knowledge Retrieval

Gives the agent access to what *humans wrote* — runbooks, postmortems, ADRs —
alongside the live infrastructure its tools already read. Implements
[AEP-025](enhancements/aep-025-knowledge-retrieval.md), phase 1.

Off by default. Nothing changes until `ORRERY_KNOWLEDGE_BACKEND` is set.

## Quick start

```bash
make up PROFILES=elastic                     # Elasticsearch on :9200
export ORRERY_KNOWLEDGE_BACKEND=elasticsearch
make knowledge-sync                          # index docs/runbooks + docs/adr
make run-api                                 # the coordinator now has search_knowledge
```

Or with semantic search (phase 2):

```bash
make up                                      # postgres now ships pgvector
export ORRERY_KNOWLEDGE_BACKEND=pgvector
export EMBEDDING_PROVIDER=gemini             # defaults to MODEL_PROVIDER
make knowledge-sync
```

`make knowledge-sync` prints what it did:

```
indexed 30 document(s) as 355 chunk(s); skipped 0 unchanged; pruned 0 stale chunk(s); removed 0 deleted document(s)
```

Re-run it and everything is skipped — sync compares each document's revision
against what the index holds, so a second pass costs one aggregation query.

## How it fits together

Two seams, deliberately separate, because a managed vendor owns both halves of
the problem while a self-hosted store owns neither:

| Seam | Protocol | Who implements it |
|------|----------|-------------------|
| Ingestion | `KnowledgeSource` → `Document` | `FilesystemSource`, `GitSource` |
| Write | `KnowledgeIndex` | Self-hosted backends only |
| Retrieval | `KnowledgeRetriever` → `Passage` | Every backend |

Adding Notion means writing one `KnowledgeSource`. Standardising on Azure AI
Search means writing one `KnowledgeRetriever`. Neither touches an agent.

A backend that implements only `KnowledgeRetriever` is declaring "my ingestion
is somebody else's job" — `knowledge-sync` reports that and exits cleanly
rather than failing.

## Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `ORRERY_KNOWLEDGE_BACKEND` | `none` | `none`, `elasticsearch`, or `pgvector` |
| `ORRERY_KNOWLEDGE_TOP_K` | `6` | Passages per query |
| `ORRERY_KNOWLEDGE_MAX_CHARS` | `1600` | Chunk budget (~400 tokens) |
| `ORRERY_KNOWLEDGE_OVERLAP_CHARS` | `200` | Context carried between chunks |
| `KNOWLEDGE_ES_URL` | `http://localhost:9200` | Index cluster |
| `KNOWLEDGE_ES_INDEX` | `orrery-knowledge` | Index name |
| `KNOWLEDGE_ES_API_KEY` | — | Or `KNOWLEDGE_ES_USERNAME`/`_PASSWORD` |
| `KNOWLEDGE_ES_VERIFY_CERTS` | `true` | `KNOWLEDGE_ES_CA_CERTS` sets a bundle |
| `KNOWLEDGE_ES_SEARCH_TIMEOUT` | `10s` | Server-side query ceiling |
| `KNOWLEDGE_PG_URL` | `DATABASE_URL` | pgvector store (needs the `vector` extension) |
| `KNOWLEDGE_PG_TABLE` | `orrery_knowledge_chunks` | Table name |
| `EMBEDDING_PROVIDER` | `MODEL_PROVIDER` | `gemini`, `openai`, `ollama`, … |
| `EMBEDDING_MODEL` | per provider | Overrides the provider default |
| `EMBEDDING_DIMENSIONS` | per model | Must match the index column |
| `KNOWLEDGE_CONFLUENCE_URL` | — | e.g. `https://acme.atlassian.net` |
| `KNOWLEDGE_CONFLUENCE_SPACES` | — | Comma-separated space keys (required) |
| `KNOWLEDGE_CONFLUENCE_EMAIL` | — | Integration user |
| `KNOWLEDGE_CONFLUENCE_API_TOKEN` | — | API token |

`KNOWLEDGE_ES_*` is deliberately **not** `ELASTICSEARCH_*`: the Elasticsearch
*agent* diagnoses somebody's production cluster, while this indexes our own
corpus. They are frequently different clusters with different credentials, and
sharing one variable set would make that impossible to express.

### Two different failure policies

- **Misconfiguration fails fast at startup.** An unknown backend name raises,
  because a pod that comes up healthy while silently serving no corpus is worse
  than one that refuses to start.
- **An unreachable backend at query time does not.** The tool returns an error
  the agent reports and works around. Knowledge is an augmentation; the
  platform diagnosed incidents without it before this existed.

That split is why "no results" and "cluster down" are different results. If
they looked the same, a broken index would quietly become "we have no runbook
for that".

## Why retrieval is a tool, not grounding

`search_knowledge` subclasses ADK's `BaseRetrievalTool`, so its result travels
the normal after-tool chain:

- `SafetyScreenPlugin` neutralizes injected spans. **Retrieved documents are
  attacker-reachable text** — a Confluence page or git-hosted runbook is
  editable by anyone with write access to the source, the same threat model as
  a pod annotation.
- `PIIRedactionPlugin` scrubs credentials. Postmortems contain pasted tokens.
- `ToolOutputCapPlugin` bounds a chatty retrieval.
- `AuditPlugin` records the query.

!!! danger "Never wire `VertexAiSearchTool`"
    Despite the name it is **model built-in grounding**, not a tool. Its
    `process_llm_request` appends a `types.Retrieval` to the LLM request config
    and the model retrieves server-side, so there is no `after_tool_callback`
    and it bypasses **all four** protections above — and never appears in the
    audit log. Reach a managed backend through `DiscoveryEngineSearchTool` (a
    real `FunctionTool`) or an adapter behind `KnowledgeRetriever`.
    `agents/orrery-assistant/tests/test_knowledge_wiring.py` fails the build if
    a grounding tool is ever attached to an agent.

## What the agent sees

Every passage carries its provenance, because an operator at 03:00 cannot
otherwise distinguish a retrieved fact from a hallucination:

```json
{
  "text": "Restart the broker after confirming ISR has not recovered…",
  "source": "git://docs/runbooks/kafka-isr-shrink.md",
  "title": "Kafka ISR shrink",
  "section": "Recovery",
  "revision": "a1b2c3d",
  "age_days": 12,
  "stale": false,
  "score": 14.05
}
```

`stale` marks documents untouched for over 180 days. It is a flag, not a
filter — a two-year-old runbook may be the only one there is, and the model
should discount it rather than never see it.

## Sources

| Source | URI | Revision | Use when |
|--------|-----|----------|----------|
| `FilesystemSource` | `file://<path>` | `mtime:size` | Local tree, no git |
| `GitSource` | `git://<path>` | Commit sha | Anything in the repo (default) |

Prefer `GitSource`: the revision is content-derived and stable across clones,
so CI re-indexes only what a merge actually changed, and a citation names a
revision an operator can check out. Filesystem mtimes change on a fresh clone
even when content did not.

```bash
make knowledge-sync ROOT="docs/runbooks docs/adr"   # pick the trees
make knowledge-sync GIT=0                            # mtimes instead of shas
make knowledge-sync PRUNE=0                          # keep unseen documents
```

### Deletion and pruning

A document no source produced any more is removed from the index — a retired
runbook must stop being retrievable. But pruning is **skipped when the run had
any error**: a source that failed halfway looks identical to one whose
documents were all deleted, and acting on that ambiguity would empty the corpus
because a Confluence token expired.

Use `PRUNE=0` when syncing a subset of sources, where "absent" does not mean
"deleted".

## Chunking

Headings first, then size. A runbook's "Recovery" section is a semantic unit,
and a fixed-width window straddling "Symptoms" and "Recovery" retrieves half of
each and reads as neither. Splitting on headings also gives every chunk a
`section`, which is what makes a citation useful — "§ Recovery" beats
"chunk 47".

Budgets are in **characters**, not tokens, so chunking has no tokenizer
dependency and stays identical across model providers (~4 chars per token for
English prose).

Fenced code blocks are never split. A truncated YAML manifest or half a shell
pipeline is worse than an oversized chunk, because the model will act on the
fragment.

## Access control

Retrieval runs at `viewer` and is **not** ACL-aware. The rule is therefore
explicit: **only index sources every viewer may read.** Exclude restricted
Confluence spaces and private directories by configuration. Per-principal
filtering is deferred to a later phase; pretending to enforce ACLs that are not
enforced would be worse than not indexing.

## Choosing a backend

| | `elasticsearch` | `pgvector` |
|---|---|---|
| Matching | BM25 lexical | **Hybrid** — semantic + lexical, fused |
| Infrastructure | Container `make up` already starts | Postgres with the `vector` extension |
| Embeddings | none | an `EMBEDDING_PROVIDER` and its cost |
| Finds "broker unreachable" → "kafka node down" | ✗ | ✓ |

Start with Elasticsearch. Move to pgvector when queries are phrased in the
words of the incident rather than the words of the runbook.

### Why pgvector is hybrid, not pure vector

Semantic search is weakest exactly where SRE queries are strongest: an exact
identifier. A query for `CrashLoopBackOff` or a specific consumer-group name
wants the document containing that literal string, and a nearest-neighbour
search will happily return three plausible-sounding pages that never mention
it.

So both rankings are computed and fused with Reciprocal Rank Fusion. RRF
combines *ranks*, not scores — deliberately, because cosine distance and
`ts_rank_cd` share no scale, and any weighted sum of the raw numbers would be
dominated by whichever happens to have the larger range.

### The image swap

`docker-compose.yml` now uses `pgvector/pgvector:pg16` instead of
`postgres:16-alpine`. It is the stock Postgres image plus the extension — same
data directory, same defaults — so an existing volume keeps working and
sessions, memory and confirmations are unaffected.

The Helm chart does not ship a database (it takes an external `DATABASE_URL`),
so nothing changes there beyond pointing at a server that has the extension.
Managed Postgres offerings generally support `CREATE EXTENSION vector`.

### Changing the embedding model

Requires a re-index — vectors from different models are not comparable. The
backend checks the column width against the configured embedder at
`ensure_ready()` and fails with an explicit message rather than letting every
query die on a cast error:

```
Index column 'orrery_knowledge_chunks.embedding' holds vector(3072) but the
configured embedder produces 1536 dimensions.
```

## Confluence

Opt-in, and it **refuses to auto-discover spaces**. Retrieval runs at `viewer`
for everyone, so indexing a space nobody explicitly listed would make
restricted content readable through the agent with no trace on the Confluence
side. Only the spaces in `KNOWLEDGE_CONFLUENCE_SPACES` are ever fetched, and a
403 fails loudly rather than being skipped.

Page *version* is the revision, so a re-sync only fetches bodies for pages that
actually moved — the difference between polling a few dozen files and polling
thousands over a rate-limited API.

Storage-format XHTML is flattened to markdown-ish text: headings become ATX so
the heading-aware chunker keeps working, and code macros become fences so the
chunker treats them as atomic. Layout macros and attachments are dropped — a
half-rendered macro reads as garbage to a model, and the prose around it is
what carries the answer.

## Not yet implemented

- **Retrieval-quality evals** — the existing 33 scenarios score
  `tool_trajectory_avg_score` only, which is blind to whether the right
  document came back.
- **ACL-aware retrieval** — see the access-control section above.
