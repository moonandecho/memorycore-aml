# origin-memorycore

**MCP memory tiering server for LLM agents — hot local tier + cold tier (local SQLite or remote service) with automatic overflow.**

MemoryCore gives your LLM agent a two-tier memory system: frequently-used behavioral knowledge (preferences, rules, corrections) stays in a fast local file tier, while low-frequency facts are automatically migrated to a cold tier — an in-process SQLite engine by default, or a remote memory service if you configure one. No more one-blob memory files that grow forever or lose important preferences to truncation.

Built on the [MCP](https://modelcontextprotocol.io) (Model Context Protocol) `streamable-http` / stdio standard. Works with any MCP client, tested with [Hermes Agent](https://github.com/NousResearch/hermes-agent).

---

## Features

- **Cold/hot routing** — every write is classified: high-importance or preference-like → hot (local); low-frequency fact → cold (remote); stale status record → dropped.
- **Six-step overflow** — capacity baseline → dedup → stale filtering → merge → safe write (cold first, then delete local) → verification.
- **Cold-tier maintenance** — dedup merge, stale cleanup, conflict resolution, embedding integrity check.
- **Capacity control** — soft threshold (overflow once before writing) / hard threshold (force overflow) / target ratio. Defaults: 60% / 80% / 40% of a 5000-char limit.
- **Triple governance (v3)** — three layers of protection for cold-tier data integrity:
  - **Cold-write dedup**: before writing to the cold tier, a semantic recall + LLM judge checks for duplicates and updates existing entries instead of creating redundant ones.
  - **Capacity hard gate**: cold tier enforces a soft limit (6000 entries, triggers one maintenance pass) and a hard limit (10000 entries, forces maintenance loops) — prevents unbounded growth.
  - **Recycle bin** (`trash_store.py`): deleted cold-tier entries are moved to `~/.memorycore/trash.json` with a 30-day expiry. Recalling a trashed entry with fresh semantic evidence restores it ("recall to revive").
- **Adaptive recall threshold** — the optional Hermes plugin (`hermes-plugin/`) recalls the cold tier every turn, but only injects matches that clear a semantic threshold that adapts to context pressure: low water → more recall, high water (near compression) → less, saving tokens. The threshold baseline self-evolves from your real recall scores (statistics in [docs/ADAPTIVE_THRESHOLD.md](docs/ADAPTIVE_THRESHOLD.md)).
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
  │ MemoryProvider plugin (per-turn cold recall, adaptive threshold)      │
  │   sync_turn → water level → coefficient → threshold                   │
  │   prefetch  → ColdStoreClient.recall_results(top_k=3) → filtered      │
  └───────────────────────────────────────────────────────────────────────┘
```

## Quick Start (single machine — zero external services)

```bash
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# That's it! MemoryCore runs entirely locally:
#   - Hot tier:  MEMORY.md / USER.md (default ~/.hermes/memories)
#   - Cold tier: SQLite via mnemosyne-memory (default ~/.memorycore/data/)
#   - Embedding: BAAI/bge-small-zh-v1.5 (Chinese) bundled — no download
python -m memorycore.server          # stdio transport (default)
```

Two embedding models are **shipped inside the package** (Chinese + English).
On first run MemoryCore auto-deploys them from the package into
`~/.memorycore/fastembed/` (one-time copy, ~155 MB total).  No network access,
no huggingface.co, no GCS mirror — zero download, ever.

**Data directory layout** (all under `~/.memorycore/`):

```
~/.memorycore/
├── data/          # SQLite database (MNEMOSYNE_DATA_DIR)
└── fastembed/     # ONNX embedding models (auto-deployed on first use)
```

Override with `MNEMOSYNE_DATA_DIR` or `MNEMOSYNE_FASTEMBED_CACHE_DIR`.

### Language switching

Default is Chinese (`BAAI/bge-small-zh-v1.5`, 512-dim).  Switch to
English (384-dim) with an env var — the model is already on disk:

```bash
export MNEMOSYNE_EMBEDDING_MODEL="BAAI/bge-small-en-v1.5"
python -m memorycore.server
```

For other languages or stronger multilingual recall, point at any
OpenAI-compatible embedding API:

```bash
export MNEMOSYNE_EMBEDDING_API_URL="http://localhost:11434/v1"
export MNEMOSYNE_EMBEDDING_MODEL="bge-m3"
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

## Hermes integration (optional): adaptive per-turn prefetch

The MCP server is client-agnostic. For **Hermes Agent** there is an
optional companion plugin that makes the cold tier participate in every
conversation turn:

- Every turn it recalls the cold tier (top-3) and injects matches into
  context — but only those clearing an **adaptive semantic threshold**:
  `max(0.45, rolling_baseline × coefficient)`, where the coefficient
  tightens with context water level (low 0.80 / mid 0.90 / high 1.00).
- Baseline self-evolves: rolling median of your real recall scores,
  persisted to `baseline.json`; delete it to reset.
- Design & statistics: [docs/ADAPTIVE_THRESHOLD.md](docs/ADAPTIVE_THRESHOLD.md).
- Install/activate/requirements: [hermes-plugin/memorycore-prefetch/README.md](hermes-plugin/memorycore-prefetch/README.md).

```bash
cp -r hermes-plugin/memorycore-prefetch ~/.hermes/plugins/
hermes config set memory.provider memorycore-prefetch   # next session
```

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
| `MNEMOSYNE_FASTEMBED_CACHE_DIR` | `~/.memorycore/fastembed` | Local ONNX embedding model cache |
| `MNEMOSYNE_EMBEDDING_MODEL` | `BAAI/bge-small-zh-v1.5` | Local embedding model (512-dim, Chinese, MIT) |
| `MNEMOSYNE_EMBEDDING_API_URL` | *(empty)* | External embedding API (unset = bundled model, zero network) |
| `MEMORY_DIR` | `~/.hermes/memories` | Hot-tier directory (`MEMORY.md` / `USER.md`) |
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
- [fastembed](https://github.com/qdrant/fastembed) — Apache-2.0,
  by Qdrant. ONNX embedding runtime that loads the bundled models.
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — MIT.
- [BAAI/bge-small-zh-v1.5](https://huggingface.co/BAAI/bge-small-zh-v1.5) — MIT,
  by Beijing Academy of Artificial Intelligence. Default Chinese embedding model.
- [BAAI/bge-small-en-v1.5](https://huggingface.co/BAAI/bge-small-en-v1.5) — MIT,
  by Beijing Academy of Artificial Intelligence. Bundled English embedding model.

The bundled ONNX model files carry their own license notice; see
[memorycore/assets/fastembed-cache/THIRD_PARTY_MODELS.md](memorycore/assets/fastembed-cache/THIRD_PARTY_MODELS.md).
