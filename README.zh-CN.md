# MemoryCore — AML 在线治理参赛版

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore** 是面向 LLM Agent 的记忆治理层: Agent 积累记忆的速度很快——偏好、事实、决策——而不治理的记忆会静默退化。MemoryCore 让热层保持在预算内、冷层保持可检索、每次写入都经去重与合并。

本仓库是 **Agent Memory Leaderboard (AML) 参赛版**。它把 MemoryCore 已验证的治理机制**在线化**, 直接跑在 AML 的写入/检索路径上:

- **写入治理(`/add`)** —— 消息 → 事实切分 → 过时过滤 → 语义去重/合并 → 冷层写入(按身份隔离)。
- **检索治理(`/search`)** —— 样本内召回(top_k 至 100)→ 相似度 × 时间衰减排序(半衰期 90 天, 重要记忆不衰减)→ AML 格式证据。
- **严格样本隔离** —— `user_id` 一对一映射存储层 `author_id`; 跨用户检索不到任何记忆(SQL 层过滤, 测试覆盖)。
- **评测期零外部 API 调用** —— embedding 本地运行(ollama + `qwen3-embedding:0.6b`, 1024 维), 存储为进程内 SQLite 引擎([mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/))。

上游开源项目: [moonandecho/origin-memorycore](https://github.com/moonandecho/origin-memorycore)(MIT)。本仓库在其之上新增 AML HTTP 适配层 `memorycore/aml_server.py` 与按身份( author_id )的透传隔离。

**完整方法披露 / 部署说明 / 可复现测试 / 诚实边界声明: [AML-COMPETITION.md](AML-COMPETITION.md)**(英文版: [AML-COMPETITION.en.md](AML-COMPETITION.en.md))。

## 快速开始

### Docker(推荐)

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
```

> 镜像已在构建期预置 embedding 模型(`qwen3-embedding:0.6b`, 约 639MB)——容器启动无需联网。entrypoint 保留兜底: 仅当模型缺失(如自定义 `MEMORYCORE_EMBED_MODEL`)时才在运行时联网拉取。数据卷 `/data` 持久化 SQLite 记忆库, 重启不丢。

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
pip install .
ollama serve &
ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data python -m memorycore.aml_server   # 默认 0.0.0.0:8000
```

## HTTP API

全部为纯 REST 端点, 挂载在 FastMCP streamable-http 应用上(uvicorn)。可选鉴权: 设置 `AML_API_KEY` 后支持 Bearer / Token / X-Api-Key。

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

## 架构

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

底层治理层(冷热路由 / 写入时归一化去重 / 容量控制 / 溢流 / 回收站)的完整说明见[上游项目](https://github.com/moonandecho/origin-memorycore)。

## 测试(可复现)

```bash
# 需 ollama + qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=$(mktemp -d) python3 tests/test_aml.py
```

28 项断言覆盖: 跨 user_id 隔离(A 写 B 查不到)、同一事实二次写入去重、"方案 A → 改为 B"合并为一条、/add /search /health HTTP 全链路、options 兜底召回、长消息切分、错误码。

## 已知边界(诚实声明)

- 检索走存储层词法+向量混合排序, 存储层对长查询有词法相关性门禁; 选择题场景由 options 兜底召回覆盖, 开放题(英文自然问句)实测可正常召回。
- 消息自带 timestamp 仅作参考, 排序使用持久化时间(写入时间); created_at 返回持久化时间(协议允许的"来源/持久化时间")。
- 不做冷层全量治理巡检(维护性批处理)在线触发, 写入侧治理以去重/合并/过时过滤为主。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。引擎依赖 [mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/) 与 [ollama](https://ollama.com) + qwen3-embedding 为 MIT / Apache-2.0。逐组件改动披露见 [AML-COMPETITION.md](AML-COMPETITION.md)。
