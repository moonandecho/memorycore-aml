# origin-memorycore

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore is a memory governance layer for LLM agents.**

Agents accumulate memory fast — preferences, facts, decisions — and memory that isn't maintained quietly degrades: duplicates accumulate, stale facts linger, the hot tier fills up and starts rejecting writes. MemoryCore keeps that from happening.

It works as a two-tier memory system:
- **Hot tier** — frequently-used behavioral knowledge (preferences, rules, corrections) in a fast local file, always in context.
- **Cold tier** — low-frequency facts, automatically migrated out, stored in an in-process SQLite engine (or a remote memory service if you configure one).

Between the two, a governance core keeps memory healthy:
- **Write-time dedup** — similar facts are merged before storing, not duplicated.
- **Capacity control** — soft/hard thresholds trigger overflow before the hot tier is full, so it never rejects writes.
- **Cold-tier governance** — periodic dedup/cleanup passes keep the cold tier findable as it grows.
- **Recycle bin** — deleted entries get a 30-day grace period; recalling a trashed entry revives it.

The result: the hot tier stays within budget, the cold tier stays findable, and memory remains maintainable no matter how much the agent accumulates.

Built on the [MCP](https://modelcontextprotocol.io) (Model Context Protocol) `streamable-http` / stdio standard. Works with any MCP client, tested with [Hermes Agent](https://github.com/NousResearch/hermes-agent).

## Features

- **Memory governance (the core)** — three layers of protection for cold-tier data integrity:
  - **Cold-write dedup**: before writing to the cold tier, a semantic recall + LLM judge checks for duplicates and updates existing entries instead of creating redundant ones.
  - **Capacity hard gate**: cold tier enforces a soft limit (6000 entries, triggers one maintenance pass) and a hard limit (10000 entries, forces maintenance loops) — prevents unbounded growth.
  - **Recycle bin** (`trash_store.py`): deleted cold-tier entries are moved to `~/.memorycore/trash.json` with a 30-day expiry. Recalling a trashed entry with fresh semantic evidence restores it ("recall to revive").
- **Cold/hot routing** — every write is classified: high-importance or preference-like → hot (local); low-frequency fact → cold (remote); stale status record → dropped.
- **Six-step overflow** — capacity baseline → dedup → stale filtering → merge → safe write (cold first, then delete local) → verification.
- **Cold-tier maintenance** — dedup merge, stale cleanup, conflict resolution, embedding integrity check.
- **Capacity control** — soft threshold (overflow once before writing) / hard threshold (force overflow) / target ratio. Defaults: 60% / 80% / 40% of a 5000-char limit.
- **Graceful degradation** — cold tier unreachable? Writes fail loudly (never silently dropped), overflow keeps local entries, health check returns local status with `cold.error`.
- **Zero core modification** — designed as a drop-in companion; your agent's built-in memory tools keep working.

## Architecture

```
┌─────────────────────────────── Mac / local ──────────────────────────────┐
│  LLM agent (e.g. Hermes)                                                 │
│    │  MCP client                                                         │
│    ▼                                                                     │
│  MemoryCore MCP server                                                   │
│    ├─ local_store.py        hot tier: MEMORY.md / USER.md (chars-based)  │
│    ├─ classifier.py         cold/hot/stale routing rules                 │
│    ├─ overflow.py           six-step overflow                            │
│    ├─ maintenance.py        cold-tier governance                         │
│    └─ cold_store_client.py  →  LocalBackend (SQLite, in-process)         │
│                               or RemoteBackend (MCP streamable-http)     │
└──────────────────────────────────────────────────────────────────────────┘
                     LocalBackend: mnemosyne-memory (in-process engine)
                     RemoteBackend: remote MCP memory service

Optional (Hermes Agent only): hermes-plugin/memorycore-prefetch
  ┌───────────────────────────────────────────────────────────────────────┐
  │ MemoryProvider plugin (single-model qwen3, enabled by default)        │
  │   system_prompt_block → static index (always active)                  │
  │   prefetch → ColdStoreClient.recall_results(top_k=20)                 │
  │            → dense ranking → session + hot-tier dedup → top-5         │
  │   Disable: MEMORYCORE_PREFETCH_ENABLED=0                              │
  └───────────────────────────────────────────────────────────────────────┘
```

## Quick Start

### Prerequisites

- **ollama** — embedding API (install: https://ollama.com)
- **qwen3-embedding:0.6b** — recommended embedding model (1024-dim)

```bash
# Install ollama (macOS/Linux)
curl -fsSL https://ollama.com/install.sh | sh

# Pull the embedding model
ollama pull qwen3-embedding:0.6b
```

### Install & run

```bash
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# That's it! MemoryCore runs with ollama for embeddings:
#   - Hot tier:  MEMORY.md / USER.md (default ~/.hermes/memories)
#   - Cold tier: SQLite via mnemosyne-memory (default ~/.memorycore/data/)
#   - Embedding: qwen3-embedding:0.6b via ollama (http://localhost:11434/v1)
python -m memorycore.server          # stdio transport (default)
```

**Data directory layout** (all under `~/.memorycore/`):

```
~/.memorycore/
├── data/          # SQLite database (MNEMOSYNE_DATA_DIR)
└── ...
```

Override with `MNEMOSYNE_DATA_DIR`.

### Model switching

Default embedding model is `qwen3-embedding:0.6b` (1024-dim). Use any
ollama model by setting environment variables:

```bash
export MEMORYCORE_EMBED_URL="http://localhost:11434/v1"
export MEMORYCORE_EMBED_MODEL="nomic-embed-text"   # or your preferred model
```

Or point at any OpenAI-compatible embedding API:

```bash
export MEMORYCORE_EMBED_URL="https://api.openai.com/v1"
export MEMORYCORE_EMBED_MODEL="text-embedding-3-small"
```

Register it in your MCP client (example for Hermes Agent `config.yaml`):

```yaml
mcp_servers:
  memorycore:
    command: python
    args: ["-m", "memorycore.server"]
```

### Remote mode (optional)

If you prefer a shared remote Mnemosyne MCP service instead of the local
engine, set `MEMORYCORE_COLD_BACKEND=remote`:

```bash
export MEMORYCORE_COLD_BACKEND=remote
export MNEMOSYNE_URL="http://your-memory-service:9000/mcp"
python -m memorycore.server
```

Exposed tools:

| Tool | Purpose |
|---|---|
| `memorycore_store_fact(content, importance, scope, target)` | Unified write entry: routes cold / hot / stale |
| `memorycore_recall(query, top_k)` | Actively recall cold-tier memories (read-only, complements per-turn prefetch) |
| `memorycore_trigger_overflow(target)` | Run six-step overflow, target ≤40% |
| `memorycore_run_cold_storage_maintenance()` | Cold-tier governance pass |
| `memorycore_get_memory_usage()` | Hot-tier usage + cold-tier stats + thresholds |

## Hermes integration — per-turn prefetch

The MCP server is client-agnostic. For **Hermes Agent** there is an
optional companion plugin that provides dual-channel cold-tier access:

### Dual-channel design

- **Static index channel (always active, zero overhead)** — a system
  prompt block listing available topics (configurable via
  `MEMORYCORE_INDEX_TOPICS`, comma-separated), with guidance to use
  `memorycore_recall(query)` for on-demand recall.
- **Per-turn prefetch channel (enabled by default)** — recalls the cold
  tier every turn, ranks by dense score, and injects the top-5 into
  context, so the agent "remembers" relevant content before it speaks.
  Set `MEMORYCORE_PREFETCH_ENABLED=0` to disable and use on-demand recall
  only.

### Prefetch pipeline

```
query → preprocess → cold-tier recall (20 candidates)
  → dense ranking (qwen3) → top-5
  → session dedup → hot-tier dedup → inject into context
```

MemoryCore uses a **single-model qwen3 architecture** (no reranker).
Dense scores from qwen3 are used for relative ranking within a batch;
there is no absolute threshold — the top-5 candidates by dense score
are always injected after dedup.

### Graceful degradation

When ollama is unreachable (not installed, not running, or model not
pulled), prefetch silently returns an empty string — the conversation
proceeds without injected memories, and no error is surfaced to the
user. A DEBUG-level log records the probe failure.

### Deployment (Hermes Agent)

```bash
# 1. install origin-memorycore (provides the cold tier + ColdStoreClient)
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# 2. put the plugin in Hermes' user plugin dir
mkdir -p ~/.hermes/plugins
cp -r hermes-plugin/memorycore-prefetch ~/.hermes/plugins/

# 3. activate (takes effect next session)
hermes config set memory.provider memorycore-prefetch
```

Three postures after deployment:

| Posture | Configuration | Behaviour |
|---|---|---|
| Default (recommended) | no extra config | static index + per-turn prefetch with top-5 injection |
| On-demand only | `MEMORYCORE_PREFETCH_ENABLED=0` | static index only, agent queries cold tier via `memorycore_recall` |
| Custom embedding | `MEMORYCORE_EMBED_URL` + `MEMORYCORE_EMBED_MODEL` | point at a different ollama instance or OpenAI-compatible API |

### Plugin configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORYCORE_PREFETCH_ENABLED` | *(unset)* | Set to `0` to disable per-turn prefetch |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama or OpenAI-compatible embedding API base URL |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding model name (1024-dim recommended) |
| `MEMORYCORE_INDEX_TOPICS` | *(unset)* | Comma-separated topics for the system prompt index block |

Requirements & notes:

- **Hermes-specific**: the plugin imports Hermes runtime modules
  (`agent.memory_provider`) and does **not** work as a standalone package —
  it is the Hermes integration side of MemoryCore. Full details:
  [hermes-plugin/memorycore-prefetch/README.md](hermes-plugin/memorycore-prefetch/README.md).
- Every recall keeps a 5s timeout; failures degrade silently to an empty
  injection and never block the conversation.

## Hot-Tier Governance

The hot tier (MEMORY.md / USER.md) is injected into the context every turn,
so it must stay small and current. MemoryCore layers three mechanisms on top
of the six-step overflow so historical records retire deterministically
instead of piling up:

### Hot-tier metadata aging

- Sidecar metadata: `MEMORY.meta.json` / `USER.meta.json` sit next to the
  .md files, keyed by the SHA-256 of the entry content. Atomic writes plus
  file locks keep them safe across processes; the §-delimited .md format is
  untouched, so host memory tools keep working unchanged.
- Every entry is typed `state` (historical decisions / status records) or
  `rule` (precepts / preferences):
  - `state`: retires to the cold tier 7 days after being written
    (configurable: `STATE_TTL_DAYS`)
  - `rule`: never retires by age; after 30 days without an update, long
    entries (>200 chars) become LLM-compression candidates
    (configurable: `RULE_COMPRESS_DAYS`). Rules also get a sustainable exit
    through the invalidation-signal ladder below — without ever
    mis-retiring an active preference.
- When an entry's content changes, its key changes — the next reconcile
  re-types the new content and garbage-collects orphaned keys.

### Dual write-entry governance

- `store_fact` write entry: content that looks like a completed
  decision/status record (a date plus completion markers such as
  拍板/已配置, with no behavior instructions) is routed straight to the
  cold tier — it never enters the hot tier.
- Plugin `on_memory_write` direct-write channel: after every built-in
  memory tool add/replace, the entry is typed immediately. `state` entries
  migrate to the cold tier in the background (dedup → cold write confirmed
  → removed from hot; on cold failure the entry stays with a state stamp
  as a 7-day backstop). This runs independent of usage thresholds. A single
  worker thread drains a bounded queue (size 128); when the queue is full
  the write is skipped and the next overflow reconcile stamps it as a
  backstop.

### Metadata-first overflow

Each overflow run first reconciles metadata (stamps untyped legacy entries,
garbage-collects orphans), then retires entries by metadata — keywords only
remain as the fallback for untyped entries. A sidecar failure degrades to
the keyword path and never blocks the overflow.

### Rule invalidation signals (tiered protection)

A hot tier made of pure `rule` entries has no exit by design ("never sink
a preference"), so short rules that are never edited would otherwise stay
forever and eventually fill the tier. MemoryCore closes that gap with a
**pressure ladder**: every overflow run measures the real usage (the
baseline) and opens deeper exits as pressure rises (the response). Five
observable signals decide *eligibility and ordering* — pressure decides
*whether to act*:

| Signal | What it observes | Action |
|---|---|---|
| S1 idle time | `updated_at` in the sidecar | eligibility gate for compression (30d) and stub-sink (45d) |
| S2 completion re-check | embedded date ≥ 60d + ≥ 2 completion markers + zero behavior words | a historical record mis-typed as `rule` is restamped `state` → normal 7-day TTL sink |
| S3 same-topic clustering | lexical similarity (+ optional embedding channel) | same-topic entries merge into one; merged long entries become compression candidates later |
| S4 topic activity | local query-activity log (prefetch/recall, rolling 45 days, optional) + LLM dormancy judge | dormant B-class rules under hard pressure: full text to the cold tier (confirmed first), a ≤40-char pointer stub stays hot |
| S5 cross-tier redundancy | cold-tier recall match | an equivalent cold copy already exists → drop the hot copy (zero information loss) |

**Tiered protection**: A-class meta-rules (behavior / interaction /
writing-style precepts), red-line rules and importance ≥ 0.9 entries never
take S2/S4/S5 — they only merge or compress. Stub pointers have a lifecycle
of their own (oldest-first GC under hard pressure; the cold tier is never
touched), so pointers cannot fill the tier a second time. Every exit is
*cold-write-first*: the local entry changes only after the cold tier
confirms, and any failure keeps the original. When a signal is unavailable
(no activity log, no LLM key), the ladder degrades to the previous
behaviour instead of guessing.

Constants (`memorycore/core/config.py`): `RULE_RETYPE_DAYS=60`,
`RULE_STUB_IDLE_DAYS=45`, `ACTIVITY_WINDOW_DAYS=30`, `MAX_STUB_PER_RUN=3`,
`STUB_MAX_CHARS=40`, `IMPORTANCE_PROTECT=0.9`.

### Health check: memorycore_memory_audit

A read-only tool listing every hot-tier entry with its type, age,
retirement plan and keep/sink classification — the observability anchor for
diagnosing an overflow that finds nothing to sink.

## Scale test & optimisation results

MemoryCore was stress-tested and recall-optimised at ten-thousand-entry
cold-tier scale (isolated test environment, zero contact with production
data, reproducible results).

**Write & capacity**

| Metric | Result |
|---|---|
| Write throughput | 10k entries in 467s, ≈21.4 entries/s (embedding-bound) |
| Database size | 300MB / 10k entries |
| Memory footprint | process RSS +19MB only, flat throughout — no leak signature |

**Query latency** — median 48ms at top_k=5; ten-thousand-entry scale
matches hundred-entry scale, no latency regression.

**Recall quality** — three probes:

1. **Exact match (self-recall)**: 20/20 hit top-1 — exact matching is intact.
2. **Noise rejection (unrelated queries)**: mean top-1 dense score 0.056,
   most return 0.0 — unrelated content almost never leaks into results.
3. **Short-query recall (before → after)** — the key optimisation outcome:

| Stage | Short-query hit rate |
|---|---|
| Before | 0/8 |
| After | 5/8 (62.5%) |

**What was optimised**: at high topic density, the fixed candidate
truncation `k=max(top_k, 20)` pushed detailed memories out of the candidate
pool, so short queries failed to recall them. The fix enlarges the
candidate truncation to `k=max(top_k*4, 300)` and expands candidates
internally at the recall entry point before truncating the return — every
recall channel (per-turn prefetch + on-demand recall) benefits from a
single fix. The fix is confined to the recall stage; ranking logic is
untouched, behaviour is predictable and reversible.

> Note: tests ran on a synthetic 10k-entry database (80 "golden" memories +
> 9920 filler memories in daily-log tone, same config as production);
> production data was untouched.

## Notes for sqlite-vec users

If you enable sqlite-vec vector indexing for the Mnemosyne cold tier, be aware
that `beam.py`'s `_wm_vec_search_sqlite` uses a raw similarity formula
`sim = 1 - distance / (2 * EMBEDDING_DIM)` that collapses float32 distances to
~1.0, making the dynamic threshold effectively useless (all results pass).

**Patch**: in the float32 branch, replace the formula with
`sim = 1 - d² / 2` — this gives the exact cosine similarity for normalised
vectors and restores correct threshold behaviour.

## Cold Store Contract

Any service that exposes these five MCP tools can act as the cold tier:

| Tool | Semantics |
|---|---|
| `remember(content, importance, scope)` | Store a memory, return `memory_id` |
| `recall(query, top_k)` | Semantic recall |
| `update(memory_id, content)` | Merge-update an existing memory |
| `forget(memory_id)` | Delete a memory |
| `stats()` | `total` + embedding integrity |

See [examples/cold-store-contract.md](examples/cold-store-contract.md) for the full contract and a reference client.

## Configuration

| Env var | Default | Meaning |
|---|---|---|
| `MEMORYCORE_COLD_BACKEND` | `local` | Cold-tier backend: `local` (in-process) or `remote` (MCP) |
| `MNEMOSYNE_URL` | *(empty)* | Cold-tier MCP endpoint (required for `remote` mode) |
| `MNEMOSYNE_DATA_DIR` | `~/.memorycore/data` | Local SQLite data directory |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama or OpenAI-compatible embedding API base URL |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding model name (1024-dim) |
| `MEMORY_DIR` | `~/.hermes/memories` | Hot-tier directory (`MEMORY.md` / `USER.md`) |
| `ACTIVITY_LOG_ENABLED` | `1` | Query-activity log for topic-activity signals; `0` disables the log and S4 stub-sink entirely |
| `MNEMOSYNE_TIMEOUT` | `10.0` | Cold-tier request timeout (remote mode, seconds) |

Capacity constants live in `memorycore/core/config.py` (`CHAR_LIMIT_*`, `SOFT_THRESHOLD`, `HARD_THRESHOLD`, `TARGET_RATIO`).

## How It Works

1. **Write** — `store_fact` classifies the content:
   - importance ≥ 0.8 or matches hot keywords (preferences / rules / corrections / red lines) → **hot**, kept local
   - stale markers (short entry, e.g. "已修复 / fixed") → **dropped** (not migrated)
   - anything else → **cold**, written directly to the remote service
2. **Overflow** — when hot usage passes the soft threshold, overflow migrates low-frequency entries to the cold tier; at the hard threshold it force-overflows until ≤ target. Order is always *write cold first, verify, then delete local* — nothing is lost if the cold tier fails.
3. **Maintenance** — a periodic pass over the cold tier merges duplicates, removes stale entries, resolves conflicts, and verifies embedding integrity.

## License

[MIT](LICENSE) © 2026 moonandecho

### Third-party licenses

- [mnemosyne-memory](https://github.com/mnemosyne-oss/mnemosyne) — MIT,
  by AxDSan. The in-process memory engine used by `LocalBackend`.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — MIT.
- [ollama](https://ollama.com) — MIT. Local embedding API server.
- [qwen3-embedding](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) — Apache-2.0,
  by Alibaba Cloud. Default embedding model (not bundled; pulled via ollama).
