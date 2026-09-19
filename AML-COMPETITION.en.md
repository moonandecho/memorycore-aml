# MemoryCore — Agent Memory Leaderboard (Cycle 2) Submission — Textual Memory Track · Open-Source Methods Board

> Submission notes: method description / deployment / disclosure / reproduction.
> Written 2026-09-19 against the current Cycle 2 specification; the public pages will be re-checked before submission.

## 0. At a glance

| Item | Value |
|---|---|
| System name and version | MemoryCore (AML edition) v0.1.0; **fixed commit (evaluated code): `68a77c5aee451c13cc987be10f3b4efbfcd8e6b8`** |
| Evaluation type | **Open-Source Methods board** (participant-hosted Add / Search API; not a repository submission) |
| Track | **Textual memory** (no multimodal track, no code track this cycle) |
| Add endpoint | `http://47.108.28.129:18000/add` |
| Search endpoint | `http://47.108.28.129:18000/search` |
| Health check | `http://47.108.28.129:18000/health` (no auth, 2xx) |
| Authentication | header `X-Api-Key: <Memory System Key>`; `Authorization: Bearer` and `X-Token` also accepted |
| Public repository | https://github.com/moonandecho/memorycore-aml |
| Prior work | MemoryCore / origin-memorycore (ours, MIT); mnemosyne-memory 3.15.1 (MIT); ollama + qwen3-embedding:0.6b |
| License | MIT |
| Availability commitment | publicly reachable, version-fixed until **2026-12-05** (covers the 30-day post-submission commitment, see §2.4) |
| Team (leaderboard attribution) | **moonandecho** (individual entry) |
| Contact | submitted to the organisers via the **Evaluation Access Request** (real name + email); this public repository does not display personal names |
| Public-display consent | system name, method description, public repository and fixed commit; evaluation data and private results are not public |

## 1. Method description

MemoryCore is a **memory governance layer** for LLM agents (MIT). Our route for this cycle is to
**run the governance mechanisms online**: instead of multi-hop retrieval augmentation (query rewriting /
iterative search), we execute the governance capabilities already validated in production directly on the
AML **write (Add) and retrieval (Search) paths**.

- **Write governance on `/add`** (returns 200 only after the write is persisted)
  - Fact splitting: messages are split into self-contained fragments on sentence boundaries
    (long messages split, short messages kept whole; fragment cap 300 characters).
  - Stale filtering: stale-state markers ("already fixed" style) are not persisted.
  - Semantic dedup / merge: candidates are recalled before writing; verbatim duplicates are skipped and
    similar facts are merged into one memory ("plan A" and "plan A changed to B" end up as a single memory).
    Merging uses literal similarity as the primary signal and **preserves the original separators** —
    newlines and semicolons are no longer re-joined, so code snippets and commands are not corrupted.
  - Temporal annotation: every memory carries its persisted timestamp, which participates in ranking.
- **Retrieval governance on `/search`**
  - In-sample recall (`top_k` ≤ 100), ranked by similarity × time decay (90-day half-life; long-unmentioned
    facts decay naturally).
  - **Read-only recall**: retrieval does **not** modify a memory's recency state (it does not bump
    `last_recalled` / `recall_count`), so repeated identical searches are **byte-for-byte reproducible**
    (storage-layer patch disclosed in §4.3).
  - Options fallback: for multiple-choice questions the options are concatenated into the retrieval query
    for one fallback recall (retrieval context only — no answer generation, no memory writes).
- **Per-user isolation (hard constraint)**: `user_id` maps 1:1 to the storage-layer `author_id`, carried through
  writes, dedup recall and retrieval; the storage layer filters by `author_id` at the SQL level, so memories are
  invisible across `user_id`s (test-covered).
- **Out of scope (boundary statement)**: no query rewriting / multi-hop retrieval; no external LLM generates
  answers; Search returns memory evidence only.

## 2. Endpoints, authentication, capacity, operational constraints

### 2.1 Endpoints

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/add` | required | synchronous write; `request_id` is idempotent (a repeated request is not written twice) |
| POST | `/search` | required | returns `{"data":[{"id","content","score","created_at"}...]}` ordered by descending score |
| GET | `/health` | none | liveness; still 2xx when storage is degraded, with a `degraded` marker in the body |

### 2.2 Capacity declaration

### 2.2 Capacity declaration (measured, 2026-09-19)

| Item | Measured | Notes |
|---|---|---|
| Add, 64 concurrent × 20 messages | **103.5 s wall, 100% success**, p50 56.4 s, p95 100.2 s | measured **through the public participant-hosted endpoint**; the 1200 s per-request timeout leaves ~11× headroom |
| Same, internal network | 109.5 s wall, p50 60.0 s, p95 107.1 s | local re-run |
| Single Add (20 messages) | 3.4–4.5 s | ~4 embedding HTTP calls per request (batched) |
| Single Search | 0.28 s | top_k=3 over the public endpoint; top_k=100 returns 155.7 KB (0.5% of the 30 MiB limit) |
| Health check | millisecond-level (always < 1 s) | in-memory snapshot, never touches storage |
| Resident memory | ~62 MB (2 GB cap, plus byte-weighted backpressure) | single machine |
| Embedding throughput | ~14.6–16.0 texts/s batched (GPU-resident) | see §4.5 |

> Search at 32 concurrency was not separately stress-tested (single Search 0.28 s; its embeddings benefit from the same batching and cache).

- Add: 64 concurrent × 20 messages per request → success rate __%, p50 __ ms, p95 __ ms, p99 __ ms, peak RSS __ MB
- Search: 32 concurrent → success rate __%, p95 __ ms
- Per-request limits: ≤20 messages / ≤2,000 words (spec); measured Add wall-clock p95 __ s (< 1200 s platform timeout)
- Response size: at `top_k=100` measured __ KB, i.e. __% of the 30 MiB per-response limit

### 2.3 Operational constraints

- Deployment: single participant-hosted machine, CPU inference, no external API dependency (embeddings via local ollama).
- Per-evaluation `user_id` data is physically isolated by `author_id` in storage; self-test data is cleared before submission.
- **No agent framework is required**: the service starts standalone in a clean environment (evidence in §5.4).
- Version freeze: the submitted version is not changed after submission (spec requirement).

### 2.4 Availability commitment and expiry

- The endpoint stays publicly reachable and version-fixed throughout the evaluation period, committed until **2026-12-05**.
  (Spec: a deployed endpoint must remain publicly reachable and stable for **at least 30 days after submission**.)
- After expiry a **self-expiring mechanism** tears the channel down automatically (service stopped, firewall rule
  reclaimed, data directory archived) — no manual clean-up left behind.

## 3. Architecture

```
AML HTTP (/add /search /health, X-Api-Key / Bearer / X-Token auth)
   │
   ├─ Add: fact splitting → stale filter → dedup/merge (separators preserved) → cold-tier write (author_id=user_id)
   ├─ Search: in-sample recall (author_id filter, top_k≤100) → time-decay ranking (read-only) → AML format
   └─ Health: 2xx liveness (2xx with `degraded` marker when storage is down)
        │
        ▼
MemoryCore cold-store client (per-user engine, in-process SQLite + vector index)
        │
        ▼
embedding: ollama + derived tag qwen3-embedding-aml-ctx1024 (FROM qwen3-embedding:0.6b; num_ctx 1024, fully GPU-resident; local, 1024-dim, no external API)
```

## 4. Prior work, attribution and changes

### 4.1 Prior work and licenses

| Component | License | Role |
|---|---|---|
| origin-memorycore (our upstream open-source project) | MIT | memory governance layer (hot/cold routing, dedup, merge, overflow, recycle bin, decay) |
| mnemosyne-memory 3.15.1 | MIT | storage engine (in-process SQLite + vector search + multi-identity filtering) |
| ollama + qwen3-embedding:0.6b | MIT / Apache-2.0 | local embeddings (1024-dim) |

### 4.2 Method changes made in this submission (relative to origin-memorycore)

| Change | Location | Description |
|---|---|---|
| AML HTTP adapter | `memorycore/aml_server.py` (new) | `/add` `/search` `/health`, request validation, idempotency, auth, exception→error-code mapping |
| Identity passthrough | `memorycore/cold_store_client.py` | `author_id` and related identity parameters (default `None`, original behaviour unchanged) |
| Merge fidelity | `memorycore/core/overflow.py` | merging two memories now **preserves the original separators** (fixes code/command corruption caused by re-joining newlines/semicolons into a full stop) |
| Stricter dedup threshold | `memorycore/core/overflow.py` | only near-verbatim duplicates are treated as duplicates, so distinct facts are no longer swallowed |
| Read-only recall | `memorycore/aml_server.py` + storage-layer patch | retrieval no longer refreshes recency state (see §4.3) |
| Evidence length | `memorycore/cold_store_client.py` | per-item returned evidence cap 500 → **2000** characters, overridable via `MEMORYCORE_CONTENT_MAX_CHARS` |
| Fail-closed data dir | `memorycore/aml_server.py` | a missing/unwritable data directory is a hard error (no silent fallback) |
| Stats | `memorycore/cold_store_client.py` | `stats()` reports an additional `all_sessions` count |
| Dependency | `pyproject.toml` | added `uvicorn` |

### 4.3 Storage-layer patch (explicit disclosure)| Embedding cache | `mnemosyne/core/embeddings.py` (patch) | in-process LRU (key = model fingerprint + endpoint + query/doc prefix + text hash; entry and byte caps; can be disabled) |
| Batched embedding | `memorycore/aml_server.py` + patch | fragment texts of one request embedded in batches (vectors identical to single calls, max abs diff 0.0) |
| Two-phase Add | `memorycore/aml_server.py` | precompute phase (no writes) + short write phase; the request budget only fires before any write; ledger checkpoints allow resume |
| Concurrency & backpressure | `memorycore/aml_server.py` | bounded thread pool + per-`user_id` serialization + embedding concurrency gate + byte-weighted admission; retryable 5xx when exceeded |
| Storage concurrency safety | `memorycore/cold_store_client.py` | global lock for the shared engine + per-user engine locks; bounded retry on `database is locked` |
| Health isolation | `memorycore/aml_server.py` | `/health` serves an in-memory snapshot updated by a background prober (no storage access, millisecond response) |
| Data-directory identity guard | `memorycore/cold_store_client.py` | compares `(st_dev, st_ino)` at runtime; rebuilds engines and drops stale connections when the data directory is replaced; `/health` exposes `db_identity*` |
| Embedding prefetch & write-phase isolation | `memorycore/aml_server.py` + patch | Phase A prefetches all vectors so the write phase issues no embedding calls; prefetched vectors are reused even with the cache disabled, preventing "200 written but no vector" |
| Resume & idempotency | `memorycore/aml_server.py` + `cold_store_client.py` | ledger stores `phase/checkpoint`, retries resume remaining fragments, completed requests replay 200, and an exact pre-write lookup avoids replay-induced state changes |

### 4.3 Storage-layer patch (explicit disclosure)

- Component: mnemosyne-memory 3.15.1 (MIT).
- Patch: `patches/mnemosyne-recall-readonly.patch`.
- Content: the engine's recall entry point **accepts and honours a read-only flag** (it does not bump
  `last_recalled` / `recall_count`). **Default behaviour is unchanged** — the flag must be passed explicitly.
- Rationale: evaluation includes repeated retrieval of the same question (streaming / multi-turn), which requires
  reproducible results; the unpatched implementation refreshes recency state during recall, so a second identical
  search returned a different ordering.
- Reproduction: `patches/README.md` documents application, the pinned version (3.15.1) and verification steps.

- Patch 2: `patches/mnemosyne-embed-cache.patch`
  - Content: an **in-process LRU embedding cache** (key = model fingerprint (tag/digest/ctx/GPU layers) + endpoint +
    query/doc prefix + text hash; entry cap 8192, byte cap 64 MiB; can be disabled by env var).
  - Rationale: identical text is re-embedded within one request (20 of 60 calls measured), and evaluation corpora
    contain many repeated lines/templates.
  - Properties: **no change to retrieval semantics** — identical inputs always return the identical vector (more
    reproducible than recomputation); batched vs single vectors measured at max absolute difference 0.0.
  - Reproduction: `patches/README.md` (application, pinned version 3.15.1, verification, disable switch).

### 4.4 Parameters that affect results (disclosed)

- Fact fragment cap: **300 characters** (write path, sentence-boundary splitting).
- Returned evidence cap: **2000 characters** per item (`MEMORYCORE_CONTENT_MAX_CHARS` overrides).
- `top_k` cap 100; time-decay half-life 90 days.
- No external LLM is used in writing or retrieval; no answer generation.

## 5. Reproduction

**Version and fixed commit**: system version `0.1.0`; evaluated code pinned at **`68a77c5aee451c13cc987be10f3b4efbfcd8e6b8`**
(the deployed `/health` reports the same commit for cross-checking; documentation commits in this repo are not part of the frozen version).

### 5.1 Local start (bare metal)

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -i https://mirrors.aliyun.com/pypi/simple/ .
ollama serve & ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data AML_API_KEY=<your-key> python -m memorycore.aml_server
# smoke: curl http://localhost:8000/health
```

### 5.2 Tests

```bash
MNEMOSYNE_DATA_DIR=$(mktemp -d) python -m pytest -q      # 670 passed (2026-09-19)
MNEMOSYNE_DATA_DIR=$(mktemp -d) python tests/test_aml.py # standalone end-to-end checks
```

### 5.3 Capacity benchmark (re-runnable)

```bash
python scripts/bench_add.py --concurrency 64 --messages 20 --rounds 3 --port 8870
```

### 5.4 Clean-environment evidence (framework independence)

`scripts/smoke_clean_env.sh` starts the service in an `env -i` clean environment and exercises
Add / Search / Health, demonstrating that the submission does not depend on any agent framework.

### 5.5 Docker (optional, local reproduction only)

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
```

> Cycle 2 does not accept repository-only or Docker-only submissions deployed by AML; Docker is provided for local reproduction convenience.

## 6. Known boundaries (honest disclosure)

- Write-path fragments are capped at 300 characters: very long messages are split into several independently
  retrievable fragments (trade-off: each fragment is findable on its own, at the cost of long evidence being split).
- A message's own `timestamp` is not used as event time; ranking uses the persisted write time, and `created_at`
  returns the persisted time.
- No query rewriting / multi-hop retrieval; open-question recall relies on the storage layer's lexical + vector
  hybrid ranking (which applies a lexical relevance gate).
- Cold-tier maintenance sweeps are not triggered online (write-path governance covers dedup / merge / stale filtering).
- Single-machine CPU inference: the capacity ceiling is the hardware itself (§2.2 gives measured numbers).
- The multimodal and code tracks are not entered this cycle; this document covers the textual memory track only.

## 7. Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `AML_HOST` / `AML_PORT` | `0.0.0.0` / `8000` | HTTP bind address and port |
| `AML_API_KEY` | empty (no auth) | when set, `/add` and `/search` require `X-Api-Key` / `Authorization: Bearer` / `X-Token` |
| `MNEMOSYNE_DATA_DIR` | none (**missing ⇒ hard error**) | storage directory; point evaluation at a dedicated directory |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | embedding service (local ollama or OpenAI-compatible) |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | embedding model (1024-dim) |
| `MEMORYCORE_CONTENT_MAX_CHARS` | `2000` | per-item evidence cap on `/search` (invalid values fall back to 2000 with a warning) |

## 8. Error-code semantics

- `400` malformed request (missing `request_id` / `user_id` / `session_id`, `messages` not an array, …)
- `401` authentication failure (when `AML_API_KEY` is configured)
- `422` semantically invalid request (e.g. missing/invalid `top_k`, invalid message `role`) — the spec treats 4xx as format errors that are not retried
- `500` storage backend unavailable (e.g. embedding service unreachable); 5xx is retried by the platform
- Valid requests **always return 200** (including a batch fully filtered by write-path governance, which is reported
  in the body as a governance count). No 202 / task IDs; `/add` returns 200 only after the write is persisted.

## English (to merge into §4.4 of AML-COMPETITION.en.md)

### 4.5 Runtime configuration and behaviour boundaries (disclosed)

- **Embedding model**: local ollama serving a derived tag **`qwen3-embedding-aml-ctx1024`**
  (`num_ctx=1024`, all layers offloaded to GPU) built `FROM qwen3-embedding:0.6b` — same weights, no model swap.
- **Context effects**: stored fact fragments are capped at 300 characters and are unaffected (cosine 1.000000 vs the
  full-context configuration). Search queries are not split; **input beyond ~1024 tokens is truncated**, with measured
  cosine deviation <1.9% on long text, which may reorder near-tie results — disclosed explicitly.
- **Embedding cache**: in-process LRU (key = model fingerprint + endpoint + query/doc prefix + text hash; entry and byte
  caps; can be disabled by env var). Identical inputs always return the identical vector (more reproducible than
  recomputation); batched vs single vectors measured at max absolute difference 0.0.
- **Batching**: fragment texts within one request are embedded in batches (vectors identical to single calls, max abs diff 0.0).
- **Concurrency and backpressure**: Add/Search work runs in a bounded thread pool with per-`user_id` serialization;
  embedding calls are globally capped; admission is limited by request bytes and request count, returning **retryable 5xx**
  when exceeded (never silently dropped).
- **Per-request budget**: default 600 s; exceeding it returns a retryable 5xx **before any write**; requests that have
  entered the write phase are never interrupted, so no partial writes can occur (a retry with the same `request_id`
  yields the same result as a single successful execution).
- **Health**: `/health` serves an in-memory snapshot, never touches storage, and **always responds within 1 s**;
  it exposes model residency (GPU/CPU) and a degradation flag.
- **Capacity declaration**: see §2.2 (measured numbers for 64 concurrent Add / 32 concurrent Search).
