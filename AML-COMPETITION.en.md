# AML Competition Entry (Cycle 2) — MemoryCore (Online Memory Governance)

> Submission draft: method disclosure / architecture / deployment / tests.
> Route: Open-Method Leaderboard + Academic · Code (public GitHub repo + Docker startup, platform builds and evaluates).

## 1. Method (technical approach)

MemoryCore is a memory governance layer for LLM agents (MIT open source).
This entry takes a **differentiated route: memory governance, made online** — no
multi-hop retrieval augmentation (query rewriting / iterative search). We move
MemoryCore's already-validated governance capabilities onto the AML Add/Search
paths:

- **Write-side governance (executed online in Add)**
  - Fact fragmentation: messages are split into self-contained fact fragments at
    sentence boundaries (long messages are split, short messages stored whole)
  - Stale filtering: stale status records (short markers like "already fixed")
    are not written
  - Semantic dedup / merge: before writing, candidates are recalled within the
    sample scope; the same fact is skipped; similar facts are merged into one
    entry ("plan A" and "plan A changed to B" end up as one memory, not two).
    Merging uses literal similarity as the primary signal (measured: embedding
    scores run spuriously high for short same-vocabulary sentences, so a pure
    semantic gate would wrongly merge unrelated facts)
  - Temporal annotation: every entry carries a persistence timestamp at write
    time, which participates in retrieval ranking
- **Retrieval-side governance (executed online in Search)**
  - In-sample recall (top_k opened to 100), ranked by "similarity × time decay"
    (half-life 90 days; important memories do not decay — facts not mentioned
    for a long time are naturally down-weighted, recent facts are seen first)
  - Options fallback recall: for multiple-choice questions, the candidate
    options are appended to the retrieval query for one fallback recall
    (not iterative search; options are only used as retrieval context, never
    to generate answers or write memories)

**Sample isolation (hard constraint)**: user_id maps 1:1 to the storage layer's
author_id. Writes, dedup recall, and retrieval all carry it. The storage layer
filters by author_id at the SQL level, and write-side dedup is per-user
isolated — no memory is retrievable across user_ids (covered by tests).

**What we deliberately do not do (boundary statement)**: no query rewriting /
multi-hop retrieval, no external LLM for answer generation, Search returns only
verbatim memory evidence.

## 2. Architecture

```
AML HTTP (FastMCP custom routes: /add /search /health, optional Bearer/Token/X-Api-Key)
   │
   ├─ Add:    fact fragmentation → stale filter → semantic dedup/merge → cold-tier write (author_id=user_id)
   ├─ Search: in-sample recall (author_id filter, top_k≤100) → time-decay ranking → AML format
   └─ Health: 2xx liveness probe (still 200 when storage degraded, reports degraded)
        │
        ▼
MemoryCore cold-store client (per-user engine, in-process SQLite + vector index)
        │
        ▼
embedding: ollama + qwen3-embedding:0.6b (local, no external API dependency)
```

## 3. Attribution of code sources (Academic · Code hard requirement)

This project builds on the following open-source components; all modifications are listed:

| Component | License | Purpose | Our changes |
|---|---|---|---|
| memorycore-aml (this repo, based on origin-memorycore) | MIT | Governance layer (hot/cold routing, dedup, overflow, recycle bin, decay) | Added AML HTTP adapter `memorycore/aml_server.py`; added author_id and other identity parameter pass-through in `cold_store_client.py` (default None, original behavior unchanged); added all_sessions count in `stats()`; added uvicorn dependency |
| mnemosyne-memory 3.15.1 | MIT | Storage engine (in-process SQLite + sqlite-vec vector retrieval + multi-identity filtering) | No library code changed; sample isolation is achieved purely through its native author_id / recall filter parameters |
| ollama + qwen3-embedding:0.6b | MIT / Apache-2.0 | Local embedding (1024-dim) | No changes |

- Author: moonandecho
- Main repository: https://github.com/moonandecho/origin-memorycore
- Competition repository: https://github.com/moonandecho/memorycore-aml
- Frozen competition commit: see git history (modular commits: identity pass-through / AML adapter / Docker)

## 4. Deployment

### Docker (recommended; used by the platform to build and evaluate)

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
# smoke:
curl http://localhost:8000/health
curl -X POST http://localhost:8000/add -H "Content-Type: application/json" -d '{
  "request_id": "eval:smoke:0",
  "messages": [{"role": "user", "content": "The user is a software engineer."}],
  "user_id": "eval:smoke:u1",
  "session_id": "eval:smoke:s0"}'
curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d '{
  "query": "What is the user\u0027s occupation?",
  "options": ["A. software engineer", "B. teacher"],
  "user_id": "eval:smoke:u1", "top_k": 100}'
```

The image bundles ollama and pulls the embedding model at startup;
the `/data` volume persists the SQLite memory store.

### Bare metal

```bash
pip install .
ollama serve &
ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data python -m memorycore.aml_server   # defaults to 0.0.0.0:8000
```

### Environment variables

| Variable | Default | Meaning |
|---|---|---|
| AML_HOST / AML_PORT | 0.0.0.0 / 8000 | HTTP listen address |
| AML_API_KEY | empty (no auth, smoke mode) | when set, Add/Search require Bearer/Token/X-Api-Key |
| MNEMOSYNE_DATA_DIR | ~/.memorycore/data | SQLite data dir (point to a dedicated dir for evaluation) |
| MEMORYCORE_EMBED_URL | http://localhost:11434/v1 | embedding API (ollama or OpenAI-compatible) |
| MEMORYCORE_EMBED_MODEL | qwen3-embedding:0.6b | embedding model (1024-dim) |

### Error-code semantics

- 400/422 format errors (missing request_id/user_id/session_id, messages not an array, etc.)
- 401 auth failure (when AML_API_KEY is configured)
- 500 storage backend unavailable (temporary failures such as embedding service down; the platform retries 5xx automatically)
- Never returns 202/task IDs; Add returns 200 only after synchronous completion

## 5. Tests (reproducible)

```bash
# requires ollama + qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=$(mktemp -d) python3 tests/test_aml.py
```

24 assertions cover: cross-user_id isolation (A writes, B cannot see),
second write of the same fact deduplicated, "plan A → changed to B" merged into
one entry, /add /search /health HTTP end-to-end, options fallback recall,
long-message fragmentation, error codes.

## 6. Known boundaries (honest statement)

- Retrieval uses the storage layer's lexical + vector hybrid ranking; the storage
  layer applies a lexical relevance gate to long queries; multiple-choice
  scenarios are covered by the options fallback recall, and open-ended
  questions (English natural-language queries) recall correctly in practice.
- Message-provided timestamps are only a reference; ranking uses the persistence
  time (write time); created_at returns the persistence time (the protocol
  allows "source or persistence time").
- Full cold-tier governance sweeps (maintenance batch jobs) are not triggered
  online; write-side governance focuses on dedup / merge / stale filtering.
