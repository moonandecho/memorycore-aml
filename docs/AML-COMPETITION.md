# AML 第二期参赛材料草稿 — MemoryCore（记忆治理在线化路线）

> 本文件为参赛提交的 README 草稿：方法披露 / 架构 / 部署 / 测试。
> 提交方式：开源方法榜 + 学术·代码（GitHub 仓库 + Docker 启动，平台构建评测）。

## 一、方法说明（技术路线）

MemoryCore 是一个面向 LLM Agent 的记忆治理层（MIT 开源）。
本期参赛走**差异化路线：把记忆治理机制在线化**——不做多跳检索补全
（query 改写/迭代搜索），只把 MemoryCore 已验证的治理能力搬到 AML 协议的
写入与检索路径上：

- **写入治理（Add 在线执行）**
  - 事实切分：消息按句子边界切成自包含事实片段（长消息拆段，短消息整存）
  - 过时过滤：过时状态记录（"已修复"式短标记）不写入
  - 语义去重/合并：写入前在样本范围内召回候选，同一事实跳过；
    相似事实合并进同一条（"方案 A" 与 "方案 A 改为 B" 最终是一条记忆，
    不是两条）——合并以字面相似度为主信号（实测 embedding 分数对同词汇域
    短句虚高，纯语义门禁会误合并无关事实）
  - 时序标注：每条记忆落库即带持久化时间戳，参与检索排序
- **检索治理（Search 在线执行）**
  - 样本内召回（top_k 放开到 100），按"相似度 × 时间衰减"排序输出
    （半衰期 90 天，重要记忆不衰减——久未提及的事实自然降权，
    新近事实优先被看到）
  - options 兜底召回：选择题的候选项拼接进检索查询做一次兜底召回
    （不是迭代搜索；options 只用于检索上下文，不生成答案、不写入记忆）

**样本隔离（硬约束）**：user_id 一对一映射到存储层的 author_id，
写入、去重召回、检索全程强制携带。存储层在 SQL 层按 author_id 过滤，
写入去重也按用户隔离——跨 user_id 检索不到任何记忆（测试覆盖）。

**不做的事（边界声明）**：不做 query 改写/多跳检索、不调用外部 LLM 生成
答案、Search 只返回记忆证据原文。

## 二、架构

```
AML HTTP (FastMCP custom routes: /add /search /health, 可选 Bearer/Token/X-Api-Key)
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

## 三、代码改动来源披露（学术·代码硬要求）

本项目基于以下开源组件，全部改动如下：

| 组件 | 许可证 | 用途 | 我方改动 |
|---|---|---|---|
| origin-memorycore（本仓库） | MIT | 治理层（冷热路由/去重/溢流/回收站/衰减） | 新增 AML HTTP 适配层 `memorycore/aml_server.py`；`cold_store_client.py` 增加 author_id 等身份参数透传（默认 None，原行为不变）；`stats()` 增加 all_sessions 计数；依赖增加 uvicorn |
| mnemosyne-memory 3.15.1 | MIT | 存储引擎（进程内 SQLite + sqlite-vec 向量检索 + 多身份过滤） | 不改动库代码，仅通过其原生 author_id / recall 过滤参数实现样本隔离 |
| ollama + qwen3-embedding:0.6b | MIT / Apache-2.0 | 本地 embedding（1024 维） | 无改动 |

- 作者：moonandecho
- 仓库：https://github.com/moonandecho/origin-memorycore
- 参赛固定 commit：见提交记录（模块化提交：身份透传 / AML 适配层 / Docker）

## 四、部署

### Docker（推荐，平台构建评测用）

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
# 冒烟:
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

容器内自带 ollama 并在启动时拉取 embedding 模型；
数据卷 `/data` 持久化 SQLite 记忆库。

### 裸机

```bash
pip install .
ollama serve &
ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data python -m memorycore.aml_server   # 默认 0.0.0.0:8000
```

### 环境变量

| 变量 | 默认 | 含义 |
|---|---|---|
| AML_HOST / AML_PORT | 0.0.0.0 / 8000 | HTTP 监听 |
| AML_API_KEY | 空（不鉴权，smoke 模式） | 设置后 Add/Search 需 Bearer/Token/X-Api-Key |
| MNEMOSYNE_DATA_DIR | ~/.memorycore/data | SQLite 数据目录（评测请指向独立目录） |
| MEMORYCORE_EMBED_URL | http://localhost:11434/v1 | embedding API（ollama 或 OpenAI 兼容） |
| MEMORYCORE_EMBED_MODEL | qwen3-embedding:0.6b | embedding 模型（1024 维） |

### 错误码语义

- 400/422 格式错误（缺 request_id/user_id/session_id、messages 非数组等）
- 401 认证失败（配置了 AML_API_KEY 时）
- 500 存储后端不可用（embedding 服务宕机等临时异常，平台按 5xx 自动重试）
- 不返回 202/任务 ID；Add 同步完成后才返回 200

## 五、测试（可复现）

```bash
# 需 ollama + qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=$(mktemp -d) python3 tests/test_aml.py
```

24 项断言覆盖：跨 user_id 隔离（A 写 B 查不到）、同一事实二次写入去重、
"方案 A → 改为 B"合并为一条、/add /search /health HTTP 全链路、
options 兜底召回、长消息切分、错误码。

## 六、已知边界（诚实声明）

- 检索走存储层词法+向量混合排序，存储层对长查询有词法相关性门禁；
  选择题场景由 options 兜底召回覆盖，开放题（英文自然问句）实测可正常召回。
- 消息自带 timestamp 仅作参考，排序使用持久化时间（写入时间）；
  created_at 返回持久化时间（协议允许的"来源/持久化时间"）。
- 不做冷层全量治理巡检（维护性批处理）在线触发，写入侧治理以去重/合并/过时过滤为主。
