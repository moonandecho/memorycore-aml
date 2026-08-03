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
coefficient = low water 0.90 / mid water 0.90 / high water 1.00
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
- Initial value `0.70`, delete `baseline.json` → reset to `0.70`.

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

### Coefficients (calibration) ⚠️ 小库校准值, 大库已失效, 见下节

| Band   | Coef | Effect (measured)                    |
|--------|------|--------------------------------------|
| low    | 0.80 | natural-language top-1 passes, some top-3 → more recall |
| mid    | 0.90 | only strong top-1 → normal           |
| high   | 1.00 | almost only self-recall-level matches → minimal recall |

⚠️ 该结论基于 37 条小库实测（噪声上界 0.403 < 0.45）；2026-08-04 大库实测 (N=1000/3000) 显示噪声上界升至 0.677/0.731，0.45 底线漏放 87–89%，详见 [大库实测与 reranker 二阶段方案](#大库实测与-reranker-二阶段方案-2026-08-04)。

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

## 大库实测与 reranker 二阶段方案 (2026-08-04)

### 背景

以下数据来自 2026-08-04 合成大库实测，目的是检验自适应阈值的核心假设——"固定绝对底线 0.45 拦噪声"——在冷库规模增长后是否仍然成立。测试在 37 条、1000 条、3000 条三个规模点进行了同构测量。

### 底线失守：噪声上界随库规模上涨

| 冷库规模 N | 噪声 batch-max 上界 | 0.45 底线噪声漏放率 |
|-----------|-------------------|-------------------|
| 37 (基线) | 0.403 | 0% (噪声 n=6, 上界 0.403 < 0.45) |
| 1000 | 0.677 | 87% |
| 3000 | 0.731 | 89% |

**结论**: 0.45 作为绝对底线在 N=1000 时已基本失效——噪声批次最大值从 0.403 上涨至 0.677，远高于 0.45 底线，漏放率达 87%。N=3000 时进一步恶化。根本原因：随着向量空间密度增加，随机噪声与任意查询的余弦相似度上界系统性抬升；固定阈值无法补偿这一效应。

### 四个检索侧信号被数据否决

除绝对分数阈值外，以下三个额外信号也被验证无法可靠区隔噪声与相关召回（各 1–2 句实测数据）：

1. **字面重合 (bigram overlap)**: scene（真实 prefetch 形态的模糊回忆句）bigram 重合中位仅 0.03，与噪声上界 (0.067) 重叠；gate ≥ 0.08 虽清光噪声但 scene 只剩 14% — 不是"噪声偶然高重合"，而是"scene 天然低重合导致门禁误杀"。
2. **top1–top2 分差 (gap12)**: scene gap12 中位 0.0096 vs noise 0.0047，分布重叠；gate ≥ 0.02 噪声仍剩 8% 但 scene 只剩 24.5%（误杀 75%）。
3. **kNN 相对距离**: scene 相对距离中位 3.0 vs noise 4.0，分布重叠；gate ≥ 2.0 语义误杀 67.5%。

### 交叉编码器 reranker 验证（调研记录; 生产版已退役）

> ⚠️ 2026-08-04 用户拍板: reranker 路线不再启用（生产版与开源版均不采用），
> llama-rerank.service 已退役归档。本节为当时调研与验证记录, 保留供参考。

生产版曾采用 **reranker 二阶段**方案替代固定阈值（2026-08-04 退役）:

- **第一阶段**: dense 召回 (top_k=3, bge-m3 1024-dim)
- **第二阶段**: bge-reranker-v2-m3 交叉编码器精判（通过 llama.cpp `--reranking` 模式；注意 Ollama 官方不支持 rerank API）
- **分离效果** (真实 bge-m3 数据): "必然相关" vs "必然无关" 分离带 12.5 分，零重叠
  - 正例 self 对（同一记忆 self-recall）：中位 +10.53
  - 负例跨主题对（无关查询 × 无关记忆）：中位 −9.43

### 定位声明

- **开源版** (`origin-memorycore`): 保持轻量——推荐用法为 `memorycore_recall` 按需召回，不引入额外服务或依赖。per-turn prefetch 插件（`hermes-plugin/memorycore-prefetch/`）标记为 **Experimental**，已知局限见上。
- **生产版**（私有部署, 2026-08-04 已退役）: 曾采用 reranker 二阶段 (dense 召回 + bge-reranker-v2-m3 精判)，阈值 -2.0（真实数据校准; 真相关 ≥ +0.4, 噪声 ≤ -2.9）。现与开源版一致: 按需召回为主, prefetch 降级实验特性。

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
