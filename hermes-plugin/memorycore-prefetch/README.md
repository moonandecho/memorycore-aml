# memorycore-prefetch — Hermes Agent plugin (adaptive recall)

A thin [Hermes Agent](https://github.com/NousResearch/hermes-agent) memory
provider plugin. Every turn it recalls the cold tier (top-3) and injects the
best matches into the agent context — but only when they clear an
**adaptive semantic threshold** that tightens as the conversation context
fills up.

## Why it exists

MemoryCore itself is an MCP server: it serves tools, but an MCP server
cannot *push* context into a conversation. The Hermes `MemoryProvider`
interface is the official extension point for per-turn recall. This plugin
adds that channel without touching Hermes source — it registers through the
public `register_memory_provider` hook and ships **zero tools**.

## What the adaptive threshold does

```
threshold = max(ABS_FLOOR 0.45, rolling_baseline × coefficient)
coefficient = low water 0.80 / mid water 0.90 / high water 1.00
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
  file to reset to the initial value (`0.69`, measured from real top_k=3
  semantic queries — see `docs/ADAPTIVE_THRESHOLD.md` for the statistics).
- **Absolute floor 0.45** blocks noise: unrelated queries top out at 0.403,
  relevant ones start at 0.471 (measured separation band).

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
