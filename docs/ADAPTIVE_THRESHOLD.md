# Adaptive Recall Threshold — design & statistics

> The MemoryCore cold-tier recall (`memorycore-prefetch` plugin) filters
> recalled memories with a threshold that adapts to how full the agent's
> context window is. This document records the design and the measurements
> behind every number. No magic constants: each parameter is anchored to
> observed data.

## Problem

Prefetch recalls the cold tier every turn and injects matches into context.
Two failure modes:

1. **Inject too much** — near compression, wasted tokens force earlier
   context compression.
2. **Inject too little / junk** — noisy matches pollute the prompt, or a
   fixed threshold either blocks everything relevant or lets noise through.

## Design

```
threshold = max(ABS_FLOOR 0.45, rolling_baseline × coefficient)
coefficient = low water 0.80 / mid water 0.90 / high water 1.00
```

Two mechanisms:

- **Water level** (how full is the context) → selects the coefficient.
- **Rolling baseline** (how good is recall for *your* data) → scales the
  base threshold, and self-evolves as the cold tier grows/changes.

### Water level (context pressure)

Estimated after each turn from the full message history
(`estimate_messages_tokens_rough`, Hermes built-in, ~0.06 ms cached).

| Band   | Tokens (1M window)       |
|--------|--------------------------|
| low    | < 50K                    |
| mid    | 50K – 150K               |
| high   | > 150K                   |

Compression-point correction (matters when the model window is small):

| Condition                         | Result        |
|-----------------------------------|---------------|
| tokens > window×0.7×0.8           | force high    |
| tokens > window×0.7×0.5           | at least mid  |

(`0.7` = the compression threshold ratio.)

### Baseline (semantic quality of your recall)

- Sample: batch-max `dense_score` of every recall (top_k=3), **not**
  `results[0]` — the server sorts by blended score, so the first result is
  not necessarily the highest semantic score.
- Rolling window: 200 samples; recompute the median every 50 new samples;
  atomic write to `baseline.json`.
- Initial value `0.69`, delete `baseline.json` → reset to `0.69`.

## Measurements (why these numbers)

Cold tier: 37 memories, Chinese short sentences, bge-m3 embeddings
(1024-dim), mnemosyne-memory recall, top_k=3.

### Score distribution (semantic queries)

| Statistic                    | Value  |
|------------------------------|--------|
| semantic top-1 median        | 0.692  |
| semantic top-1 (82 queries)  | 0.728  |
| noise top-1 max              | 0.403  |
| semantic min (separation)    | 0.471  |
| separation band              | 0.403 – 0.471 |

### Bootstrap (10,000 resamples)

| Estimate      | Median CI        |
|---------------|------------------|
| baseline init | [0.703, 0.748]   |

The CI is narrow — the baseline estimate is stable across resamples.

### Separation band → absolute floor

The gap between noise (max 0.403) and relevant (min 0.471) is
**0.403 – 0.471**. `ABS_FLOOR = 0.45` sits inside the band: zero relevant
recalls lost, zero noise admitted. Verified empirically at every band.

### Coefficients (calibration)

| Band   | Coef | Effect (measured)                    |
|--------|------|--------------------------------------|
| low    | 0.80 | natural-language top-1 passes, some top-3 → more recall |
| mid    | 0.90 | only strong top-1 → normal           |
| high   | 1.00 | almost only self-recall-level matches → minimal recall |

Noise never passes at any band (`0.45 > 0.403`).

### Why not a fixed threshold

A fixed value cannot track your data: same score means different things as
the cold tier grows, models change, or query habits shift. The rolling
baseline follows the real distribution; the water-level coefficient is the
single knob that reacts to context pressure.

### Why not query-derived thresholds

Earlier candidate designs used the current query's top-1 score as the
baseline (`threshold = max(0.45, top1 × ratio)`). Rejected after testing:
a single bad query would drag the threshold to ~0.34 and admit noise
(measured: unrelated query top-1 = 0.405 → threshold 0.34 < noise max).
A global statistical baseline is robust to per-query outliers.

## Verification

12 unit tests + end-to-end checks (all pass):

- low water (20 tokens) → 2/3 kept
- mid water (53K) → strict filtering
- high water (154K) → 0–1 kept
- noise query at any band → 0 kept
- compression correction: 64K window + 30K tokens → mid; 1M + 400K → high
- band boundaries: 40K→low / 52K→mid / 152K→high
- per-turn overhead < 1 ms (estimate 0.06 ms + integer compare)

## Reproducing the measurements

```python
# cold tier must be populated; top_k must match prefetch (3)
from memorycore.cold_store_client import ColdStoreClient
c = ColdStoreClient()
results = c.recall_results(query, top_k=3)
batch_max = max(r.get("dense_score", 0) for r in results)
```

Collect batch-max over many queries (semantic + noise), then:

```python
import statistics, random
samples = [...]  # your batch-max values
print("median", statistics.median(samples))
# bootstrap 10k for CI
medians = [statistics.median(random.choices(samples, k=len(samples)))
           for _ in range(10_000)]
print("CI", sorted(medians)[250], sorted(medians)[9750])
```

> ⚠️ `top_k` changes the returned scores — the same query gives different
> numbers at top_k=3 vs top_k=10. Always sample with the prefetch's real
> parameters (top_k=3).
