# MemoryCore — AML Online-Governance Submission

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore** is a memory governance layer for LLM agents: agents accumulate memory fast — preferences, facts, decisions — and memory that is not maintained quietly degrades. MemoryCore keeps the hot tier within budget, the cold tier findable, and every write deduplicated and mergeable.

This repository is the **Agent Memory Leaderboard (AML) submission**. It runs MemoryCore's verified governance mechanisms **online** on the AML write/retrieval paths:

- **Write governance on `/add`** — message → fact fragments → stale filter → semantic dedup/merge → cold-tier write (author-scoped).
- **Retrieval governance on `/search`** — in-sample recall (top_k up to 100) → similarity × time-decay ranking (90-day half-life, high-importance memories exempt) → AML-format evidence.
- **Strict per-user isolation** — `user_id` maps 1:1 to the storage `author_id`; cross-user retrieval is impossible (SQL-layer filter, test-covered).
- **No external API calls at evaluation time** — embeddings run locally (ollama + `qwen3-embedding:0.6b`, 1024-dim), storage is an in-process SQLite engine ([mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/)).

Upstream open-source project: [moonandecho/origin-memorycore](https://github.com/moonandecho/origin-memorycore) (MIT). This repo adds the AML HTTP adapter `memorycore/aml_server.py` and author-scoped identity passthrough on top of it.

**Full method disclosure, deployment notes, reproducible tests and honest known-boundaries section: [AML-COMPETITION.md](AML-COMPETITION.md)** (English: [AML-COMPETITION.en.md](AML-COMPETITION.en.md)).

## Quick start

### Docker (recommended)

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
```

> The image preloads the embedding model (`qwen3-embedding:0.6b`, ~639 MB) at build time — container startup needs no network. The entrypoint keeps a fallback that pulls the model at runtime only if it is missing (e.g. a custom `MEMORYCORE_EMBED_MODEL`). The data volume `/data` persists the SQLite memory store across restarts.

Smoke test:

```bash
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

### Bare metal

```bash
pip install .
ollama serve &
ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data python -m memorycore.aml_server   # 0.0.0.0:8000 by default
```

## HTTP API

All endpoints are plain REST, served on the FastMCP streamable-http app (uvicorn). Optional auth via `AML_API_KEY` (Bearer / Token / X-Api-Key).

| Endpoint | Description |
|---|---|
| `POST /add` | AML write: messages → fact fragments → stale filter → semantic dedup/merge → author-scoped cold-tier write. Returns 200 only after the write is committed (no async task IDs). |
| `POST /search` | AML recall: author-scoped in-sample recall → time-decay ranking → AML-format evidence list. `options` are appended to the query for a fallback recall pass (retrieval context only — never used to generate answers or write memory). |
| `GET /health` | Liveness. Returns 2xx even when storage is degraded (`status: degraded` + `storage.error`), so the platform can distinguish crash from dependency outage. |

Error-code semantics:

- `400` — malformed request (missing `request_id`/`user_id`/`session_id`, non-array `messages`, …)
- `401` — auth required but missing/invalid (only when `AML_API_KEY` is set)
- `500` — storage backend unavailable (e.g. embedding service down); transient, safe to retry on 5xx

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AML_HOST` / `AML_PORT` | `0.0.0.0` / `8000` | HTTP listen address |
| `AML_API_KEY` | empty (no auth, smoke mode) | when set, Add/Search require Bearer/Token/X-Api-Key |
| `MNEMOSYNE_DATA_DIR` | `~/.memorycore/data` | SQLite data directory (point evaluations at a dedicated dir) |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | embedding API (ollama or any OpenAI-compatible server) |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | embedding model (1024-dim) |

## Architecture

```
AML HTTP (FastMCP custom routes: /add /search /health, optional Bearer/Token/X-Api-Key)
   │
   ├─ Add: fact splitting → stale filter → semantic dedup/merge → cold-tier write (author_id=user_id)
   ├─ Search: in-sample recall (author_id filter, top_k≤100) → time-decay ranking → AML format
   └─ Health: 2xx liveness (degraded when storage is down)
        │
        ▼
MemoryCore cold-store client (per-user engine, in-process SQLite + vector index)
        │
        ▼
embedding: ollama + qwen3-embedding:0.6b (local, no external API dependency)
```

The underlying governance layer (hot/cold routing, write-time normalization dedup, capacity control, overflow, recycle bin) is documented in the [upstream project](https://github.com/moonandecho/origin-memorycore).

## Tests (reproducible)

```bash
# requires ollama + qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=$(mktemp -d) python3 tests/test_aml.py
```

28 assertions cover: cross-user isolation (A writes, B cannot see), second write of the same fact deduplicated, "plan A → changed to B" merged into one memory, full HTTP path for `/add` `/search` `/health`, options fallback recall, long-message splitting, and error codes.

## Known boundaries (honest disclosure)

- Retrieval uses the storage engine's lexical+vector hybrid ranking; the engine applies a lexical-relevance gate on long queries. The options-fallback recall covers multiple-choice scenes; open-ended English natural questions were verified to recall normally.
- Message-supplied `timestamp` is treated as reference only; ranking uses the persisted write time. `created_at` in responses returns the persisted time (the protocol's "source/persisted time").
- No online cold-tier full-maintenance pass; write-side governance is dedup/merge/stale-filter oriented.

## License

MIT — see [LICENSE](LICENSE). The engine dependency [mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/) and [ollama](https://ollama.com) + qwen3-embedding are MIT / Apache-2.0. See [AML-COMPETITION.md](AML-COMPETITION.md) for the per-component change disclosure.
