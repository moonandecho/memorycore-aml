# memorycore-prefetch — Hermes Agent plugin (dual-channel recall)

> ⚠️ **EXPERIMENTAL — off by default.** Per-turn prefetch is provided for
> experimentation and small-scale use. The primary retrieval path is
> on-demand recall via the `memorycore_recall` tool, which avoids the
> noise problem entirely by letting the agent query with intent. See
> [../../docs/ADAPTIVE_THRESHOLD.md](../../docs/ADAPTIVE_THRESHOLD.md)
> for measurement data and the rationale for the default-off posture.

A thin [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory
provider plugin. It provides:

1. A **static cold-store index block** via `system_prompt_block()` (always
   active) that guides the model to use on-demand recall
   (`memorycore_recall` tool). Topics are configurable via the
   `MEMORYCORE_INDEX_TOPICS` environment variable (comma-separated).
2. **Per-turn semantic prefetch** (opt-in via `MEMORYCORE_PREFETCH_ENABLED=1`):
   recalls the cold tier (top-10), optionally re-ranks via a cross-encoder,
   and injects the top-3 into context.
3. **on_memory_write auto-overflow**: after every built-in memory tool write,
   checks hot-tier usage and triggers overflow at thresholds.

## Design: dual-channel recall

When prefetch is **disabled** (default), the plugin injects only a static
index block via `system_prompt_block()`. The agent uses `memorycore_recall`
on demand — zero per-turn overhead, no noise problem.

When prefetch is **enabled** (`MEMORYCORE_PREFETCH_ENABLED=1`), both channels
coexist:
- **Static index** (always present) — topic overview + recall guidance.
- **Active injection** (every turn) — top-3 cold-tier memories, filtered
  through the pipeline below.

## Prefetch pipeline (when enabled)

```
query → preprocess → cold-tier recall (10 candidates)
  → reranker refinement (if MEMORYCORE_RERANK_URL configured)
     or dense-score top-1 fallback (if no reranker)
  → session dedup → hot-tier dedup → inject top-3
```

- **Reranker (optional)**: when `MEMORYCORE_RERANK_URL` is set to a
  cross-encoder endpoint (e.g. `http://your-reranker-host:8899/rerank`), the plugin
  sends a POST with `{"query": q, "documents": docs}` and filters by
  absolute threshold (-2.0), then injects the top-3 by rerank_score.
- **Fallback (default)**: when no reranker is configured, the plugin
  injects only the single best dense-score match if it clears
  `max(0.45, baseline × 0.90)`. This conservative fallback prioritises
  silence over noise.

## Dynamic baseline threshold

```
threshold = max(0.45, rolling_baseline × 0.90)
```

- **Fixed coefficient 0.90** — no water-level bands (removed in 2026-08-05).
- **Baseline self-evolves**: every recall records the batch-max `dense_score`
  into a 200-sample rolling window; the median is recomputed every 50 samples
  and atomically persisted to `baseline.json` next to this file. Delete that
  file to reset to the initial value (`0.70`).
- **Absolute floor 0.45** acts as a lower guardrail. At scale (1000+ entries)
  it is not a noise barrier — the reranker handles noise separation when
  configured; the fallback path tightens to top-1.

## on_memory_write auto-overflow

After every built-in memory tool write (add/replace), the plugin checks
hot-tier usage in real time and triggers overflow at ≥80% (hard) or ≥60%
(soft, with 5% hysteresis). Overflow runs in a background thread so it
never blocks the tool return. This covers all write paths (built-in memory
tool, external gateways, other sessions).

Cold tier access goes through the same `ColdStoreClient` factory as the
server: `MEMORYCORE_COLD_BACKEND=local` (default, in-process SQLite) or
`remote` (any MCP memory service via `MNEMOSYNE_URL`).

## Install & activate (Hermes Agent)

```bash
# 1. install origin-memorycore (provides the cold tier + ColdStoreClient)
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# 2. put the plugin in Hermes' user plugin dir
mkdir -p ~/.hermes/plugins
cp -r hermes-plugin/memorycore-prefetch ~/.hermes/plugins/

# 3. activate (takes effect next session)
hermes config set memory.provider memorycore-prefetch
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `MEMORYCORE_PREFETCH_ENABLED` | *(unset)* | Set to `1` to enable per-turn prefetch (EXPERIMENTAL) |
| `MEMORYCORE_RERANK_URL` | *(unset)* | Full cross-encoder endpoint URL (e.g. `http://your-reranker-host:8899/rerank`). When unset, falls back to dense-score top-1 |
| `MEMORYCORE_INDEX_TOPICS` | *(unset)* | Comma-separated topics for the system prompt index block |

The adaptive threshold baseline is persisted to `baseline.json` next to
this file. Delete it to reset to the initial value (`0.70`).

## Requirements & notes

- **Hermes-specific**: this plugin imports Hermes runtime modules
  (`agent.memory_provider`). It does **not** work as a standalone package —
  it is the Hermes integration side of MemoryCore.
- Every recall keeps a 5s timeout; failures degrade silently to an empty
  injection and never block the conversation.
- `baseline.json` is written only after 50 new samples (roughly 50 turns);
  until then the in-memory initial value is used.
- The reranker call uses a 3s timeout; timeouts or failures trigger the
  same fallback as an unconfigured URL.
