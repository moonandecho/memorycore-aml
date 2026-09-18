# mnemosyne-recall-readonly.patch

## 1. 改了什么，为什么

### 改了什么
让本地 `mnemosyne-memory==3.15.1` 的召回接口接受并尊重
`bump_recalled: bool = True`。只读调用显式传
`bump_recalled=False` 时，召回不再执行
`UPDATE ... SET recall_count = recall_count + 1, last_recalled = ?`，
返回集合、顺序、dense/keyword/fts 分数、过滤逻辑均不变。

改动的函数/文件：

| 文件 | 函数 | 改动 |
|---|---|---|
| `mnemosyne/core/memory.py` | `Mnemosyne.recall` | 新增 keyword-only 参数 `bump_recalled=True`，向 `BeamMemory.recall` / `recall_enhanced` 透传 |
| `mnemosyne/core/memory.py` | 模块级 `recall` | 新增 `bump_recalled=True` 并透传 |
| `mnemosyne/core/beam.py` | `BeamMemory.recall` | 新增 `bump_recalled=True`，仅当为 `True` 时执行 linear 路径的 recall tracking UPDATE；polyphonic 路径透传 |
| `mnemosyne/core/beam.py` | `_recall_polyphonic` | 新增 `bump_recalled=True`，为 `False` 时跳过 polyphonic 路径的 recall tracking UPDATE |

默认值保持 `True`，即默认行为仍是原来的“召回会刷新
`last_recalled` / 递增 `recall_count`”；只有显式调用者选择只读时才关闭写回。

### 为什么
参赛版 `memorycore/cold_store_client.py:262` 为治理/枚举路径传入
`bump_recalled=False`。该版本 mnemosyne 的调用链
（`LocalBackend.recall` → `Mnemosyne.recall` → `BeamMemory.recall`）
原先不认识这个参数，代码落进 `except TypeError` 降级分支，退回默认召回，
从而把 `last_recalled` 刷新并让 `recall_count +1`。同一 query 在
Streaming 场景中重复检索时，旧记忆的衰减系数被第一次查询刷新，
导致后续结果漂移。

## 2. 如何对干净 mnemosyne 3.15.1 应用

在包含 `mnemosyne/` 包的 `site-packages` 目录（即 `mnemosyne/` 的父目录）执行：

```bash
patch -p1 -d /path/to/site-packages < patches/mnemosyne-recall-readonly.patch
```

本仓库的参赛版 venv 对应目录是：

```text
/home/echo/D/memorycore-aml/.venv/lib/python3.12/site-packages/
```

补丁头为 `a/mnemosyne/...` / `b/mnemosyne/...`，所以 `-p1` 后正好落到
`mnemosyne/core/memory.py` 与 `mnemosyne/core/beam.py`。

## 3. 适用前提

- 修复只打在参赛版仓库自己的 venv 库副本：
  `/home/echo/D/memorycore-aml/.venv/lib/python3.12/site-packages/mnemosyne/`。
- 生产 mnemosyne 服务使用它自己的 venv（`mnemosyne-venv`），本补丁不改变、
  也无意触碰 `/opt/mcp-servers/**`。
- 这是参赛版一次性项目；本补丁不计划移植回开源版。
- 默认行为不改变：未传 `bump_recalled=False` 的调用仍照旧刷新
  `last_recalled` / `recall_count`。

## 4. 如何验证生效

```bash
cd /home/echo/D/memorycore-aml
TMPDIR=/tmp .venv/bin/python -m pytest tests/test_fix3c_recall_readonly.py -q
```

两条回归用例分别验证：

1. `POST /search` 前后 SQLite 的 `working_memory.last_recalled` /
   `recall_count` 逐字节不变；
2. 同一 query 连查 5 次，返回 id 序列、顺序、score 完全一致，包括构造
   的“旧记忆处于衰减态”（旧 timestamp + `last_recalled IS NULL`）场景。

未打补丁时这两条用例都失败；证据见
`fix-3c-20260919/evidence/unpatched_new_tests.txt`，
补丁应用性自查见
`fix-3c-20260919/evidence/patch_apply_dryrun.txt`。

---

# mnemosyne-embed-cache.patch

## 1. 改了什么，为什么

给本地 `mnemosyne-memory==3.15.1` 的 embedding 调用加**线程安全、按模型指纹隔离、带双上限的进程内 LRU 缓存**：

| 模块/函数 | 改动 |
|---|---|
| `mnemosyne/core/embeddings.py` | 新增 `_embed_cache_key()` / `_embed_cached_batch()` / `cache_stats()` / `cache_clear()`；`embed()` 与 `embed_query()` 统一走缓存；新增 `embed_queries()`（一次批量 query-embedding，供适配层预热） |
| `_embed_api` 调用计数 | 用 `_API_CALL_COUNT_LOCK` 保护 `_API_CALL_COUNT += 1` |
| 缓存键 | `sha256(model fingerprint + URL + prefix_kind + prefix + text)`；fingerprint 含 `model`、`url`、`dim`、`digest`、`num_ctx`、`num_gpu`、`ollama_version`（由 `MNEMOSYNE_EMBED_MODEL_DIGEST` / `MNEMOSYNE_EMBED_NUM_CTX` / `MNEMOSYNE_EMBED_NUM_GPU` 提供；可选 `MNEMOSYNE_EMBED_FINGERPRINT_FETCH=1` 时尝试 `/api/show` + `/api/ps`） |
| 值 | `np.ndarray(dtype=float32)`；不存 Python `list[float]` |
| 上限 | `MNEMOSYNE_EMBED_CACHE_SIZE`（默认 8192 条）+ `MNEMOSYNE_EMBED_CACHE_BYTES`（默认 64 MiB）；两者任一超出即 LRU 淘汰 |
| 一键回退 | `MNEMOSYNE_EMBED_CACHE_SIZE=0` 完全关闭缓存，行为回到逐次网络调用 |
| 统计 | `cache_stats()` 返回 `enabled/entries/bytes/max_entries/max_bytes/hits/misses/model_fingerprint`，由 AML `/health` 的 `embed_cache` 字段暴露 |

## 2. 为什么

- 一次 Add(20 消息) 原先 60 次 embedding HTTP，其中 20 次是同一 doc 文本被 BEAM 与 legacy 双写重复嵌入；缓存命中后，doc 文本由适配层批量预热一次，后续单条库调用不再发 HTTP。
- 批量 query 预热同样只改变“何时算”和“算一次还是多次”，同键返回同一 float32 向量；实测 batch16 与单条 `max_abs_diff = 0.0`。
- 模型指纹必须包含 tag/digest/num_ctx/num_gpu/维度，否则同一进程切换 alias/参数后可能错误复用不同语义的向量。

## 3. 如何对干净 mnemosyne 3.15.1 应用

在包含 `mnemosyne/` 包的 `site-packages` 目录执行：

```bash
patch -p1 -d /path/to/site-packages < patches/mnemosyne-embed-cache.patch
```

本仓库参赛版 venv 对应目录：

```text
/home/echo/D/memorycore-aml/.venv/lib/python3.12/site-packages/
```

补丁头为 `a/mnemosyne/...` / `b/mnemosyne/...`，`-p1` 后落到
`mnemosyne/core/embeddings.py`。该文件未被 `mnemosyne-recall-readonly.patch`
改动，两个补丁可独立/顺序应用。

## 4. 如何验证生效

```bash
cd /home/echo/D/memorycore-aml
TMPDIR=/tmp .venv/bin/python -m pytest tests/test_p0_concurrency_safety.py -q
```

其中 `test_p0_embedding_cache_batch_matches_single` 验证缓存命中/未命中、
batch/单条向量完全一致；`tests/test_p0_concurrency_safety.py` 的 G4/G3/G5
用例验证适配层调用方在预算、假 ollama、背压场景下的行为。
