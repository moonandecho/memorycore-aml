# Recall Eval — P0 格式规范与 runner

本轮只交付**评估 runner + 标签格式 + 纯合成样例**；不生成真实标签，
不产出真实质量数字。holdout 内容不进入工作副本；评估方拿到 sealed 标签后
只需替换文件、复跑同一 runner。

## 目录

| 文件 | 用途 | 可见性 |
|---|---|---|
| `eval/eval_dev.labels.jsonl` | 纯合成调试样例（不含真实记忆内容） | 改动方可见 |
| `eval/eval_holdout.labels.jsonl` | sealed 评估集占位；本轮为空文件 | 仅评估方持有 |
| `eval/manifest.json` | edition 元数据：`frozen_at / labels_sha256 / labeler / label_version / embed_model / buckets` | 可见（不含标签） |
| `tools/run_recall_eval.py` | 读 labels、跑只读召回、**只输出聚合指标** | 可见 |

`manifest.json` 额外记录 `dev_labels_file / dev_labels_sha256 / holdout_sealed`
便于复现；`labels_sha256` 对应正式评估文件，`dev_labels_sha256` 对应合成调试
样例。当前 holdout 为空占位，其 sha256 为空文件真实摘要
`e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`。
替换正式 holdout 后须同步重算 `labels_sha256`。

## 标签行格式 (JSONL，每行一个 object)

```json
{"qid":"synth-entity-001","query":"合成样例：代号 ATLAS-1 对应哪个演示实体？","targets":["cold_synth_entity_01"],"supportable":true,"bucket":"entity","anchor_sha256":"<64-hex>"}
```

字段：
- `qid`：评估集内唯一字符串（runner 只用于去重校验，不输出）。
- `query`：送入只读召回的自然语言 query。
- `targets`：应命中的冷层 `cold_id` 列表；negative_control 为 `[]`。
- `supportable`：positive 样本为 `true`；negative_control 为 `false`。
- `bucket`：`entity | date | paraphrase | multihop | negative_control`。
- `anchor_sha256`：标签锚点摘要，供评估方审计，runner 不解释其内容。

语义校验（非法即 exit 2，不静默放行）：
- `negative_control` 必须 `supportable=false` 且 `targets=[]`；
- 其余 bucket 必须 `supportable=true` 且 `targets` 非空；
- `top_k >= 1`；manifest 中与实际 labels 文件对应的 sha256 必须一致。

## Runner 输出口径

正式评估（需要冷层可达）：

```bash
.venv/bin/python tools/run_recall_eval.py \
    --labels eval/eval_holdout.labels.jsonl \
    --manifest eval/manifest.json --recall-source cold
```

离线只证明输出格式、不产生真实数字（`--recall-source empty`）：

```bash
.venv/bin/python tools/run_recall_eval.py \
    --labels eval/eval_dev.labels.jsonl \
    --manifest eval/manifest.json --recall-source empty
```

输出 JSON 仅含：
- 顶层 `edition_hash` 与 `sample_count`（edition 的显式别名）；
- `edition`：`labels_sha256`、`sample_count`、`label_version`、`embed_model`、`buckets`；
- `metrics`：`hit@1`、`hit@3`、`hit@5`、`hit@k`、`mrr`、`negative_control_FP_rate`、
  `supportable_n`、`negative_control_n`、`errors`；
- `by_bucket`：按 bucket 的 `n / hit@k / mrr / negative_control_FP_rate` 聚合。

指标语义：
- positive/supportable：`targets` 任一 id 首次出现在返回序列第 k 名 → `hit@k=1`；
  多目标样本取首次命中名次计算 MRR。
- `negative_control_FP_rate`：negative_control 中 `top_k` 返回了**任意**结果的比例。
- 召回调用异常按 miss 计，只累计 `errors` 数量。

## 隐私与一票否决

- runner **绝不输出** `qid`、`query` 文本、`targets` id、逐题名次或召回 id 列表。
- 召回默认 `bump=False`（只读），不写冷层、不触碰热层。
- 验收若在 runner 输出中发现 `targets` 内容或逐题明细，本轮评估交付不通过。
