#!/usr/bin/env python3
"""MemoryCore MCP server — memory tiering for LLM agents

Provides: cold/hot routing (store_fact) / capacity control / six-step
overflow (trigger_overflow) / cold-tier maintenance
(run_cold_storage_maintenance) / health status (get_memory_usage).

Internals: reads local MEMORY.md/USER.md (local_store), talks to a remote
MCP memory service via ColdStoreClient (cold tier).
"""
import json

from mcp.server.fastmcp import FastMCP  # noqa: E402

from .local_store import LocalStore  # noqa: E402
from .cold_store_client import ColdStoreClient  # noqa: E402
from .core.config import SOFT_THRESHOLD, HARD_THRESHOLD, TARGET_RATIO, COLD_SOFT_LIMIT, COLD_HARD_LIMIT  # noqa: E402
from .core.classifier import classify, COLD, STALE  # noqa: E402
from .core.overflow import run_overflow, _recall_safe, _find_best_match, _merge_two_entries  # noqa: E402
from .core.maintenance import run_maintenance  # noqa: E402

mcp = FastMCP("memorycore")

_store = LocalStore()
_client = ColdStoreClient()

def _targets(target: str):
    if target == "user":
        return ["user"]
    if target == "both":
        return ["memory", "user"]
    return ["memory"]


def _force_overflow_to_target(t: str) -> None:
    """硬阈值 (>=80%) 强制全量溢流: 循环执行直到本地占用 ≤40% 或无可下沉。

    与软阈值单次溢流不同, 硬阈值语义是"必须把本地降到安全区间",
    循环上限 3 次防止异常死循环 (每次 run_overflow 都会处理所有
    可下沉条目, 正常 1-2 轮即达目标)。
    """
    for _ in range(3):
        before = _store.usage_pct(t)
        run_overflow(_store, _client, t)
        after = _store.usage_pct(t)
        if after <= TARGET_RATIO * 100 or after >= before:
            return


def _check_cold_capacity() -> None:
    """Cold-tier capacity hard gate: check total entries before writing (Task C).

    - > HARD_LIMIT: force maintenance in a loop until under SOFT_LIMIT (max 5)
    - > SOFT_LIMIT: run one maintenance pass before continuing
    - cold tier unreachable: skip (never block the write)
    """
    try:
        stats = _client.stats()
    except Exception:
        return  # cold tier unreachable → degrade, skip

    cold_total = stats.get("total", 0)

    if cold_total > COLD_HARD_LIMIT:
        # force maintenance to shrink, max 5 rounds
        for _ in range(5):
            run_maintenance(_client)
            try:
                stats = _client.stats()
                cold_total = stats.get("total", 0)
            except Exception:
                break
            if cold_total <= COLD_SOFT_LIMIT:
                break
    elif cold_total > COLD_SOFT_LIMIT:
        # one maintenance pass before continuing
        run_maintenance(_client)


# ---------------------------------------------------------------------------
# 工具 1: store_fact — 写入统一入口
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_store_fact(content: str, importance: float = 0.8, scope: str = "global", target: str = "memory") -> str:
    """记忆写入统一入口: 冷热分流 + 容量校验。

    Args:
        content: 要记忆的事实 (一句话, 中文, 主语清晰)
        importance: 0.0-1.0 重要度 (>=0.8 倾向热数据留本地)
        scope: 'global' 或 'session'
        target: 热数据写入本地哪个文件 ('memory' 或 'user')
    Returns:
        JSON: {"status": "stored"|"cold_stored"|"stale"|"error", "detail": "..."}
    """
    try:
        decision = classify(content, importance=importance)
        d = decision["decision"]

        if d == STALE:
            return json.dumps({"status": "stale", "detail": decision["reason"],
                               "note": "过时状态记录, 不迁移不写入"}, ensure_ascii=False)

        if d == COLD:
            # Task C: capacity hard gate — check cold-tier size before writing
            _check_cold_capacity()

            # Task A: cold data → dedup-check first, avoid cross-layer duplicates
            try:
                existing = _recall_safe(_client, content)
            except Exception as e:
                return json.dumps({"status": "error",
                                   "detail": f"cold tier unreachable, write failed: {e}"},
                                  ensure_ascii=False)

            if existing:
                matched = _find_best_match(content, existing)
                if matched:
                    if matched["level"] == "same":
                        return json.dumps({"status": "cold_duplicate",
                                           "detail": "cold tier already has this fact, skip"},
                                          ensure_ascii=False)
                    elif matched["level"] == "similar":
                        merged = _merge_two_entries(content, matched["content"])
                        try:
                            r = _client.update(matched["id"], merged)
                            if r.get("status") == "updated":
                                return json.dumps({"status": "cold_updated",
                                                   "memory_id": matched["id"],
                                                   "detail": "merged into existing cold entry"},
                                                  ensure_ascii=False)
                            return json.dumps({"status": "error",
                                               "detail": f"update merge failed: {r}"},
                                              ensure_ascii=False)
                        except Exception as e:
                            return json.dumps({"status": "error",
                                               "detail": f"update merge exception: {e}"},
                                              ensure_ascii=False)

            # no match → remember (original logic)
            r = _client.remember(content, importance=importance, scope=scope)
            if r.get("status") == "stored":
                return json.dumps({"status": "cold_stored", "memory_id": r.get("memory_id"),
                                   "detail": "低频事实已下沉冷层"}, ensure_ascii=False)
            return json.dumps({"status": "error", "detail": f"cold tier write failed: {r}"},
                              ensure_ascii=False)

        # HOT: 先查容量再写本地
        #   软阈值 (>=60%): 先溢流一次降压再写
        #   硬阈值 (>=80%): 强制全量溢流至 ≤40% (循环, 有上限保护) 再写
        t = target if target in ("memory", "user") else "memory"
        usage = _store.usage_pct(t)
        if usage >= HARD_THRESHOLD * 100:
            _force_overflow_to_target(t)
        elif usage >= SOFT_THRESHOLD * 100:
            run_overflow(_store, _client, t)
        r = _store.add(t, content)
        if r.get("success"):
            return json.dumps({"status": "stored", "target": t,
                               "usage_after": f"{_store.usage_pct(t)}%",
                               "detail": "热数据已写本地"}, ensure_ascii=False)
        # 写本地失败 (超限等) → 降级写冷层, 不丢失
        rr = _client.remember(content, importance=importance, scope=scope)
        if rr.get("status") == "stored":
            return json.dumps({"status": "cold_stored_fallback", "memory_id": rr.get("memory_id"),
                               "detail": f"本地写入失败({r.get('error')}), 降级写冷层"}, ensure_ascii=False)
        return json.dumps({"status": "error", "detail": f"本地写失败且冷层降级失败: {r}"},
                          ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "detail": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 2: trigger_overflow — 六步溢流
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_trigger_overflow(target: str = "both") -> str:
    """执行六步溢流流程, 将本地 memory 降至安全区间 (≤40%)。

    Args:
        target: 'memory' | 'user' | 'both' (默认 both)
    Returns:
        JSON: 溢流统计 {overflowed, updated, deleted, merged, usage_after}
    """
    try:
        results = {}
        for t in _targets(target):
            results[t] = run_overflow(_store, _client, t)
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 3: run_cold_storage_maintenance — 冷层治理 (阶段2完整, 此处骨架)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_run_cold_storage_maintenance() -> str:
    """冷层全量治理: 合并/清理/冲突取舍/向量校验。"""
    try:
        result = run_maintenance(_client)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 4: get_memory_usage — 健康状态
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_get_memory_usage() -> str:
    """获取本地 (双文件) / 冷层占用与健康状态。"""
    try:
        cold = _client.stats()
    except Exception as e:
        # 冷层不可达 → 降级: 本地状态照常返回, 冷层标记 error
        # (Task 3.1 实测: 修复前整体报错, agent 连本地占用都看不到)
        cold = {"error": str(e)}
    return json.dumps({
        "local": {
            "memory": {"chars": _store.char_count("memory"),
                       "pct": _store.usage_pct("memory"),
                       "limit": 5000},
            "user": {"chars": _store.char_count("user"),
                     "pct": _store.usage_pct("user"),
                     "limit": 5000},
        },
        "cold": cold,
        "thresholds": {
            "soft_60": 3000, "hard_80": 4000, "target_40": 2000,
        },
    }, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 5: memorycore_recall — active cold-tier recall (read-only)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_recall(query: str, top_k: int = 3) -> str:
    """Actively recall cold-tier memories (read-only; complements the
    per-turn top-3 prefetch with on-demand manual queries).

    Args:
        query: natural-language semantic query
        top_k: number of results (default 3, max 10)
    Returns:
        JSON: {"results": [{"id": ..., "content": ..., "score": ...}]}
              or {"error": "..."} when the cold tier is unreachable.
    """
    try:
        k = max(1, min(int(top_k), 10))
        results = _client.recall_results(query, top_k=k)
        return json.dumps({"results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
