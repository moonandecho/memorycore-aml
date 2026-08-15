# AML 适配层测试结果（可复现）

运行环境: 本仓库 + mnemosyne-memory 3.15.1 + ollama(qwen3-embedding:0.6b, 本地)
运行命令:
    MNEMOSYNE_DATA_DIR=$(mktemp -d) \
    MEMORYCORE_EMBED_URL=http://localhost:11434/v1 \
    MEMORYCORE_EMBED_MODEL=qwen3-embedding:0.6b \
    python3 tests/test_aml.py

最近一次完整输出:
```
Seeded config.yaml at /tmp/tmp.E1C79FFoqp/config.yaml (106 keys, 4 from env vars)
/home/echo/D/origin-memorycore/tests/test_aml.py:122: StarletteDeprecationWarning: Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
  from starlette.testclient import TestClient
StreamableHTTP session manager started
HTTP Request: GET http://testserver/health "HTTP/1.1 200 OK"
HTTP Request: POST http://testserver/add "HTTP/1.1 200 OK"
HTTP Request: POST http://testserver/search "HTTP/1.1 200 OK"
HTTP Request: POST http://testserver/search "HTTP/1.1 200 OK"
HTTP Request: POST http://testserver/search "HTTP/1.1 200 OK"
HTTP Request: POST http://testserver/add "HTTP/1.1 400 Bad Request"
HTTP Request: POST http://testserver/add "HTTP/1.1 200 OK"
HTTP Request: POST http://testserver/search "HTTP/1.1 200 OK"
StreamableHTTP session manager shutting down
  PASS  import memorycore
  PASS  ColdStoreClient init (embedding probe)
  PASS  write author A
  PASS  author B cannot see author A's memory
  PASS  author A sees own memory
  PASS  author A result is the coffee fact
  PASS  same fact twice → 2nd is duplicate/updated
  PASS  duplicate fact stored exactly once
  PASS  方案A stored, 方案A改为B merged
  PASS  merge → single cache-plan entry (not two)
  PASS  merged entry contains the newer fact
  PASS  GET /health → 200
  PASS  POST /add → 200
  PASS  add echoes success+3 IDs
  PASS  POST /search → 200
  PASS  search returns {data:[...]}
  PASS  search data ≤ top_k
  PASS  search item fields (id/content/score)
  PASS  search with content-word query returns evidence
  PASS  HTTP search: other user sees nothing
  PASS  POST /add missing fields → 400
  PASS  options-aware search finds evidence
  PASS  options-aware result is the engineer fact
  PASS  long message split into fragments

24 passed, 0 failed
```
