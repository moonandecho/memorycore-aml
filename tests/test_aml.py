#!/usr/bin/env python3
"""test_aml.py — AML adapter end-to-end tests (isolation / governance / HTTP)

Covers:
  1. user_id isolation: author A write → author B recall returns nothing
  2. online governance: same fact twice → duplicate; "方案A" + "方案A改为B"
     → merged into ONE entry
  3. HTTP smoke: /add /search /health full chain, AML response shapes
  4. top_k=100 recall cap + AML field format

Usage (requires ollama with qwen3-embedding:0.6b):
  MNEMOSYNE_DATA_DIR=$(mktemp -d) \
  python3 tests/test_aml.py
"""
import os
import sys
import tempfile
import json
import shutil

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        print(f"  PASS  {name}")
        passed += 1
    else:
        print(f"  FAIL  {name}")
        if detail:
            print(f"        {detail}")
        failed += 1


def main():
    global passed, failed

    # ---- isolated data dir + embedding env BEFORE importing memorycore ----
    tmpdir = tempfile.mkdtemp(prefix="aml-test-")
    os.environ.setdefault("MNEMOSYNE_DATA_DIR", tmpdir)
    os.environ.setdefault("MEMORYCORE_EMBED_URL", "http://localhost:11434/v1")
    os.environ.setdefault("MEMORYCORE_EMBED_MODEL", "qwen3-embedding:0.6b")
    os.environ.setdefault("MNEMOSYNE_EMBEDDING_API_URL",
                          os.environ["MEMORYCORE_EMBED_URL"])
    os.environ.setdefault("MNEMOSYNE_EMBEDDING_MODEL",
                          os.environ["MEMORYCORE_EMBED_MODEL"])
    os.environ.pop("AML_API_KEY", None)  # smoke mode

    try:
        from memorycore.cold_store_client import ColdStoreClient
        from memorycore.aml_server import _store_fragment, _split_fragments
    except Exception as e:
        check("import memorycore", False, f"{type(e).__name__}: {e}")
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1

    check("import memorycore", True)

    try:
        client = ColdStoreClient()
        check("ColdStoreClient init (embedding probe)", True)
    except Exception as e:
        check("ColdStoreClient init (embedding probe)", False, str(e))
        shutil.rmtree(tmpdir, ignore_errors=True)
        return 1

    UA = "aml:user-A"
    UB = "aml:user-B"

    # ---- 1. isolation: write as A, recall as B → nothing ------------------
    r = client.remember("用户A喜欢喝美式咖啡，每天上午一杯", importance=0.6,
                        author_id=UA)
    check("write author A", r.get("status") == "stored", str(r))

    res_b = client.recall_results("咖啡偏好", top_k=10, author_id=UB)
    check("author B cannot see author A's memory", len(res_b) == 0,
          f"got {len(res_b)} results: {res_b}")

    res_a = client.recall_results("咖啡偏好", top_k=10, author_id=UA)
    check("author A sees own memory", len(res_a) >= 1, str(res_a))
    check("author A result is the coffee fact",
          any("美式" in (x.get("content") or "") for x in res_a))

    # ---- 2. governance: duplicate ----------------------------------------
    fact = "部署方案：冷层使用进程内 SQLite 引擎存储"
    s1 = _store_fragment(client, fact, UA)
    s2 = _store_fragment(client, fact, UA)
    check("same fact twice → 2nd is duplicate/updated",
          s1.get("status") == "stored" and
          s2.get("status") in ("duplicate", "updated"),
          f"s1={s1} s2={s2}")

    dup_res = client.recall_results("冷层 SQLite 部署", top_k=100, author_id=UA)
    dup_cnt = sum(1 for x in dup_res if "SQLite" in (x.get("content") or ""))
    check("duplicate fact stored exactly once", dup_cnt == 1,
          f"found {dup_cnt} copies: {dup_res}")

    # ---- 3. governance: merge ("方案A" then "方案A改为B") -----------------
    plan = "缓存方案A：使用 Redis 做会话缓存"
    plan2 = "缓存方案A改为B：使用内存缓存替代 Redis"
    m1 = _store_fragment(client, plan, UA)
    m2 = _store_fragment(client, plan2, UA)
    check("方案A stored, 方案A改为B merged",
          m1.get("status") == "stored" and m2.get("status") == "updated",
          f"m1={m1} m2={m2}")

    merge_res = client.recall_results("缓存方案", top_k=100, author_id=UA)
    merge_cnt = sum(1 for x in merge_res if "缓存" in (x.get("content") or ""))
    check("merge → single cache-plan entry (not two)",
          merge_cnt == 1, f"found {merge_cnt}: {merge_res}")
    check("merged entry contains the newer fact",
          any("B" in (x.get("content") or "") and "内存缓存" in (x.get("content") or "")
              for x in merge_res), str(merge_res))

    # ---- 4. HTTP smoke: /health /add /search -----------------------------
    try:
        from starlette.testclient import TestClient
        from memorycore.aml_server import mcp
        app = mcp.streamable_http_app()
        with TestClient(app) as c:
            h = c.get("/health")
            check("GET /health → 200", h.status_code == 200,
                  f"{h.status_code} {h.text}")

            add_body = {
                "request_id": "eval:test:conv-0:chunk-0",
                "messages": [
                    {"role": "user", "timestamp": 1704067200000,
                     "content": "我最近在写一个记忆治理系统。"},
                    {"role": "assistant", "timestamp": 1704067260000,
                     "content": "好的，我会记住你在写记忆治理系统。"},
                ],
                "user_id": "eval:test:user-http",
                "session_id": "eval:test:sample:0",
            }
            a = c.post("/add", json=add_body)
            check("POST /add → 200", a.status_code == 200,
                  f"{a.status_code} {a.text}")
            if a.status_code == 200:
                aj = a.json()
                check("add echoes success+3 IDs",
                      aj.get("success") is True and
                      aj.get("request_id") == add_body["request_id"] and
                      aj.get("user_id") == add_body["user_id"] and
                      aj.get("session_id") == add_body["session_id"],
                      str(aj))

            s = c.post("/search", json={
                "query": "这个人在做什么项目",
                "options": [],
                "user_id": "eval:test:user-http",
                "top_k": 100,
            })
            check("POST /search → 200", s.status_code == 200,
                  f"{s.status_code} {s.text}")
            if s.status_code == 200:
                sj = s.json()
                check("search returns {data:[...]}", isinstance(sj.get("data"), list),
                      str(sj))
                if isinstance(sj.get("data"), list):
                    check("search data ≤ top_k", len(sj["data"]) <= 100,
                          f"{len(sj['data'])}")
                    for item in sj["data"]:
                        ok_keys = (isinstance(item.get("id"), str) and item["id"]
                                   and isinstance(item.get("content"), str)
                                   and item["content"]
                                   and isinstance(item.get("score"), (int, float)))
                        if not ok_keys:
                            check("search item fields (id/content/score)",
                                  False, str(item))
                            break
                    else:
                        check("search item fields (id/content/score)", True)

            # same-user query sharing content words must hit (≥1 evidence)
            s_hit = c.post("/search", json={
                "query": "记忆治理系统",
                "user_id": "eval:test:user-http",
                "top_k": 100,
            })
            check("search with content-word query returns evidence",
                  s_hit.status_code == 200 and
                  len(s_hit.json().get("data", [])) >= 1,
                  f"{s_hit.status_code} {s_hit.text}")

            # cross-user isolation over HTTP
            s_other = c.post("/search", json={
                "query": "记忆治理系统", "user_id": "eval:test:other-user",
                "top_k": 100})
            check("HTTP search: other user sees nothing",
                  s_other.status_code == 200 and
                  s_other.json().get("data") == [],
                  f"{s_other.status_code} {s_other.text}")

            # format errors
            bad = c.post("/add", json={"request_id": "x"})
            check("POST /add missing fields → 400",
                  bad.status_code == 400, f"{bad.status_code} {bad.text}")

            # options-aware fallback: 协议示例式查询 + 选项
            en_add = {
                "request_id": "eval:test:en:0",
                "messages": [
                    {"role": "user",
                     "content": "The user is a software engineer."},
                ],
                "user_id": "eval:test:user-en",
                "session_id": "eval:test:sample:1",
            }
            c.post("/add", json=en_add)
            s2 = c.post("/search", json={
                "query": "Which answer best matches the memory?",
                "options": ["A. software engineer", "B. teacher", "C. doctor"],
                "user_id": "eval:test:user-en",
                "top_k": 100,
            })
            check("options-aware search finds evidence",
                  s2.status_code == 200 and len(s2.json().get("data", [])) >= 1,
                  f"{s2.status_code} {s2.text}")
            if s2.status_code == 200 and s2.json().get("data"):
                check("options-aware result is the engineer fact",
                      any("software engineer" in (d.get("content") or "")
                          for d in s2.json()["data"]),
                      str(s2.json()["data"]))
    except Exception as e:
        check("HTTP smoke (TestClient)", False, f"{type(e).__name__}: {e}")

    # ---- 5. fragment splitting -------------------------------------------
    long_msg = "第一件事。第二件事！第三件事？" * 30  # > 300 chars
    frags = _split_fragments(long_msg)
    check("long message split into fragments",
          len(frags) > 1 and all(len(f) <= 300 for f in frags),
          f"{len(frags)} frags, max {max(len(f) for f in frags)}")

    print(f"\n{passed} passed, {failed} failed")
    shutil.rmtree(tmpdir, ignore_errors=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
