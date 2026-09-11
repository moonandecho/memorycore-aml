#!/usr/bin/env python3
"""MemoryCore MCP server — 完整溢流层记忆系统

承载: 冷热分流 (store_fact) / 容量管控 / 六步溢流 (trigger_overflow) /
冷层治理 (run_cold_storage_maintenance) / 健康状态 (get_memory_usage)。

内部: 读本地 MEMORY.md/USER.md (local_store), 经 ColdStoreClient 双后端
访问冷层 (默认进程内 mnemosyne-memory, 可切 MCP 远程后端)。
"""
import json
from datetime import datetime, timezone

try:
    from mcp.server.mcpserver import MCPServer  # noqa: E402  # mcp 2.x 官方高级 API (替代第三方 fastmcp)
except ImportError as _e:
    # 依赖前置检查: 把 mcp 大版本变更变成可读中文提示, 不裸抛 traceback (任务 E)
    import importlib.metadata as _imd
    import sys as _sys
    try:
        _mcp_ver = _imd.version("mcp")
    except Exception:
        _mcp_ver = "未知"
    _sys.stderr.write(
        f"[memorycore] 启动失败: 当前 mcp 版本 {_mcp_ver} 不含 MCPServer (属 mcp 大版本变更)。\n"
        "[memorycore] 建议: 用独立 venv 安装 memorycore; 或 pip install \"mcp>=2,<3\"; "
        "不要与其它工具 (如 Hermes) 共用同一个环境。\n"
        f"[memorycore] 原始错误: {_e}\n"
    )
    _sys.exit(1)


from .local_store import LocalStore  # noqa: E402
from .cold_store_client import ColdStoreClient  # noqa: E402
from .core.config import (SOFT_THRESHOLD, HARD_THRESHOLD, TARGET_RATIO, COLD_SOFT_LIMIT, COLD_HARD_LIMIT,  # noqa: E402
                         STATE_TTL_DAYS, RULE_COMPRESS_DAYS,
                         RULE_MIN_RESIDENCY_DAYS, RULE_BUDGET_CHARS,
                         AUDIT_SINK_WEIGHT_THRESHOLD, AUDIT_SINK_INACTIVE_DAYS,
                         MAX_EVICT_PER_RUN)
from .core.classifier import (classify, classify_user_pref, should_keep_local,  # noqa: E402
                             classify_entry_type, COLD, STALE)
from .core.metadata import (MetaStore, entry_age_days, log_activity_query,  # noqa: E402  # Phase 3 S4 采集
                           _parse_iso)
from .core.overflow import (run_overflow, _recall_safe, _find_best_match,  # noqa: E402
                           _merge_two_entries, _is_protected_rule,
                           enforce_rule_budget, apply_activity_hits,
                           restore_stubs_from_results, _rule_weight_eff)
from .core.maintenance import run_maintenance  # noqa: E402
from .core import llm_config  # noqa: E402  # TTL 失效入口 (终审低危)
from .core.decay import _apply_decay  # noqa: E402  # 小项1: 提取到独立模块

mcp = MCPServer("memorycore")

_store = LocalStore()
_client = ColdStoreClient()

def _targets(target: str):
    if target == "user":
        return ["user"]
    if target == "both":
        return ["memory", "user"]
    return ["memory"]


def _metastore_for(target: str) -> MetaStore:
    """按 target 构造 sidecar 元数据存储 (跟随 _store 的路径, 测试可注入)。"""
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


# ---------------------------------------------------------------------------

def _check_cold_capacity() -> None:
    """冷层容量硬闸: 写入前检查冷层条数, 超阈值触发治理 (Task C)。

    - > HARD_LIMIT: 强制循环 maintenance 至回落 (上限 5 轮)
    - > SOFT_LIMIT: 先跑一轮 maintenance 再继续
    - 冷层不可达: 跳过 (不阻塞写入)
    """
    try:
        stats = _client.stats()
    except Exception:
        return  # 冷层不可达 → 降级跳过

    cold_total = stats.get("total", 0)

    if cold_total > COLD_HARD_LIMIT:
        # 强制清理至回落, 上限 5 轮
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
        # 跑一轮 maintenance 再继续
        run_maintenance(_client)

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
        # ---- A 写入分流: target="user" 用 classify_user_pref ----
        is_user = (target == "user")
        if is_user:
            d = classify_user_pref(content, importance=importance,
                                   sentence_level=False)
            stale_reason = None  # classify_user_pref 不返回 reason dict
        else:
            decision = classify(content, importance=importance)
            d = decision["decision"]
            stale_reason = decision.get("reason", "")

        # Task C: 容量硬闸 — 写入前检查冷层条数
        _check_cold_capacity()

        # STALE 处理
        if d == STALE or d == "stale":
            if is_user:
                return json.dumps({"status": "stale",
                                   "detail": "过时状态记录 (USER.md), 不写入"},
                                  ensure_ascii=False)
            return json.dumps({"status": "stale", "detail": stale_reason,
                               "note": "过时状态记录, 不迁移不写入"}, ensure_ascii=False)

        # COLD / sink 处理
        go_cold = (is_user and d == "sink") or (not is_user and d == COLD)
        if not go_cold and classify_entry_type(content) == "state":
            # Phase 2 (2026-08-16): 写入口联动 — 判 hot/core 但属历史决策/
            # 状态记录 (日期+完成态词, 无行为指令词) → 强制走冷层路径,
            # 污染从源头不进热层。默认 importance=0.8 会把几乎所有内容判热,
            # 此判定在其后覆盖, 不修改 classify 本身 (兼容)。
            go_cold = True
        if go_cold:
            # Task A: 冷数据 → 先查重再写, 防止重复双写
            try:
                existing = _recall_safe(_client, content)
            except Exception as e:
                return json.dumps({"status": "error",
                                   "detail": f"冷层不可达, 写入失败: {e}"},
                                  ensure_ascii=False)

            if existing:
                matched = _find_best_match(content, existing)
                if matched:
                    if matched["level"] == "same":
                        return json.dumps({"status": "cold_duplicate",
                                           "detail": "冷层已有相同事实, 跳过写入"},
                                          ensure_ascii=False)
                    elif matched["level"] == "similar":
                        merged = _merge_two_entries(content, matched["content"])
                        try:
                            r = _client.update(matched["id"], merged)
                            if r.get("status") == "updated":
                                return json.dumps({"status": "cold_updated",
                                                   "memory_id": matched["id"],
                                                   "detail": "已合并到冷层已有记录"},
                                                  ensure_ascii=False)
                            return json.dumps({"status": "error",
                                               "detail": f"更新合并失败: {r}"},
                                              ensure_ascii=False)
                        except Exception as e:
                            return json.dumps({"status": "error",
                                               "detail": f"更新合并异常: {e}"},
                                              ensure_ascii=False)

            # 无匹配 → remember (原逻辑)
            r = _client.remember(content, importance=importance, scope=scope)
            if r.get("status") == "stored":
                cold_detail = "USER.md 长尾事实已进冷层" if is_user else "低频事实已下沉冷层"
                return json.dumps({"status": "cold_stored",
                                   "memory_id": r.get("memory_id"),
                                   "detail": cold_detail}, ensure_ascii=False)
            return json.dumps({"status": "error", "detail": f"冷层写入失败: {r}"},
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
            # Phase 2: 热层写入成功后盖元数据 (rule 型, written_at=now)
            # Phase 3 S6: importance 透传 (保护线, 缺失默认 0.8 向后兼容)
            try:
                _metastore_for(t).stamp(content, "rule",
                                        importance=importance,
                                        origin="store_fact")
            except Exception:
                pass  # 元数据失败不影响写入, 下次 reconcile 兜底
            # Phase 4: 写后规则预算检查 (LRU 挤权; 新规则有 7 天驻留不会被立刻挤)
            try:
                enforce_rule_budget(_store, _client, t, _metastore_for(t), {})
            except Exception:
                pass  # 预算挤权失败不阻塞写入 (下轮溢流兜底)
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
    # TTL invalidation entry (final audit low-risk): long-lived MCP server
    # picks up ~/.hermes/.env changes immediately at the next tool call
    llm_config.invalidate_cache()
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
    # TTL invalidation entry (final audit low-risk): see trigger_overflow
    llm_config.invalidate_cache()
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


@mcp.tool()
def memorycore_memory_audit(target: str = "both") -> str:
    """热层体检 (只读, 不修改): 列出热层每条的分类 (keep=留热层 / sink=可下沉)、
    字数与占用统计。用于排查溢流空转 — 热层被历史决策/状态记录占满时 sink_candidates>0,
    溢流才有东西可沉; 全 keep 说明内容本身都是准则 (溢流降不动属正常)。"""
    result = {}
    for t in _targets(target):
        usage = _store.usage_pct(t)
        entries = _store.entries(t)
        meta = _metastore_for(t)
        rows = []
        sink_total = 0
        sink_chars = 0
        lru_sink = 0  # 缺口2: 活性维度可沉候选计数 (新字段, 不改现有计数口径)
        for e in entries:
            keep = should_keep_local(e)  # 关键词视图 (legacy 回退用)
            m = meta.get_entry(e)
            row = {"keep": keep, "chars": len(e), "text": e[:40]}
            # Phase 2: 元数据列 (type/written_at/age/退役计划), 只读体检
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
                        keep = True  # 未到期, 当前仍留
                elif m.get("type") == "stub":
                    # Phase 3 S4: 指针驻留, 高压时最老优先 GC (全文在冷层)
                    row["plan"] = "stub_pointer (gc_oldest_on_hard_pressure)"
                    keep = True
                else:
                    # rule 型: keep 与溢流行为对齐 (S0 即时出口, 2026-08-26 修复) —
                    # 关键词视图判可沉 + 非保护 → 溢流会实际下沉, 审计如实标 sink
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
                    # Phase 4 LRU 观测字段 (M2, 2026-08-26 Pi 检查建议)
                    row["weight"] = m.get("weight")
                    row["w_eff"] = round(_rule_weight_eff(m), 4)
                    row["last_active_at"] = m.get("last_active_at")
                    row["residency_days"] = max(
                        0, RULE_MIN_RESIDENCY_DAYS - (age or 0))
                    row["next_gate"] = {
                        "compress_in_d": max(0, RULE_COMPRESS_DAYS - (age or 0)),
                    }
                    # 缺口2 (2026-08-28): 活性维度可沉判定 — weight 低 +
                    # last_active_at 久远 + 非保护 → sink_candidate。
                    # 复用与 weight/w_eff/last_active_at 列同一数据源
                    # (m.get 原始 meta), 不另算一套; 仅可见性, 不改溢流逻辑。
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
                # 附注1 修复 (终审): typed 条目 keep 与 plan 同源 —
                # 消除 "keep=true + plan=sink_now" 矛盾视图
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
        # Phase 4 LRU 汇总观测 (M2): 规则生态字符 vs 预算
        rule_chars = 0
        for e in entries:
            m = meta.get_entry(e)
            if m and m.get("type") in ("rule", "stub"):
                rule_chars += len(e)
        result[t]["rule_chars"] = rule_chars
        result[t]["rule_budget"] = RULE_BUDGET_CHARS
        result[t]["rule_chars_vs_budget"] = rule_chars - RULE_BUDGET_CHARS
        result[t]["lru_sink_candidates"] = lru_sink  # 缺口2: 活性可沉候选数
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 4.5: get_rule_weight — 规则权重分布 (只读, LRU 监控)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_get_rule_weight(target: str = "memory") -> str:
    """查看热层 rule 型条目权重分布 (只读, 不修改) — LRU 退役机制监控。

    w_eff = weight × 0.5^(距 last_active_at 天数 / 30) (惰性折现, 与退役排序
    同一口径); 列表按 w_eff 升序 = 超预算时最先被挤的退役顺序。
    next_eviction_candidates = 若此刻超预算, 最先退役的至多 MAX_EVICT_PER_RUN 条
    (仅供观测 — 实际挤权还受驻留期/词法活跃保护过滤, 以 enforce_rule_budget 为准)。

    Args:
        target: 'memory' | 'user' | 'both' (默认 memory)
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
# 工具 5: memorycore_recall — 主动召回冷层 (只读, 冷层降权重排)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_recall(query: str, top_k: int = 3) -> str:
    """主动召回冷层记忆 (只读, 补足 prefetch 每轮 top-3 之外的手动查询能力)。

    结果经冷层降权重排: final_score = base_score × 0.5^(days/90),
    importance≥0.8 不降权。

    Args:
        query: 查询内容 (自然语言, 语义召回)
        top_k: 返回条数 (默认 3, 最大 10)
    Returns:
        JSON: {"results": [{"id": ..., "content": ..., "final_score": ...}]}
              冷层不可达时 {"error": "..."}
    """
    try:
        k = max(1, min(int(top_k), 10))
        log_activity_query(query)  # Phase 3 S4: 主题活性采集 (失败静默, 可配置关)
        results = _client.recall_results(query, top_k=k)
        results = _apply_decay(results)
        # Phase 4: 召回结果命中 stub 的 cold_id → 自动恢复全文到热层
        try:
            results = restore_stubs_from_results(
                _store, {t: _metastore_for(t) for t in ("memory", "user")}, results)
        except Exception:
            pass  # 恢复失败不影响召回返回
        return json.dumps({"results": results}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


if __name__ == "__main__":
    mcp.run(transport="stdio")
