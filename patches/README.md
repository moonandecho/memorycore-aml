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
