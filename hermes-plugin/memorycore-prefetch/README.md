# memorycore-prefetch — Hermes Agent plugin (single-model qwen3)

A thin [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory
provider plugin. It provides:

1. A **static cold-store index block** via `system_prompt_block()` (always
   active) that guides the model to use on-demand recall.
2. **Per-turn semantic prefetch** (enabled by default): recalls the cold
   tier (top-20), ranks by dense score, and injects the top-5 into context.
   Set `MEMORYCORE_PREFETCH_ENABLED=0` to disable.
3. **on_memory_write auto-overflow**: after every built-in memory tool write,
   checks hot-tier usage and triggers overflow at thresholds.

## Prerequisites

- **ollama** running with `qwen3-embedding:0.6b` pulled (or configure
  `MEMORYCORE_EMBED_URL` / `MEMORYCORE_EMBED_MODEL` for a compatible API).

When ollama is unreachable, prefetch silently returns empty — no errors.

## Design: dual-channel recall

- **Static index** (always present) — topic overview + recall guidance.
- **Active injection** (every turn, default on) — top-5 cold-tier memories
  ranked by qwen3 dense score, session-deduped, hot-tier-deduped.

## Prefetch pipeline

```
query → preprocess → cold-tier recall (20 candidates)
  → dense ranking (qwen3) → top-5
  → session dedup → hot-tier dedup → inject into context
```

Single-model qwen3 architecture — no reranker, no bge-m3.

## Install & activate (Hermes Agent)

```bash
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"
mkdir -p ~/.hermes/plugins
cp -r hermes-plugin/memorycore-prefetch ~/.hermes/plugins/
hermes config set memory.provider memorycore-prefetch
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORYCORE_PREFETCH_ENABLED` | *(unset)* | Set to `0` to disable per-turn prefetch |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama / OpenAI-compatible embedding API base URL |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding model name (1024-dim) |
| `MEMORYCORE_INDEX_TOPICS` | *(unset)* | Comma-separated topics for the system prompt index block |

The adaptive threshold baseline is persisted to `baseline.json` next to
this file. Delete it to reset to the initial value.

## Requirements & notes

- **Hermes-specific**: imports Hermes runtime modules (`agent.memory_provider`).
- Every recall keeps a 5s timeout; failures degrade silently to empty injection.
- `baseline.json` is written after ~50 turns of samples.
