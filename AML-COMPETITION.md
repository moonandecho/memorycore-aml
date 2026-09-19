# MemoryCore — AML 第二期参赛材料（文本记忆赛道 · 开源方法榜）

> 本文件为提交材料的"方法说明 / 部署 / 披露 / 复现"合订稿。
> 基础事实以 AML Cycle 2 现行规范为准（本稿写于 2026-09-19，提交前会再抓官网比对一次）。

## 0. 一页速览

| 项 | 内容 |
|---|---|
| 系统名称与版本 | MemoryCore（AML 适配版）v0.1.0；**固定 commit**：以部署端点 `/health` 返回的 `commit` 字段为准（提交申请表时同步声明） |
| 参评类型 | **开源方法榜**（参赛方自托管 Add / Search API，非仓库提交） |
| 参评赛道 | **文本记忆**（本期不报多模态赛道、不报代码赛道） |
| Add 端点 | `http://47.108.28.129:18000/add` |
| Search 端点 | `http://47.108.28.129:18000/search` |
| 健康检查 | `http://47.108.28.129:18000/health`（免鉴权，2xx） |
| 鉴权 | 请求头 `X-Api-Key: <Memory System Key>`；另支持 `Authorization: Bearer` 与 `X-Token` |
| 公开仓库 | https://github.com/moonandecho/memorycore-aml |
| 原始工作 | MemoryCore / origin-memorycore（我方，MIT）；mnemosyne-memory 3.15.1（MIT）；ollama + qwen3-embedding:0.6b |
| 许可证 | MIT |
| 端点可达承诺 | 保持公开可达、版本固定至 **2026-12-05**（含提交后 30 天承诺，见 §2.4） |
| 团队（公榜署名） | **moonandecho**（个人参赛） |
| 联系人 | 随**准入申请表**提交给主办方（含真实姓名与邮箱）；本公开仓库不展示个人姓名 |
| 允许公开展示 | 系统名称、方法说明、公开仓库与固定 commit；不公开评测数据与私有结果 |

## 1. 方法说明（技术路线）

MemoryCore 是一个面向 LLM Agent 的**记忆治理层**（MIT 开源）。本期的差异化路线是
**把记忆治理机制在线化**：不做多跳检索补全（query 改写 / 迭代搜索），而是把 MemoryCore
已在生产使用中验证过的治理能力，直接放到 AML 协议的**写入（Add）与检索（Search）路径**上执行。

- **写入治理（Add 在线执行，同步完成后才返回 200）**
  - 事实切分：消息按句子边界切成自包含片段（长消息拆段，短消息整存；片段上限 300 字符）
  - 过时过滤：过时状态记录（"已修复"式短标记）不写入
  - 语义去重 / 合并：写入前在样本范围内召回候选，逐字重复跳过；相似事实合并进同一条
    （"方案 A" 与 "方案 A 改为 B" 最终是一条记忆）——合并以字面相似度为主信号，
    并以**原分隔符**保留现场（换行 / 分号不再被重拼，代码片段与命令不会被拆坏）
  - 时序标注：每条记忆落库即带持久化时间戳，参与检索排序
- **检索治理（Search 在线执行）**
  - 样本内召回（top_k ≤ 100），按「相似度 × 时间衰减」排序输出（半衰期 90 天；久未提及的事实自然降权）
  - **只读召回**：检索**不修改**记忆的活性状态（不刷新 last_recalled / recall_count），
    因此同一问题反复检索结果**逐字可复现**（见 §4 的存储层补丁披露）
  - options 兜底召回：选择题的候选项拼接进检索查询做一次兜底召回（仅用于检索上下文，
    不生成答案、不写入记忆）
- **样本隔离（硬约束）**：`user_id` 一对一映射到存储层 `author_id`，写入、去重召回、检索全程强制携带；
  存储层在 SQL 层按 `author_id` 过滤——跨 `user_id` 检索不到任何记忆（有测试覆盖）。
- **不做的事（边界声明）**：不做 query 改写 / 多跳检索；不调用外部 LLM 生成答案；
  Search 只返回记忆证据原文，不返回答案。

## 2. 端点、鉴权、容量与运行限制（提交说明）

### 2.1 端点与请求形态

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| POST | `/add` | 需要 | 同步写入；`request_id` 幂等（重复请求不会二次写入） |
| POST | `/search` | 需要 | 返回 `{"data":[{"id","content","score","created_at"}...]}`，按 score 降序 |
| GET | `/health` | 免鉴权 | 存活探测；存储降级时仍返回 2xx 并在体内标注 degraded |

### 2.2 容量声明

### 2.2 容量声明（实测，2026-09-19）

| 项 | 实测值 | 说明 |
|---|---|---|
| Add 64 并发 × 20 消息 | **墙钟 103.5 s，成功率 100%**，p50 56.4 s，p95 100.2 s | **经公网自托管端点实测**（Mac → 公网 → 本机服务）；平台单请求超时 1200 s → **约 11 倍余量** |
| 同上（内网直连） | 墙钟 109.5 s，p50 60.0 s，p95 107.1 s | 本机直连复核 |
| 单次 Add（20 消息） | 3.4～4.5 s | 每一请求含约 4 次嵌入 HTTP（批量） |
| 单次 Search | 0.28 s | top_k=3 公网实测；top_k=100 响应 155.7 KB（规范上限 30 MiB 的 0.5%） |
| 健康检查 | **1 ms 级**（最坏情况亦 1 s 内） | 内存快照，不访问存储层 |
| 服务常驻内存 | 约 62 MB（上限 2 GB，另有按字节背压） | 单机、CPU/GPU 混合推理 |
| 嵌入吞吐 | 批量约 14.6～16.0 文本/秒（GPU 纯负载） | 见 §4.5 |

> Search 32 并发未单独压测（单次 0.28 s；其嵌入同样受益于批量与缓存）。

- Add：64 并发 × 20 消息/请求 → 成功率 __%，p50 __ ms，p95 __ ms，p99 __ ms，峰值常驻内存 __ MB
- Search：32 并发 → 成功率 __%，p95 __ ms
- 单请求上限：≤20 条消息 / ≤2,000 词（规范值）；实测单次 Add 墙钟 p95 __ s（< 1200 s 平台超时）
- 单次响应体积：top_k=100 实测 __ KB，占规范 30 MiB 上限的 __%

### 2.3 运行限制与运维约束

- 部署形态：单机自托管（参赛方自有服务器，CPU 推理），无外部 API 依赖（embedding 走本机 ollama）
- 每次评测的 `user_id` 数据在存储层按 author_id 物理隔离；评测前的自测数据已清空
- 不依赖任何 Agent 框架（不依赖 Hermes 或其他编排层）：服务可在一个**干净环境**里独立启动
  （见 §5 的可复现证据）
- 版本冻结：提交后不再更换参评版本（规范要求）

### 2.4 稳定性承诺与到期

- 端点在本届评测期内保持公开可达、版本固定；承诺保持至 **2026-12-05**。
  （规范要求：已部署端点须在提交后**至少 30 天**保持公开可达与稳定。）
- 到期后由**自失效机制**自动拆除（服务停机 + 端口规则回收 + 数据目录归档），不留下需要人工收尾的残留。

## 3. 架构

```
AML HTTP (/add /search /health, X-Api-Key / Bearer / X-Token 鉴权)
   │
   ├─ Add: 事实切分 → 过时过滤 → 语义去重/合并（保留原分隔符） → 冷层写入 (author_id=user_id)
   ├─ Search: 样本内召回 (author_id 过滤, top_k≤100) → 时间衰减排序（只读：不刷新活性状态）→ AML 格式
   └─ Health: 2xx 存活探测（存储降级时仍 2xx，体内标注 degraded）
        │
        ▼
MemoryCore 冷存储客户端（per-user 引擎，进程内 SQLite + 向量索引）
        │
        ▼
embedding: ollama + 派生模型 qwen3-embedding-aml-ctx1024（FROM qwen3-embedding:0.6b；num_ctx 1024、全层 GPU；本机，1024 维，无外部 API）
```

## 4. 来源披露与改动说明

### 4.1 原始工作与许可证

| 组件 | 许可证 | 用途 |
|---|---|---|
| origin-memorycore（我方原始工作，MemoryCore 开源版） | MIT | 记忆治理层（冷热路由 / 去重 / 合并 / 溢流 / 回收站 / 衰减） |
| mnemosyne-memory 3.15.1 | MIT | 存储引擎（进程内 SQLite + 向量检索 + 多身份过滤） |
| ollama + qwen3-embedding:0.6b | MIT / Apache-2.0 | 本机 embedding（1024 维） |

### 4.2 本次方法改动（相对 origin-memorycore）

| 改动 | 位置 | 说明 |
|---|---|---|
| AML HTTP 适配层 | `memorycore/aml_server.py`（新增） | `/add` `/search` `/health`、请求校验、幂等、鉴权、异常到错误码的映射 |
| 身份透传 | `memorycore/cold_store_client.py` | 增加 `author_id` 等身份参数透传（默认 None，原行为不变） |
| 合并保真 | `memorycore/core/overflow.py` | 合并两条记忆时**保留原分隔符**（修掉"换行/分号被重拼为中文句号"导致代码/命令被拆坏的问题） |
| 去重门槛收紧 | `memorycore/core/overflow.py` | 仅对**接近逐字重复**的内容判重，避免不同事实被吞 |
| 只读召回 | `memorycore/aml_server.py` + 存储层补丁 | 检索不刷新活性时间戳（见 4.3） |
| 返回长度 | `memorycore/cold_store_client.py` | 单条证据返回上限 500 → **2000** 字符，支持 `MEMORYCORE_CONTENT_MAX_CHARS` 覆盖 |
| 存储目录 fail-closed | `memorycore/aml_server.py` | 数据目录缺失/不可写时显式报错退出，不静默回退到默认目录 |
| 统计口径 | `memorycore/cold_store_client.py` | `stats()` 增加 `all_sessions` 计数 |
| 依赖 | `pyproject.toml` | 增加 `uvicorn`（参考实现给出的可选依赖） |
| 嵌入缓存 | `mnemosyne/core/embeddings.py`（补丁） | 进程内 LRU 缓存（键 = 模型指纹 + 端点 + query/doc 前缀 + 文本哈希；条目与字节双上限，可关闭） |
| 批量嵌入 | `memorycore/aml_server.py` + 补丁 | 单请求内的片段文本批量嵌入（向量与单条完全一致，实测最大绝对差 0.0） |
| 两阶段 Add | `memorycore/aml_server.py` | 预计算阶段（无写入）+ 短写入阶段；请求预算只在无写入阶段触发；ledger 记录检查点并支持断点续跑 |
| 并发与背压 | `memorycore/aml_server.py` | 有界线程池 + 按 `user_id` 串行 + 嵌入并发闸 + 按字节加权准入；超限返回可重试 5xx |
| 存储层并发安全 | `memorycore/cold_store_client.py` | 共享引擎全局锁 + 每用户引擎锁；`database is locked` 有界重试 |
| 健康检查隔离 | `memorycore/aml_server.py` | `/health` 改为内存快照 + 后台探测线程（不访问存储层，毫秒级返回） |
| 数据目录身份守卫 | `memorycore/cold_store_client.py` | 运行时比对 `(st_dev, st_ino)`；数据目录被替换时重建引擎并丢弃旧连接，`/health` 暴露 `db_identity*` |
| 嵌入预取与写阶段隔离 | `memorycore/aml_server.py` + 补丁 | Phase A 预取全部向量，写阶段不再发起嵌入调用；缓存禁用时也复用预取结果，避免"200 写入但无向量" |
| 断点续跑与幂等 | `memorycore/aml_server.py` + `cold_store_client.py` | ledger 记录 `phase/checkpoint`，重试续跑剩余片段；已完成请求直接 replay 200；写前 exact 查重避免重放改状态 |

### 4.3 存储层补丁（明示披露）

- 组件：mnemosyne-memory 3.15.1（MIT）
- 补丁：`patches/mnemosyne-recall-readonly.patch`
- 内容：让存储引擎的召回接口**接受并尊重"只读召回"参数**（不刷新 `last_recalled` / `recall_count`）；
  **默认行为不变**——只有显式传入只读标志时才生效
- 理由：评测存在同一问题多次检索的场景（streaming / 多轮），需要**结果可复现**，
  而原实现的召回会顺带刷新活性状态，导致同一问题第二次检索排序漂移
- 复现：`patches/README.md` 给出应用方法、适用版本（3.15.1）与验证步骤

- 补丁二：`patches/mnemosyne-embed-cache.patch`
  - 内容：为嵌入调用增加**进程内 LRU 缓存**（键 = 模型指纹（tag/digest/ctx/GPU 层数）+ 端点 + query/doc 前缀 + 文本哈希；
    条目上限 8192、字节上限 64 MiB，可用环境变量关闭）
  - 理由：同一请求内同一文本会被重复嵌入（实测 60 次调用中 20 次为同一文本的重复），评测语料中亦有大量重复台词/模板句
  - 性质：**不改变检索语义**——相同输入必然返回同一向量（比每次重算更可复现）；批量与单条向量实测最大绝对差 0.0
  - 复现：`patches/README.md`（应用方法、适用版本 3.15.1、验证步骤与关闭开关）

### 4.4 关键参数（会影响评测结果的方法选择，如实披露）

- 事实片段上限 **300 字符**（写入侧按句子边界切分）
- Search 单条证据返回上限 **2000 字符**（`MEMORYCORE_CONTENT_MAX_CHARS` 可覆盖）
- `top_k` 上限 100；时间衰减半衰期 90 天
- 不使用任何外部 LLM 参与写入或检索；不生成答案

## 5. 复现步骤

**版本与固定 commit**：系统版本 `0.1.0`；参评代码固定 commit **`68a77c5aee451c13cc987be10f3b4efbfcd8e6b8`**
（部署端点的 `/health` 会返回同一 commit，可交叉核对；材料自身的文档提交不参与版本冻结）。

### 5.1 本地启动（裸机）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -i https://mirrors.aliyun.com/pypi/simple/ .
ollama serve & ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data AML_API_KEY=<your-key> python -m memorycore.aml_server
# 冒烟：curl http://localhost:8000/health
```

### 5.2 测试

```bash
MNEMOSYNE_DATA_DIR=$(mktemp -d) python -m pytest -q      # 全量 670 passed（2026-09-19）
MNEMOSYNE_DATA_DIR=$(mktemp -d) python tests/test_aml.py # 独立端到端断言
```

### 5.3 容量基准（可复跑）

```bash
python scripts/bench_add.py --concurrency 64 --messages 20 --rounds 3 --port 8870
```

### 5.4 干净环境证据（框架无关）

`scripts/smoke_clean_env.sh`：在 `env -i` 的干净环境中启动服务并跑通 Add / Search / Health，
证明参赛系统**不依赖任何 Agent 框架**。

### 5.5 Docker（可选，仅供本地复现）

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
```

> 说明：第二期不接受"仅提交仓库 / Docker 镜像、由平台代为部署"的形式；Docker 仅作为本地复现的便捷方式。

## 6. 已知边界（诚实声明）

- 写入侧事实片段上限 300 字符：超长消息会被切分为多个可独立检索的片段（取舍：保证单条记忆可独立命中，
  代价是超长证据被切散）。
- 消息自带的 `timestamp` 不作为事件时间，排序使用持久化时间（写入时间）；`created_at` 返回持久化时间。
- 不做 query 改写 / 多跳检索；开放题的召回依赖存储层的词法 + 向量混合排序（有词法相关性门禁）。
- 不做冷层全量治理巡检的在线触发（写入侧治理为去重 / 合并 / 过时过滤）。
- 单机 CPU 推理，容量上限即本机硬件能力（§2.2 给出实测数字）。
- 多模态与代码赛道本期不参加（本材料只覆盖文本记忆赛道）。

## 7. 环境变量（提交说明附表）

| 变量 | 默认 | 含义 |
|---|---|---|
| `AML_HOST` / `AML_PORT` | `0.0.0.0` / `8000` | HTTP 监听地址与端口 |
| `AML_API_KEY` | 空（不鉴权） | 设置后 `/add` `/search` 需鉴权（`X-Api-Key` / `Authorization: Bearer` / `X-Token`） |
| `MNEMOSYNE_DATA_DIR` | 无默认（**缺失即报错退出**） | 存储目录；评测请指向独立目录，避免与其它实例混用 |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | embedding 服务（本机 ollama 或 OpenAI 兼容端点） |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | embedding 模型（1024 维） |
| `MEMORYCORE_CONTENT_MAX_CHARS` | `2000` | Search 单条证据返回上限（非法值回落到 2000 并记 warning） |

## 8. 错误码语义

- `400` 格式错误（缺 `request_id` / `user_id` / `session_id`、`messages` 非数组等）
- `401` 鉴权失败（配置了 `AML_API_KEY` 时）
- `422` 请求语义错误（如 `top_k` 缺失/非法、`messages` 元素缺少合法 role）——规范中 4xx 表示格式错误且不重试
- `500` 存储后端不可用（如 embedding 服务不可达等临时异常；5xx 平台会重试）
- 合法请求**一律返回 200**（含整批被写入治理过滤的情况，此时体内标注治理计数）——
  不使用 202 / 任务 ID；Add 同步持久化完成后才返回 200





### 4.5 运行配置与行为边界（如实披露）

- **嵌入模型**：本机 ollama + `qwen3-embedding:0.6b` 的派生模型 **`qwen3-embedding-aml-ctx1024`**
  （`num_ctx=1024` + 全层 GPU 卸载）。派生模型与基础模型**同一份权重**，未更换模型。
- **上下文长度的影响**：写入侧的"事实片段"上限 300 字符，**不受该上下文长度影响**（实测与全上下文配置余弦 1.000000）；
  检索 query 不做切分，**超过约 1024 token 的尾部会被截断**，长文本实测余弦差异 <1.9%，
  在极接近的排序分数上可能导致顺序变化——这一点不影响写入结果，但**如实声明**。
- **嵌入缓存**：进程内 LRU 缓存（键含模型指纹、API 端点、query/doc 前缀与文本哈希；条目与字节双上限，
  可用环境变量关闭）。相同输入必然返回同一向量（比每次重算更可复现）；批量与单条向量实测最大绝对差 0.0。
- **批量嵌入**：同一请求内的片段文本合并为批量调用（向量与单条完全一致，实测最大绝对差 0.0）。
- **并发与背压**：Add/Search 的处理在有界线程池中执行，按 `user_id` 串行；嵌入调用有全局并发上限；
  按请求体字节与请求数双重限流，超限返回**可重试的 5xx**（不静默丢弃）。
- **单请求预算**：默认 600 秒；超预算在**未写入阶段**返回可重试 5xx；**已进入写入阶段的请求不会被中断**，
  因此不会留下半写状态（同一 `request_id` 重试与一次成功执行的结果一致）。
- **健康检查**：`/health` 读取内存快照，不访问存储层，**任何情况下 1 秒内返回**。`status` 只反映真故障（存储不可用 / 数据目录身份不一致 / 探测异常）；模型驻留状态与容量提示另见 `embedding_gpu_resident` 与 `capacity_warning`（闲置时模型未驻留属正常，首次调用自动加载）。
- **容量声明**：见 §2.2（64 并发 Add / 32 并发 Search 的实测数字）。

