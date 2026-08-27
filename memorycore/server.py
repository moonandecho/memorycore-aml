#!/usr/bin/env python3
"""MemoryCore MCP server — memory tiering for LLM agents

Provides: cold/hot routing (store_fact) / capacity control / six-step
overflow (trigger_overflow) / cold-tier maintenance
(run_cold_storage_maintenance) / health status (get_memory_usage).

Internals: reads local MEMORY.md/USER.md (local_store), talks to a remote
MCP memory service via ColdStoreClient (cold tier).
"""
import json
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP  # noqa: E402

from .local_store import LocalStore  # noqa: E402
from .cold_store_client import ColdStoreClient  # noqa: E402
from .core.config import (SOFT_THRESHOLD, HARD_THRESHOLD, TARGET_RATIO, COLD_SOFT_LIMIT, COLD_HARD_LIMIT,  # noqa: E402
                         STATE_TTL_DAYS, RULE_COMPRESS_DAYS)
from .core.classifier import (classify, classify_user_pref, should_keep_local,  # noqa: E402
                             classify_entry_type, COLD, STALE)
from .core.metadata import MetaStore, entry_age_days, log_activity_query, _parse_iso  # noqa: E402  # Phase 3 S4
from .core.overflow import (run_overflow, _recall_safe, _find_best_match,  # noqa: E402
                           _merge_two_entries, _is_protected_rule,
                           enforce_rule_budget, apply_activity_hits,
                           restore_stubs_from_results, _rule_weight_eff)
from .core.config import RULE_MIN_RESIDENCY_DAYS, RULE_BUDGET_CHARS, AUDIT_SINK_WEIGHT_THRESHOLD, AUDIT_SINK_INACTIVE_DAYS, MAX_EVICT_PER_RUN  # noqa: E402
from .core.maintenance import run_maintenance  # noqa: E402
from .core.decay import _apply_decay  # noqa: E402  # shared by recall + prefetch

mcp = FastMCP("memorycore")

_store = LocalStore()
_client = ColdStoreClient()

def _targets(target: str):
    if target == "user":
        return ["user"]
    if target == "both":
        return ["memory", "user"]
    return ["memory"]


def _metastore_for(target: str) -> MetaStore:
    """Build a sidecar MetaStore following _store paths (tests can inject)."""
    return MetaStore(target, memory_path=_store.memory_path,
                     user_path=_store.user_path)


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
        # A write routing: target="user" uses classify_user_pref (USER.md governance)
        is_user = (target == "user")
        if is_user:
            d = classify_user_pref(content, importance=importance,
                                   sentence_level=False)
            stale_reason = None  # classify_user_pref returns no reason dict
        else:
            decision = classify(content, importance=importance)
            d = decision["decision"]
            stale_reason = decision.get("reason", "")

        if d == STALE or d == "stale":
            if is_user:
                return json.dumps({"status": "stale",
                                   "detail": "stale status record (USER.md), not written"},
                                  ensure_ascii=False)
            return json.dumps({"status": "stale", "detail": stale_reason,
                               "note": "过时状态记录, 不迁移不写入"}, ensure_ascii=False)

        go_cold = (is_user and d == "sink") or (not is_user and d == COLD)
        if not go_cold and classify_entry_type(content) == "state":
            # Phase 2 (2026-08-16): write-entry linkage — content judged
            # hot/core but typed as a historical decision/status record
            # (date + completion marker, no behavior instructions) is forced
            # to the cold path so pollution never enters the hot tier.
            # This overrides the default importance=0.8 hot shortcut without
            # touching classify itself.
            go_cold = True
        if go_cold:
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
                cold_detail = "USER.md long-tail fact stored to cold tier" if is_user else "低频事实已下沉冷层"
                return json.dumps({"status": "cold_stored", "memory_id": r.get("memory_id"),
                                   "detail": cold_detail}, ensure_ascii=False)
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
            # Phase 2: stamp sidecar metadata after a successful hot write
            # Phase 3 S6: importance passes through (protection line;
            # missing field defaults to 0.8, backward compatible)
            try:
                _metastore_for(t).stamp(content, "rule",
                                        importance=importance,
                                        origin="store_fact")
            except Exception:
                pass  # metadata failure never blocks the write (reconcile re-stamps)
            # Phase 4: post-write rule budget check (LRU eviction; new rules
            # have 7-day residency so they are never evicted immediately)
            try:
                enforce_rule_budget(_store, _client, t, _metastore_for(t), {})
            except Exception:
                pass  # budget enforcement failure never blocks the write
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
# Tool 5: memorycore_memory_audit — hot-tier health check (read-only)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_memory_audit(target: str = "both") -> str:
    """Hot-tier health check (read-only, never modifies): list every entry
    with its keep/sink classification, char count, and Phase 2 metadata
    (type / written_at / age_days / retirement plan). Used to diagnose
    overflow no-ops — when the hot tier is full of historical records,
    sink_candidates > 0 means overflow has something to sink."""
    result = {}
    for t in _targets(target):
        usage = _store.usage_pct(t)
        entries = _store.entries(t)
        meta = _metastore_for(t)
        rows = []
        sink_total = 0
        sink_chars = 0
        lru_sink = 0  # 2026-08-28: activity-dimension sink candidates (new field, keeps legacy counters intact)
        for e in entries:
            keep = should_keep_local(e)  # keyword view (legacy fallback)
            m = meta.get_entry(e)
            row = {"keep": keep, "chars": len(e), "text": e[:40]}
            # Phase 2: metadata columns (type / written_at / age / plan)
            if m:
                row["type"] = m.get("type")
                row["written_at"] = m.get("written_at")
                age = entry_age_days(m)
                row["age_days"] = age
                if m.get("type") == "state":
                    if age is not None and age >= STATE_TTL_DAYS:
                        row["plan"] = "sink_now"
                        keep = False
                    else:
                        row["plan"] = f"sink_in_{STATE_TTL_DAYS - (age or 0)}d"
                        keep = True  # not expired: still kept for now
                elif m.get("type") == "stub":
                    # Phase 3 S4: pointer stays; oldest-first GC under hard
                    # pressure (full text lives in the cold tier)
                    row["plan"] = "stub_pointer (gc_oldest_on_hard_pressure)"
                    keep = True
                else:
                    # rule type: keep aligned with actual overflow behaviour
                    # (S0 immediate exit, 2026-08-26) — keyword view sinkable
                    # + unprotected → overflow will actually sink it
                    protected = _is_protected_rule(e, m)
                    kw_sink = not should_keep_local(e)
                    if kw_sink and not protected:
                        row["plan"] = "sink_now (kw_view + S0)"
                        keep = False
                    else:
                        row["plan"] = f"keep (compress after {RULE_COMPRESS_DAYS}d)"
                        keep = True
                    row["protected"] = protected
                    row["kw_sink"] = kw_sink
                    # Phase 4 LRU observability (M2)
                    row["weight"] = m.get("weight")
                    row["w_eff"] = round(_rule_weight_eff(m), 4)
                    row["last_active_at"] = m.get("last_active_at")
                    row["residency_days"] = max(
                        0, RULE_MIN_RESIDENCY_DAYS - (age or 0))
                    row["next_gate"] = {
                        "compress_in_d": max(0, RULE_COMPRESS_DAYS - (age or 0)),
                    }
                    # 2026-08-28 activity dimension: low weight + long-inactive +
                    # unprotected → sink_candidate. Same data source as the
                    # weight/w_eff/last_active_at columns; visibility only,
                    # does NOT change overflow execution.
                    w_raw = m.get("weight")
                    laa_dt = (_parse_iso(str(m.get("last_active_at")))
                              if m.get("last_active_at") else None)
                    inactive_days = (
                        (datetime.now(timezone.utc) - laa_dt).days
                        if laa_dt else None)
                    if (not protected and w_raw is not None
                            and float(w_raw) < AUDIT_SINK_WEIGHT_THRESHOLD
                            and (inactive_days is None
                                 or inactive_days > AUDIT_SINK_INACTIVE_DAYS)):
                        row["sink_candidate"] = True
                        row["sink_reason"] = "low_weight+inactive"
                        lru_sink += 1
                # keep and plan stay consistent for typed entries (final review)
                row["keep"] = keep
            else:
                row["type"] = "legacy"
                row["plan"] = "stamp_on_next_overflow"
            rows.append(row)
            if not keep:
                sink_total += 1
                sink_chars += len(e)
        result[t] = {
            "usage_pct": f"{usage:.0f}%",
            "entries": len(entries),
            "keep": len(entries) - sink_total,
            "sink_candidates": sink_total,
            "sink_chars": sink_chars,
            "rows": rows,
        }
        # Phase 4 LRU summary: rule ecology chars vs budget
        rule_chars = 0
        for e in entries:
            m = meta.get_entry(e)
            if m and m.get("type") in ("rule", "stub"):
                rule_chars += len(e)
        result[t]["rule_chars"] = rule_chars
        result[t]["rule_budget"] = RULE_BUDGET_CHARS
        result[t]["rule_chars_vs_budget"] = rule_chars - RULE_BUDGET_CHARS
        result[t]["lru_sink_candidates"] = lru_sink  # 2026-08-28: activity-dimension sink candidates
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 5b: memorycore_get_rule_weight — rule weight distribution (read-only LRU monitor)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_get_rule_weight(target: str = "memory") -> str:
    """View hot-tier rule weight distribution (read-only, never modifies) —
    LRU retirement monitor.

    w_eff = weight × 0.5^(days since last_active_at / 30) (lazy discount,
    same formula as retirement ordering); rules sorted by w_eff ascending =
    eviction order when the budget is exceeded.
    next_eviction_candidates = up to MAX_EVICT_PER_RUN rules that would be
    evicted first if the budget were exceeded right now (observation only —
    actual eviction is also gated by residency/lexical-activity protection,
    see enforce_rule_budget).

    Args:
        target: 'memory' | 'user' | 'both' (default memory)
    Returns:
        JSON: {summary: {rule_count, rule_chars, rule_budget, over_budget,
               w_eff_min/avg/max, next_eviction_candidates}, rules: [...]}
    """
    out = {}
    for t in _targets(target):
        entries = _store.entries(t)
        ms = _metastore_for(t)
        rules = []
        rule_chars = 0
        for e in entries:
            m = ms.get_entry(e)
            if not m or m.get("type") != "rule":
                continue
            rule_chars += len(e)
            rules.append({
                "text": e[:60],
                "chars": len(e),
                "weight": m.get("weight"),
                "w_eff": round(_rule_weight_eff(m), 4),
                "last_active_at": m.get("last_active_at"),
                "age_days": entry_age_days(m),
                "protected": _is_protected_rule(e, m),
                "kw_sink": not should_keep_local(e),
            })
        rules.sort(key=lambda r: r["w_eff"])
        w_effs = [r["w_eff"] for r in rules]
        out[t] = {
            "summary": {
                "rule_count": len(rules),
                "rule_chars": rule_chars,
                "rule_budget": RULE_BUDGET_CHARS,
                "over_budget": rule_chars > RULE_BUDGET_CHARS,
                "w_eff_min": round(min(w_effs), 4) if w_effs else None,
                "w_eff_avg": round(sum(w_effs) / len(w_effs), 4) if w_effs else None,
                "w_eff_max": round(max(w_effs), 4) if w_effs else None,
                "next_eviction_candidates":
                    [r["text"] for r in rules[:MAX_EVICT_PER_RUN]],
            },
            "rules": rules,
        }
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Tool 6: memorycore_recall — active cold-tier recall (read-only)
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
        log_activity_query(query)  # Phase 3 S4: topic-activity collection
        results = _client.recall_results(query, top_k=k)
        results = _apply_decay(results)
        # Phase 4: recall hit on a stub cold_id -> restore full text to hot tier
        try:
            results = restore_stubs_from_results(
                _store, {t: _metastore_for(t) for t in ("memory", "user")}, results)
        except Exception:
            pass  # restore failure never blocks the recall response
        return json.dumps({"results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio", show_banner=False)  # FastMCP 3.x banner pollutes stdio stdout
