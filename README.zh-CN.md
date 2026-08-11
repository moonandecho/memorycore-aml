# origin-memorycore

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore 是一个面向 LLM Agent 的记忆治理层 (memory governance layer)。**

Agent 积累记忆的速度很快——偏好、事实、决策——而不维护的记忆会悄悄退化:重复条目堆积、过时事实滞留、热层塞满后开始拒绝写入。MemoryCore 阻止这一切发生。

它采用双层记忆架构:

- **热层 (Hot tier)** —— 高频使用的行为知识(偏好、规则、纠正),存放在本地快速文件中,始终在上下文内。
- **冷层 (Cold tier)** —— 低频事实,自动迁移出去,存放在进程内 SQLite 引擎中(或你配置的远程记忆服务)。

两层之间,一个治理核心维持记忆健康:

- **写入时去重** —— 相似事实在存入前合并,而不是重复堆积。
- **容量控制** —— 软/硬阈值在热层写满之前触发溢流,让它永不拒绝写入。
- **冷层治理** —— 周期性去重/清理,让冷层在增长中保持可检索。
- **回收站** —— 被删除的条目有 30 天宽限期;召回一条被回收的记忆即可复活它。

结果:热层保持在预算内,冷层保持可检索,无论 Agent 积累多少记忆,记忆始终可维护。

基于 [MCP](https://modelcontextprotocol.io)(Model Context Protocol)`streamable-http` / stdio 标准构建。适用于任何 MCP 客户端,已在 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 上测试。

---

## 特性

- **记忆治理(核心)** —— 冷层数据完整性的三层保护:
  - **冷层写入去重**:写入冷层前,语义召回 + LLM 判断检查重复,更新已有条目而非创建冗余。
  - **容量硬闸**:冷层强制软上限(6000 条,触发一次治理)和硬上限(10000 条,强制治理循环)——防止无界增长。
  - **回收站**(`trash_store.py`):被删除的冷层条目移入 `~/.memorycore/trash.json`,30 天过期。召回被回收的条目时,若带有新的语义证据则恢复("召回即复活")。
- **冷/热路由** —— 每次写入都被分类:高重要度或偏好类 → 热层(本地);低频事实 → 冷层(远程);过时状态记录 → 丢弃。
- **六步溢流** —— 容量基线 → 去重 → 过时过滤 → 合并 → 安全写入(先写冷层,再删本地)→ 验证。
- **冷层治理** —— 去重合并、过时清理、冲突消解、embedding 完整性检查。
- **容量控制** —— 软阈值(写入前溢流一次)/ 硬阈值(强制溢流)/ 目标比例。默认:5000 字符限制的 60% / 80% / 40%。
- **优雅降级** —— 冷层不可达?写入大声失败(绝不静默丢弃),溢流保留本地条目,健康检查返回本地状态并标注 `cold.error`。
- **零核心修改** —— 设计为即插即用的伴侣组件;Agent 内置的记忆工具继续正常工作。

## 架构

```
┌─────────────────────────────── Mac / 本地 ──────────────────────────────┐
│  LLM Agent (如 Hermes)                                                 │
│    │  MCP client                                                       │
│    ▼                                                                   │
│  MemoryCore MCP server                                                 │
│    ├─ local_store.py        热层: MEMORY.md / USER.md (基于字符)        │
│    ├─ classifier.py         冷/热/过时 路由规则                         │
│    ├─ overflow.py           六步溢流                                   │
│    ├─ maintenance.py        冷层治理                                   │
│    └─ cold_store_client.py  →  LocalBackend (SQLite, 进程内)           │
│                               or RemoteBackend (MCP streamable-http)   │
└─────────────────────────────────────────────────────────────────────────┘
                     LocalBackend: mnemosyne-memory (进程内引擎)
                     RemoteBackend: 远程 MCP 记忆服务

可选 (仅 Hermes Agent): hermes-plugin/memorycore-prefetch
  ┌───────────────────────────────────────────────────────────────────────┐
  │ MemoryProvider 插件 (单模型 qwen3, 默认开启)                           │
  │   system_prompt_block → 静态索引 (常驻激活)                            │
  │   prefetch → ColdStoreClient.recall_results(top_k=20)                 │
  │            → dense 排序 → 会话 + 热层去重 → top-5 注入                │
  │   关闭: MEMORYCORE_PREFETCH_ENABLED=0                                 │
  └───────────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 前置依赖

- **ollama** — embedding API (安装: https://ollama.com)
- **qwen3-embedding:0.6b** — 推荐 embedding 模型 (1024 维)

```bash
# 安装 ollama (macOS/Linux)
curl -fsSL https://ollama.com/install.sh | sh

# 拉取 embedding 模型
ollama pull qwen3-embedding:0.6b
```

### 安装与运行

```bash
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# 就这样! MemoryCore 使用 ollama 提供 embedding:
#   - 热层:  MEMORY.md / USER.md (默认 ~/.hermes/memories)
#   - 冷层:  SQLite (通过 mnemosyne-memory, 默认 ~/.memorycore/data/)
#   - Embedding: qwen3-embedding:0.6b (通过 ollama, http://localhost:11434/v1)
python -m memorycore.server          # stdio 传输 (默认)
```

**数据目录布局**(全部位于 `~/.memorycore/` 下):

```
~/.memorycore/
├── data/          # SQLite 数据库 (MNEMOSYNE_DATA_DIR)
└── ...
```

可用 `MNEMOSYNE_DATA_DIR` 覆盖。

### 模型切换

默认 embedding 模型为 `qwen3-embedding:0.6b`(1024 维)。可通过环境变量使用任意 ollama 模型:

```bash
export MEMORYCORE_EMBED_URL="http://localhost:11434/v1"
export MEMORYCORE_EMBED_MODEL="nomic-embed-text"   # 或你偏好的模型
```

也可指向任何 OpenAI 兼容的 embedding API:

```bash
export MEMORYCORE_EMBED_URL="https://api.openai.com/v1"
export MEMORYCORE_EMBED_MODEL="text-embedding-3-small"
```

在 MCP 客户端注册(以 Hermes Agent `config.yaml` 为例):

```yaml
mcp_servers:
  memorycore:
    command: python
    args: ["-m", "memorycore.server"]
```

### 远程模式(可选)

如果你希望使用共享的远程 Mnemosyne MCP 服务而非本地引擎,设置 `MEMORYCORE_COLD_BACKEND=remote`:

```bash
export MEMORYCORE_COLD_BACKEND=remote
export MNEMOSYNE_URL="http://your-memory-service:9000/mcp"
python -m memorycore.server
```

暴露的工具:

| 工具 | 用途 |
|---|---|
| `memorycore_store_fact(content, importance, scope, target)` | 统一写入入口:路由冷 / 热 / 过时 |
| `memorycore_recall(query, top_k)` | 主动召回冷层记忆(只读,补充每轮 prefetch) |
| `memorycore_trigger_overflow(target)` | 执行六步溢流,目标 ≤40% |
| `memorycore_run_cold_storage_maintenance()` | 冷层治理流程 |
| `memorycore_get_memory_usage()` | 热层用量 + 冷层统计 + 阈值 |

## Hermes 集成 —— 每轮主动召回 prefetch

MCP server 与客户端无关。对于 **Hermes Agent**,有一个可选伴侣插件提供双通道冷层访问:

### 双通道设计

- **静态索引通道(常驻,零开销)** —— 系统提示块列出可用主题(通过 `MEMORYCORE_INDEX_TOPICS` 配置,逗号分隔),并引导 Agent 使用 `memorycore_recall(query)` 按需召回。
- **每轮主动召回通道(默认开启)** —— 每轮对话自动召回冷层,按 dense 分数排序,注入 top-5 到上下文,让 Agent 开口前就"想起"相关内容。设置 `MEMORYCORE_PREFETCH_ENABLED=0` 可关闭,仅保留按需召回。

### Prefetch 管道

```
query → 预处理 → 冷层召回(20 候选)
  → dense 排序 (qwen3) → top-5
  → 会话去重 → 热层去重 → 注入上下文
```

MemoryCore 采用**单模型 qwen3 架构(无 reranker)**。qwen3 的 dense 分数用于批次内相对排序;没有绝对阈值——dense 分数最高的 5 条候选在去重后始终注入。

### 优雅降级

当 ollama 不可达(未安装、未运行或模型未拉取)时,prefetch 静默返回空字符串——对话继续,没有注入的记忆,用户不会看到任何错误。DEBUG 级别日志会记录探测失败。

### 部署方案(Hermes Agent)

```bash
# 1. 安装 origin-memorycore(提供冷层引擎 + ColdStoreClient)
pip install "origin-memorycore @ git+https://github.com/moonandecho/origin-memorycore.git"

# 2. 把插件放入 Hermes 用户插件目录
mkdir -p ~/.hermes/plugins
cp -r hermes-plugin/memorycore-prefetch ~/.hermes/plugins/

# 3. 激活(下一会话生效)
hermes config set memory.provider memorycore-prefetch
```

部署后三种形态:

| 形态 | 配置 | 行为 |
|---|---|---|
| 默认(推荐) | 无需额外配置 | 静态索引 + 每轮 prefetch,注入 top-5 |
| 仅按需召回 | `MEMORYCORE_PREFETCH_ENABLED=0` | 只启用静态索引,Agent 通过 `memorycore_recall` 按需查询 |
| 自定义 embedding | `MEMORYCORE_EMBED_URL` + `MEMORYCORE_EMBED_MODEL` | 指向不同 ollama 实例或 OpenAI 兼容 API |

### 插件配置

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `MEMORYCORE_PREFETCH_ENABLED` | *(未设置)* | 设为 `0` 关闭每轮主动召回 |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama 或 OpenAI 兼容 embedding API 基地址 |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding 模型名称(推荐 1024 维) |
| `MEMORYCORE_INDEX_TOPICS` | *(未设置)* | 系统提示索引块的主题列表(逗号分隔) |

要求与注意:

- **Hermes 专用**:插件导入 Hermes 运行时模块(`agent.memory_provider`),不能作为独立包运行——它是 MemoryCore 的 Hermes 集成侧。完整说明见 [hermes-plugin/memorycore-prefetch/README.md](hermes-plugin/memorycore-prefetch/README.md)。
- 每次召回保持 5s 超时;失败静默降级为空注入,绝不阻塞对话。

## 规模化测试与优化结果

MemoryCore 在万条级冷层规模下做了完整压力测试与召回优化(隔离测试环境,生产数据零接触,结果可复现)。

**写入与容量**

| 指标 | 结果 |
|---|---|
| 写入吞吐 | 10k 条共 467s,≈21.4 条/s(瓶颈在 embedding) |
| 库文件体积 | 300MB / 10k 条 |
| 内存占用 | 进程 RSS 仅 +19MB,全程平稳无泄漏特征 |

**查询延迟** —— top_k=5 时中位 48ms;万条规模与百条规模持平,无延迟退化。

**召回质量** —— 三项测试:

1. **精确匹配(原文自召回)**:20/20 全部命中 top1 —— 精确匹配能力完整。
2. **噪声抑制(无关查询)**:top1 dense 分数均值 0.056,绝大多数返回 0.0 —— 无关内容几乎不会混入结果。
3. **短查询召回(修复前 → 修复后)** —— 关键优化成果:

| 阶段 | 短查询命中率 |
|---|---|
| 修复前 | 0/8 |
| 修复后 | 5/8 (62.5%) |

**优化内容**:高密度主题下,固定候选截断 `k=max(top_k, 20)` 会把详细记忆挤出候选池,导致短查询召回失败。修复将候选截断放大为 `k=max(top_k*4, 300)`,并在召回入口内部放大候选后再截断返回——所有召回通道(每轮 prefetch + 按需 recall)一处修复全部受益。修复只发生在召回阶段,排序逻辑未改动,行为可预期、可回退。

> 注:测试在 10k 条合成库上进行(80 条"黄金记忆"+ 9920 条日常口吻填充记忆,与生产同配置),生产数据零污染。

## sqlite-vec 用户注意事项

如果你为 Mnemosyne 冷层启用 sqlite-vec 向量索引,请注意 `beam.py` 的 `_wm_vec_search_sqlite` 使用原始相似度公式 `sim = 1 - distance / (2 * EMBEDDING_DIM)`,会把 float32 距离压缩到 ~1.0,使动态阈值实际失效(所有结果都通过)。

**补丁**:在 float32 分支中,将公式替换为 `sim = 1 - d² / 2` —— 这给出归一化向量的精确余弦相似度,恢复正确的阈值行为。

## 冷存储契约

任何暴露以下五个 MCP 工具的服务都可以作为冷层:

| 工具 | 语义 |
|---|---|
| `remember(content, importance, scope)` | 存储一条记忆,返回 `memory_id` |
| `recall(query, top_k)` | 语义召回 |
| `update(memory_id, content)` | 合并更新已有记忆 |
| `forget(memory_id)` | 删除一条记忆 |
| `stats()` | `total` + embedding 完整性 |

完整契约与参考客户端见 [examples/cold-store-contract.md](examples/cold-store-contract.md)。

## 配置

| 环境变量 | 默认值 | 含义 |
|---|---|---|
| `MEMORYCORE_COLD_BACKEND` | `local` | 冷层后端:`local`(进程内)或 `remote`(MCP) |
| `MNEMOSYNE_URL` | *(空)* | 冷层 MCP 端点(`remote` 模式必需) |
| `MNEMOSYNE_DATA_DIR` | `~/.memorycore/data` | 本地 SQLite 数据目录 |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | Ollama 或 OpenAI 兼容 embedding API 基地址 |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | Embedding 模型名称(1024 维) |
| `MEMORY_DIR` | `~/.hermes/memories` | 热层目录(`MEMORY.md` / `USER.md`) |
| `MNEMOSYNE_TIMEOUT` | `10.0` | 冷层请求超时(远程模式,秒) |

容量常量位于 `memorycore/core/config.py`(`CHAR_LIMIT_*`、`SOFT_THRESHOLD`、`HARD_THRESHOLD`、`TARGET_RATIO`)。

## 工作原理

1. **写入** —— `store_fact` 分类内容:
   - importance ≥ 0.8 或命中热关键词(偏好 / 规则 / 纠正 / 红线)→ **热层**,留在本地
   - 过时标记(短条目,如 "已修复 / fixed")→ **丢弃**(不迁移)
   - 其他 → **冷层**,直接写入远程服务
2. **溢流** —— 热层用量超过软阈值时,溢流将低频条目迁移到冷层;达到硬阈值时强制溢流直到 ≤ 目标。顺序永远是*先写冷层,验证,再删本地* —— 冷层失败也不会丢任何东西。
3. **治理** —— 周期性对冷层执行:合并重复、移除过时、消解冲突、验证 embedding 完整性。

## 许可

[MIT](LICENSE) © 2026 moonandecho

### 第三方许可

- [mnemosyne-memory](https://github.com/mnemosyne-oss/mnemosyne) — MIT,by AxDSan。`LocalBackend` 使用的进程内记忆引擎。
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) — MIT。
- [ollama](https://ollama.com) — MIT。本地 embedding API 服务。
- [qwen3-embedding](https://huggingface.co/Qwen/Qwen3-Embedding-0.6B) — Apache-2.0,by Alibaba Cloud。默认 embedding 模型(非内置,通过 ollama 拉取)。
