# memorycore-prefetch — Hermes Agent plugin (on-demand recall)

> ⚠️ **EXPERIMENTAL — known limitations at scale.** This plugin uses an
> adaptive threshold with a fixed absolute floor (0.45).  Measurements on
> synthetic cold tiers at 1000–3000 entries show the noise ceiling rises
> to 0.73, admitting 87–89% of noise through the 0.45 floor.  For
> medium-to-large cold tiers, prefer the reranker two-stage solution
> (dense recall + cross-encoder such as bge-reranker-v2-m3).  See
> [../../docs/ADAPTIVE_THRESHOLD.md § 大库实测](../../docs/ADAPTIVE_THRESHOLD.md#大库实测与-reranker-二阶段方案-2026-08-04)
> for the full measurements and the production solution.

A thin [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory
provider plugin. It provides a **static cold-store index block** via
`system_prompt_block()` that guides the model to use on-demand recall
(`memorycore_recall` tool). Per-turn semantic prefetch is **off by default**
(degraded to an experimental feature — see below).

## Design: on-demand recall with index guidance

The plugin injects a short index block into the system prompt once per
session. The block lists available topics (configurable via the
`MEMORYCORE_INDEX_TOPICS` environment variable, comma-separated) and
instructs the model to use `memorycore_recall(query)` when it needs
historical details. This is the **primary retrieval path**.

This design follows the industry consensus (Mem0, Letta, Hindsight,
OpenViking): on-demand retrieval is the mature paradigm for memory
augmentation. Per-turn automatic injection adds token overhead without
proportional benefit and introduces threshold-tuning complexity that
does not scale to large cold tiers.

## Prefetch (experimental, off by default)

The original per-turn semantic recall (`prefetch` / `queue_prefetch`) is
preserved in the codebase but **returns empty by default**. `prefetch()`
returns `""` and `queue_prefetch()` is a no-op. All internal recall
infrastructure (`_recall_sync`, `_recall_bg`, adaptive threshold, dynamic
water level, baseline self-evolution) remains intact and can be re-enabled
via a config flag. See git history for the full original implementation.

## Adaptive threshold (preserved, not active by default)

The adaptive threshold infrastructure is fully preserved but not exercised
in the default code path (since `prefetch` returns `""`). When re-enabled,
the behaviour is:

```
threshold = max(ABS_FLOOR 0.45, rolling_baseline × coefficient)
coefficient = low water 0.90 / mid water 0.90 / high water 1.00
```

- **Water level** is estimated from the full conversation history via
  `estimate_messages_tokens_rough` after each turn (`sync_turn`):
  `<50K tokens → low`, `50K–150K → mid`, `>150K → high`, plus a
  compression-point correction for smaller context windows.
- **Low water → lower threshold → more recall injected** (context is cheap).
- **High water → higher threshold → less injected** (saves tokens near
  compression).
- **Baseline self-evolves**: every recall records the batch-max `dense_score`
  into a 200-sample rolling window; the median is recomputed every 50 samples
  and atomically persisted to `baseline.json` next to this file. Delete that
  file to reset to the initial value (`0.70`, measured from real top_k=3
  semantic queries — see `docs/ADAPTIVE_THRESHOLD.md` for the statistics).
- **Absolute floor 0.45** acts as a lower guardrail: unrelated queries
  top out at ~0.40, relevant ones start at ~0.47 (measured separation band
  on a small cold tier).  At scale (1000+ entries) this floor is
  insufficient — see the Known Limitations section above.

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

- `MEMORYCORE_INDEX_TOPICS`: comma-separated list of topics to display in
  the system prompt index block. When unset, only a generic guidance line
  is shown.
- The adaptive threshold baseline is persisted to `baseline.json` next to
  this file. Delete it to reset to the initial value (`0.70`).

## Requirements & notes

- **Hermes-specific**: this plugin imports Hermes runtime modules
  (`agent.memory_provider`, `agent.model_metadata`, `agent.models_dev`) and
  reads `~/.hermes/config.yaml` for the model context window. It does **not**
  work as a standalone package — it is the Hermes integration side of
  MemoryCore.
- Every recall keeps a 5s (sync) / 8s (background) timeout; failures degrade
  silently to an empty injection and never block the conversation.
- `baseline.json` is written only after 50 new samples (roughly 50 turns);
  until then the in-memory initial value is used.
