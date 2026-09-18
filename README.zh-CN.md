# MemoryCore — AML 在线治理参赛版

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore** 是面向 LLM Agent 的记忆治理层: Agent 积累记忆的速度很快——偏好、事实、决策——而不治理的记忆会静默退化。MemoryCore 让热层保持在预算内、冷层保持可检索、每次写入都经去重与合并。

本仓库是 **Agent Memory Leaderboard (AML) 参赛版**。它把 MemoryCore 已验证的治理机制**在线化**, 直接跑在 AML 的写入/检索路径上:

- **写入治理(`/add`)** —— 消息 → 事实切分 → 过时过滤 → 语义去重/合并 → 冷层写入(按身份隔离)。
- **检索治理(`/search`)** —— 样本内召回(top_k 至 100)→ 相似度 × 时间衰减排序(半衰期 90 天, 重要记忆不衰减)→ AML 格式证据。
- **严格样本隔离** —— `user_id` 一对一映射存储层 `author_id`; 跨用户检索不到任何记忆(SQL 层过滤, 测试覆盖)。
- **评测期零外部 API 调用** —— embedding 本地运行(ollama + `qwen3-embedding:0.6b`, 1024 维), 存储为进程内 SQLite 引擎([mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/))。

上游开源项目: [moonandecho/origin-memorycore](https://github.com/moonandecho/origin-memorycore)(MIT)。本仓库在其之上新增 AML HTTP 适配层 `memorycore/aml_server.py` 与按身份( author_id )的透传隔离。

**第二期提交形态**: 本参赛系统通过**参赛方自托管的公开 Add/Search 端点**接入(第二期不接受仅提交仓库或 Docker 镜像、由平台代为部署的形式, 见 [AML-COMPETITION.md](AML-COMPETITION.md) §2)。参评赛道: **仅文本记忆**; 本期不报多模态与代码赛道。


**完整方法披露 / 部署说明 / 可复现测试 / 诚实边界声明: [AML-COMPETITION.md](AML-COMPETITION.md)**(英文版: [AML-COMPETITION.en.md](AML-COMPETITION.en.md))。

## 快速开始

### Docker(可选 — 仅用于本地复现)

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
```

> 镜像已在构建期预置 embedding 模型(`qwen3-embedding:0.6b`, 约 639MB)——容器启动无需联网。entrypoint 保留兜底: 仅当模型缺失(如自定义 `MEMORYCORE_EMBED_MODEL`)时才在运行时联网拉取。数据卷 `/data` 持久化 SQLite 记忆库, 重启不丢。第二期要求参赛方自托管端点, 故本镜像仅用于本地复现。

冒烟:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/add -H "Content-Type: application/json" -d '{
  "request_id": "eval:smoke:0",
  "messages": [{"role": "user", "content": "The user is a software engineer."}],
  "user_id": "eval:smoke:u1",
  "session_id": "eval:smoke:s0"}'
curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d '{
  "query": "What is the user\u0027s occupation?",
  "options": ["A. software engineer", "B. teacher"],
  "user_id": "eval:smoke:u1", "top_k": 100}'
```

### 裸机

```bash
python3 -m venv .venv && source .venv/bin/activate   # 推荐: 独立 venv, 依赖 mcp>=2,<3 (已知兼容 2.0.0/2.2.0)
pip install .
ollama serve &
ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data python -m memorycore.aml_server   # 默认 0.0.0.0:8000
```

## HTTP API

全部为纯 REST 端点, 挂载在 MCP server streamable-http 应用上(uvicorn)。可选鉴权: 设置 `AML_API_KEY` 后支持 Bearer / Token / X-Api-Key。

| 端点 | 说明 |
|---|---|
| `POST /add` | AML 写入: 消息 → 事实切分 → 过时过滤 → 语义去重/合并 → 按身份写入冷层。写入落库后才返回 200(无异步任务 ID)。 |
| `POST /search` | AML 检索: 按身份样本内召回 → 时间衰减排序 → AML 格式证据列表。`options` 会拼进检索查询做一次兜底召回(仅用于检索上下文——不生成答案、不写入记忆)。 |
| `GET /health` | 存活探测。存储降级时仍返回 2xx(`status: degraded` + `storage.error`), 便于平台区分崩溃与依赖故障。 |

错误码语义:

- `400` — 请求格式错误(缺 `request_id`/`user_id`/`session_id`、`messages` 非数组等)
- `401` — 设置了 `AML_API_KEY` 但鉴权缺失/无效
- `500` — 存储后端不可用(如 embedding 服务宕机); 临时异常, 平台按 5xx 自动重试安全

## 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| `AML_HOST` / `AML_PORT` | `0.0.0.0` / `8000` | HTTP 监听地址 |
| `AML_API_KEY` | 空(不鉴权, smoke 模式) | 设置后 Add/Search 需 Bearer/Token/X-Api-Key |
| `MNEMOSYNE_DATA_DIR` | `~/.memorycore/data` | SQLite 数据目录(评测请指向独立目录) |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | embedding API(ollama 或任何 OpenAI 兼容服务) |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | embedding 模型(1024 维) |
| `LLM_API_KEY` | 空(LLM 关闭) | 可选 LLM 增强 key(空 = 纯规则, 始终安全) |
| `LLM_BASE_URL` / `LLM_MODEL` | `https://api.deepseek.com` / `deepseek-v4-flash` | 可选 LLM 端点/模型 |

> **LLM 增强为可选项, 默认关闭。** 评测期间本参赛版**不产生任何外部 API
> 调用**(embedding 为本地 ollama; LLM 特性不运行在 `/add` / `/search`
> 路径上)。可选 LLM(热层压缩 / 休眠判定 / 下沉确认与合并)**仅读环境变量**:
> 文件来源(`~/.hermes/.env` / `~/.hermes/config.yaml`)默认关闭, 需显式
> `MEMCORE_LLM_FILE_SOURCES=1` 才读取, 因此绝不会静默捡起属于其它工具
> (如 Hermes)的 key 开始计费外呼。安全阀: `MEMCORE_LLM_ENABLED=0` 总开关、
> 每轮调用上限(`MEMCORE_LLM_MAX_CALLS` / `MEMCORE_LLM_COLD_MAX_CALLS`,
> 默认 8)与失败退避。LLM 状态始终可见于统计/报告/日志。
> 自检: `python -m memorycore.llm_check [--live]`(--live 至多计费一次
> max_tokens=1 的 completion, ~1e-5 元级)。

## 架构

```
AML HTTP (MCP server custom routes: /add /search /health, 可选 Bearer/Token/X-Api-Key)
   │
   ├─ Add: 事实切分 → 过时过滤 → 语义去重/合并 → 冷层写入 (author_id=user_id)
   ├─ Search: 样本内召回 (author_id 过滤, top_k≤100) → 时间衰减排序 → AML 格式
   └─ Health: 2xx 存活探测 (存储降级时仍 200, 报 degraded)
        │
        ▼
MemoryCore cold-store client (per-user 引擎, 进程内 SQLite + 向量索引)
        │
        ▼
embedding: ollama + qwen3-embedding:0.6b (本地, 无外部 API 依赖)
```

底层治理层(冷热路由 / 写入时归一化去重 / 容量控制 / 溢流 / 回收站)的完整说明见[上游项目](https://github.com/moonandecho/origin-memorycore)。

## 上游治理同步（R1–R3）

本 AML 参赛版保留自己的适配层（`memorycore/aml_server.py`、按身份隔离、
per-user 引擎、容器契约）不变，底层共享实现已携带
origin-memorycore 的 R1–R3 治理内核。AML 的 `/add` 与 `/search` 本来就走
该内核（`classify`、`_find_best_match` / `_merge_two_entries`、
`_apply_decay`、`ColdStoreClient`），因此 R1–R3 修复在线生效，HTTP 契约不变。

- **R1 — 缓存策略 V2 / SAFE-JUDGE v3 / `_ts_anchor`**：统一 LRU 候选池，
  `GRACE_MULT=9.0`、`RULE_BUDGET_CHARS=2000`、`RULE_MIN_RESIDENCY_DAYS=7`、
  `MAX_EVICT_PER_RUN=3`、`HIT_STRONG_COS=0.48`；同步路径零 LLM 的三值判型；
  统一时间入口 `metadata._ts_anchor`（未来时间容差 300 s）。
- **R2 — 隐私**：夹具全部为合成语料；全树无真实标识、路径或语料。
- **R3 — 写实合成语料**：409 条真实分布短查询；夹具规则与查询最长公共子串
  ≤7 字；回放基线冻结为 `fault_rate=7.0%`、`relative_drop=83.1%`。
- **发布版隔离**：测试把 `MEMORY_DIR` / `MNEMOSYNE_DATA_DIR` 指向临时运行时，
  LLM 文件来源默认关闭（需显式 `MEMCORE_LLM_FILE_SOURCES=1`），不读
  `~/.hermes` 生产配置。

### 热层缓存策略 V2(LRU,2026-09-13)

- **统一候选池**:`rule`/`state`/指针 `stub` 不再分档,全部进入同一排序池:
  `w_eff × protected(×3.0) × kw_sink(×0.5) × 新鲜窗口(×9.0)`。
- **预算硬约束**:规则生态 `RULE_BUDGET_CHARS=2000`(与 40% 目标同源);
  换出一律先冷层写成功;单轮全文换出 ≤ `MAX_EVICT_PER_RUN=3`。
- **两阶段换出**:普通路径留 ≤40 字指针(`STUB_MAX_CHARS=40`,句柄
  `STUB_HANDLE_MAX_CHARS=20`)+ `cold_id`;`memorycore_recall(handle=...)`
  绕过阈值直查并标记 `page_fault`,驱动写回恢复;显式 0 指针预算时全文冷写
  成功后直接删本地(`--budget 0` / T3 cold-only)。
- **无永久驻留**:全 protected / 红线 / importance≥0.9 / `protect_override`
  在足够压力下仍可换出;固定压测:
  `tests/test_cache_policy_v2.py::test_no_permanent_residency_all_protected`。
- **新鲜窗口是排序乘数**:`RULE_MIN_RESIDENCY_DAYS=7`,`written_at` /
  `last_recall_hit_at` 经 `_ts_anchor` 统一入口;窗口内 ×`GRACE_MULT=9.0`
  (设计用 bundled R2 合成快照夹具标定,25 条;`tools/residency_dryrun.py`
  可复现新鲜窗口排序),压力足够仍可换出。
- **活性参数**:`WEIGHT_INIT=1.0`,半衰期 30 天,强命中 +1.0
  (`HIT_STRONG_COS=0.48`),弱命中 +0.3,封顶 `WEIGHT_MAX=5.0`,
  kw-sink 乘数 `WEIGHT_KWSINK_MULT=0.5`。

### SAFE-JUDGE v3 判型(2026-09-13)

`memorycore/core/judge.py` 用一次确定性三态判型替代旧的
`classify()` + `should_keep_local()` 双判:

- `state` → 冷迁移(冷写成功才删本地);
- `rule` → 留热层,写 `judge_v3` 审计字段;
- `ambiguous` → 强制留热层,写 `judge_review_at`(首审 +7 天,
  `JUDGE_AMBIGUOUS_LRU_DAYS=21` A1 指针兜底,最多 2 次复审)。同步判型零
  LLM,只有周治理可发起一次显式 LLM 终审;终审为 rule 给
  `JUDGE_RESOLVED_RULE_GRACE_DAYS=14` 审计宽限。
- strong rule 与 ambiguous 在写入口强制 hot;ambiguous 本地写失败直接报错,
  不允许静默冷迁兜底(`DESIGN-DEVIATIONS.md` §6.5)。

### 回滚开关

| 开关 | 默认 | 效果 |
|---|---|---|
| `MEMORYCORE_CACHE_POLICY_V2=0` | `1` | 回退旧资格候选池(protected 资格豁免/年龄门),冷写安全铁律不变;`PROTECT_SKIP_LRU=1` 仅告警(已废弃) |
| `RULE_MIN_RESIDENCY_DAYS <= 0` | `7` | 只关闭新鲜窗口乘数(等价旧纯 rank 排序) |
| `GRACE_MULT <= 0` | `9.0` | 同上,排序乘数入口关闭 |
| `MEMORYCORE_RULE_BUDGET_ENABLED=0` | `1` | 关闭规则预算换出层(仍受硬 5000 字兜底) |
| `MEMORYCORE_JUDGE_V3_ENABLED=0` | `1` | 回退词法 v2 判型(`CLASSIFIER_V2_ENABLED` 决定 v2/v1),attack 基线 20/29 |
| `MEMORYCORE_JUDGE_AMBIGUOUS_HOLD=0` | `1` | ambiguous 当 rule(二值行为,不写复审期限) |
| `MEMCORE_LLM_FILE_SOURCES=1` | `0` | 选择性开启白名单 `~/.hermes/.env` / `config.yaml` 文件来源,默认绝不开 |

常量(`memorycore/core/config.py`):`RULE_BUDGET_CHARS=2000`、
`INDEX_BUDGET_CHARS=800`、`RULE_MIN_RESIDENCY_DAYS=7`、`GRACE_MULT=9.0`、
`WEIGHT_INIT=1.0`、`WEIGHT_PROTECT_MULT=3.0`、`WEIGHT_KWSINK_MULT=0.5`、
`WEIGHT_HALF_LIFE_DAYS=30`、`HIT_STRONG_COS=0.48`、`MAX_EVICT_PER_RUN=3`、
`MAX_STUB_PER_RUN=3`、`STUB_MAX_CHARS=40`、
`JUDGE_AMBIGUOUS_REVIEW_DAYS=7`、`JUDGE_AMBIGUOUS_LRU_DAYS=21`、
`JUDGE_RESOLVED_RULE_GRACE_DAYS=14`。

## 可复现合成夹具

发布树在 `tests/fixtures/synthetic/` 提供中性合成夹具（25 条 `notehub` 规则：
19 rule + 恰好 6 条完成态/历史 state；5 条 USER 示例；配套 sidecar 元数据；
409 条写实分布的带时间戳查询：短问句改写、低重叠语义问法、真正无关噪声、
会被 F1 闸门挡掉的低信息短句与动作型指令，且不把规则原文抄进查询）。
依赖夹具的验收路径不读取任何生产记忆。用 bundled
一次性生成器重建 silver 回放夹具：

```bash
.venv/bin/python tools/build_fault_replay_fixture.py \
  --activity tests/fixtures/synthetic/activity.jsonl \
  --rules    tests/fixtures/synthetic/MEMORY.md \
  --out      tests/fixtures/fault_replay_silver.json
```

合成语料上复测基线：

| 检查项 | 结果 |
|---|---|
| 缺页率回放（`replay_fault_rate.py`） | `hits=186/200 faults=14 fault_rate=7.0% baseline=41.5% relative_drop=83.1% pass=True`（R3 写实合成语料；夹具可复现，与生产语料数值不同） |
| 合成快照 retype 干跑 | `19 rule / 6 state`，state 集合恰为 6 条 bundled 目标 |
| 迁移后水位 | `2598 / 5000 chars（51%）` |
| 快照预算回放 | `--budget 2000` 与 `--budget 0` 均 EXIT=0 |
| 驻留干跑 | 25 条 / 3441 chars / need 1441，`need_satisfied=True`、`new_evictable_when_full=True` |

## 测试(可复现)

```bash
# 全量回归(隔离临时运行时, 不碰生产路径):
.venv/bin/python -m pytest tests/ -q          # 【待填：全量测试数】 passed

# 夹具验收(无需 ollama, 只读冻结 silver fixture):
.venv/bin/python tools/replay_fault_rate.py \
    --fixture tests/fixtures/fault_replay_silver.json
# -> hits=186/200 faults=14 fault_rate=7.0% relative_drop=83.1% pass=True

.venv/bin/python tools/retype_20260912.py --dry-run \
    --data-dir tests/fixtures/snapshot_20260912 --backup-dir /tmp/retype-backup
# -> 19 rule / 6 state

# AML 适配层端到端(需 ollama + qwen3-embedding:0.6b):
MNEMOSYNE_DATA_DIR=$(mktemp -d) python3 tests/test_aml.py
```

28 项断言覆盖: 跨 user_id 隔离(A 写 B 查不到)、同一事实二次写入去重、"方案 A → 改为 B"合并为一条、/add /search /health HTTP 全链路、options 兜底召回、长消息切分、错误码。

## 已知边界(诚实声明)
- 超长消息会被切分为**每条上限 300 字符**的自包含事实片段——好处是每个片段都能被独立检索到, 代价是超长证据会被切散。
- `/search` 单条证据默认最多返回 **2000 字符**(可用 `MEMORYCORE_CONTENT_MAX_CHARS` 覆盖)。
- 检索是**只读**的: 同一问题反复检索结果逐字一致(检索不刷新记忆的活性时间戳; 需应用 AML-COMPETITION.md §4.3 披露的存储层补丁)。
- 本期不报多模态与代码赛道; 消息自带 `timestamp` 不作为事件时间(排序使用持久化写入时间)。

- 检索走存储层词法+向量混合排序, 存储层对长查询有词法相关性门禁; 选择题场景由 options 兜底召回覆盖, 开放题(英文自然问句)实测可正常召回。
- 消息自带 timestamp 仅作参考, 排序使用持久化时间(写入时间); created_at 返回持久化时间(协议允许的"来源/持久化时间")。
- 不做冷层全量治理巡检(维护性批处理)在线触发, 写入侧治理以去重/合并/过时过滤为主。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。引擎依赖 [mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/) 与 [ollama](https://ollama.com) + qwen3-embedding 为 MIT / Apache-2.0。逐组件改动披露见 [AML-COMPETITION.md](AML-COMPETITION.md)。
