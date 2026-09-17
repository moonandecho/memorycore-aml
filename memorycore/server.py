#!/usr/bin/env python3
"""MemoryCore MCP server — 完整溢流层记忆系统

承载: 冷热分流 (store_fact) / 容量管控 / 六步溢流 (trigger_overflow) /
冷层治理 (run_cold_storage_maintenance) / 健康状态 (get_memory_usage)。

内部: 读本地 MEMORY.md/USER.md (local_store), 经 ColdStoreClient 双后端
访问冷层 (默认进程内 mnemosyne-memory, 可切 MCP 远程后端)。
"""
import json
import math
import os
import re
import time
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
                         RULE_MIN_RESIDENCY_IDLE_DAYS,
                         RULE_MIN_RESIDENCY_WARM_DAYS,
                         RULE_MIN_RESIDENCY_ACTIVE_DAYS,
                         AUDIT_SINK_WEIGHT_THRESHOLD, AUDIT_SINK_INACTIVE_DAYS,
                         MAX_EVICT_PER_RUN, JUDGE_RESOLVED_RULE_GRACE_DAYS,
                         WEIGHT_PROTECT_MULT)
from .core.classifier import (classify, classify_user_pref, should_keep_local,  # noqa: E402
                             classify_entry_type, judge_engine_enabled, COLD, STALE)
from .core.judge import judge_entry  # noqa: E402  SAFE-JUDGE v3
from .core.metadata import (MetaStore, entry_age_days, log_activity_query,  # noqa: E402  # Phase 3 S4 采集
                           _ts_anchor, _ts_anomaly_snapshot,
                           load_recent_queries,
                           _judge_hold_kwargs as _judge_kwargs)
from .core.overflow import (run_overflow, _recall_safe, _find_best_match,  # noqa: E402
                           _merge_two_entries, _is_protected_rule, _entry_sha,
                           enforce_rule_budget, apply_activity_hits,
                           restore_stubs_from_results, _rule_weight_eff,
                           _rule_rank, _select_retirement_candidates,
                           _soft_residency_grace_ts,
                           _cache_policy_v2, _stub_topic, _rule_activity_tier,
                           _ambiguous_a1_eligible,
                           _ambiguous_hold_valid,
                           _lex_evidence)
from .core.maintenance import run_maintenance  # noqa: E402
from .core import llm_config  # noqa: E402  # TTL 失效入口 (终审低危)
from .core.decay import _apply_decay  # noqa: E402  # 小项1: 提取到独立模块
from .core.recall_probe import (  # noqa: E402  # P0 只读观测探针
    query_sha256 as _probe_query_sha256, record_recall_probe)

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
        stat = run_overflow(_store, _client, t)
        after = _store.usage_pct(t)
        # CACHE-POLICY-V2: plateau_reason 非空 = 真实数据安全暂停
        # (cold_backstop 等), 继续空转也不会降占用 → 停止。
        if (after <= TARGET_RATIO * 100 or after >= before
                or stat.get("plateau_reason")):
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

_STORE_FACT_LEGACY_TOOL = "memorycore_" + "store_fact"

@mcp.tool(name=_STORE_FACT_LEGACY_TOOL)
def memorycore_store_entry(content: str, importance: float = 0.8, scope: str = "global", target: str = "memory", type_hint: str = "") -> str:
    """记忆写入统一入口: 冷热分流 + 容量校验。

    Args:
        content: 要记忆的事实 (一句话, 中文, 主语清晰)
        importance: 0.0-1.0 重要度 (>=0.8 倾向热数据留本地)
        scope: 'global' 或 'session'
        target: 热数据写入本地哪个文件 ('memory' 或 'user')
        type_hint: 可选人工判型标注 'state'|'rule' (v2, 优先于词法; 留空=自动)
    Returns:
        JSON: {"status": "stored"|"cold_stored"|"stale"|"error", "detail": "..."}
    """
    try:
        # ---- A 写入分流: target="user" 用 classify_user_pref ----
        is_user = (target == "user")
        t = target if target in ("memory", "user") else "memory"
        _hint = type_hint if type_hint in ("state", "rule") else None

        # SAFE-JUDGE v3: 判型一次成型; 不再 classify + should_keep_local 双判。
        # ambiguous → 强制 hot (永不冷迁); strong rule → 不因 classify COLD 被送走;
        # state → 强制冷层路径 (冷层写成功才删本地, 当前入口不写本地故无需删)。
        jr = (judge_entry(content, type_hint=_hint)
              if judge_engine_enabled() else None)
        _jkw = _judge_kwargs(jr) if jr is not None else {}
        # §6.2 审计: store_fact 热路径落 type_source=judge_v3* / manual_override.
        if _hint is not None:
            _type_source = "manual_override"
        elif jr is not None:
            _type_source = ("judge_v3_ambiguous"
                            if jr.decision == "ambiguous" else "judge_v3")
        else:
            _type_source = "lexical_v2"
        _force_hot_amb = bool(jr is not None and jr.decision == "ambiguous")
        _force_hot_rule = bool(jr is not None and jr.decision == "rule"
                               and jr.band == "strong")
        _force_hot = _force_hot_amb or _force_hot_rule
        _judge_state = bool(jr is not None and jr.decision == "state")

        if _force_hot:
            d = "hot"
            stale_reason = "judge_v3_force_hot"
        elif is_user:
            d = classify_user_pref(content, importance=importance,
                                   sentence_level=False)
            stale_reason = None  # classify_user_pref 不返回 reason dict
        else:
            decision = classify(content, importance=importance)
            d = decision["decision"]
            stale_reason = decision.get("reason", "")

        # Task C: 容量硬闸 — 写入前检查冷层条数
        _check_cold_capacity()

        # STALE 处理 (v3 state 优先走冷迁移: 有完成态但属历史事实, 不 forget)
        if not _force_hot and not _judge_state and (d == STALE or d == "stale"):
            if is_user:
                return json.dumps({"status": "stale",
                                   "detail": "过时状态记录 (USER.md), 不写入"},
                                  ensure_ascii=False)
            return json.dumps({"status": "stale", "detail": stale_reason,
                               "note": "过时状态记录, 不迁移不写入"}, ensure_ascii=False)

        # COLD / sink 处理
        if _force_hot:
            go_cold = False
        elif _judge_state:
            go_cold = True
        else:
            go_cold = (is_user and d == "sink") or (not is_user and d == COLD)
            if jr is None:
                # legacy 回滚路径 (JUDGE_V3_ENABLED=0): 保留旧写入口联动
                if not go_cold and classify_entry_type(
                        content, type_hint=_hint) == "state":
                    if (_hint == "state"
                            or not should_keep_local(content)):
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
            return json.dumps({"status": "error", "detail": f"Mnemosyne write failed: {r}"},
                              ensure_ascii=False)

        # HOT: 先查容量再写本地
        #   软阈值 (>=60%): 先溢流一次降压再写
        #   硬阈值 (>=80%): 强制全量溢流至 ≤40% (循环, 有上限保护) 再写
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
                                        origin="store_fact",
                                        type_source=_type_source,
                                        **_jkw)
            except Exception:
                pass  # 元数据失败不影响写入, 下次 reconcile 兜底
            # Phase 4: 写后规则预算检查; ambiguous(A0) 不触发全文挤权。
            if not _force_hot_amb:
                try:
                    enforce_rule_budget(_store, _client, t,
                                        _metastore_for(t), {})
                except Exception:
                    pass  # 预算挤权失败不阻塞写入 (下轮溢流兜底)
            detail = "热数据已写本地"
            if _force_hot_amb:
                detail = "判型模糊: 留热层并标记周治理终审 (不同步 LLM/不冷迁)"
            return json.dumps({"status": "stored", "target": t,
                               "usage_after": f"{_store.usage_pct(t)}%",
                               "judge_decision": (jr.decision if jr else None),
                               "detail": detail}, ensure_ascii=False)
        # ambiguous 不允许冷迁兜底 (B-3 安全方向): 本地写失败 → 明确失败, 不丢判型语义
        if _force_hot_amb:
            return json.dumps({"status": "error",
                               "detail": "ambiguous 条目本地写入失败; "
                                         "按设计拒绝冷迁 (避免误沉)"},
                              ensure_ascii=False)
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
    # TTL 失效入口 (终审低危): 长驻 MCP server 中改 ~/.hermes/.env 即时生效
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
    # TTL 失效入口 (终审低危): 长驻 MCP server 中改 ~/.hermes/.env 即时生效
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
        # FIX6 R2: 时间戳异常必须可见 (未来/不可解析 → ts_anomaly).
        "timestamp_anomaly": _ts_anomaly_snapshot(),
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
        # v2: 近 7 天查询 (Q2 分级词法输入, 与 _select_retirement_candidates 同口径)
        try:
            queries_7d = load_recent_queries(days=7)
        except Exception:
            queries_7d = []
        now = datetime.now(timezone.utc)
        for e in entries:
            keep = should_keep_local(e)  # 关键词视图 (legacy 回退用)
            m = meta.get_entry(e)
            row = {"keep": keep, "chars": len(e), "text": e[:40]}
            # Phase 2: 元数据列 (type/written_at/age/退役计划), 只读体检
            if m:
                row["type"] = m.get("type")
                row["written_at"] = m.get("written_at")
                age = entry_age_days(m, now=now, entry=e)
                row["age_days"] = age
                # SAFE-JUDGE v3 审计字段 (只读; 旧 sidecar 缺失为 None)
                row["judge_decision"] = m.get("judge_decision")
                row["judge_band"] = m.get("judge_band")
                row["judge_reason"] = m.get("judge_reason")
                row["judge_review_at"] = m.get("judge_review_at")
                row["judge_review_count"] = m.get("judge_review_count")
                # B-2/C-3: A0/A1/R-grace plan 视图
                if (m.get("judge_decision") == "ambiguous"
                        and m.get("judge_resolution") != "rule"):
                    if _cache_policy_v2():
                        # CACHE-POLICY-V2: ambiguous 仅低先验审计, 无 hold/A1 资格。
                        row["plan"] = ("evictable_by_budget "
                                       "(ambiguous low prior; judge audit only)")
                        row["lru_eligible"] = True
                        row["priority"] = round(_rule_rank(e, m, now), 4)
                        row["protected_mult"] = 1.0
                        row["keep"] = True
                        rows.append(row)
                        continue
                    now_j = datetime.now(timezone.utc)
                    if (_ambiguous_hold_valid(m, now_j, entry=e)
                            and not _ambiguous_a1_eligible(m, now_j, entry=e)):
                        row["plan"] = ("hold_ambiguous (review due "
                                       f"{m.get('judge_review_at')})")
                    else:
                        row["plan"] = ("stub_eligible_ambiguous "
                                       "(A1: cold full text + pointer)")
                    row["keep"] = True
                    rows.append(row)
                    continue
                if m.get("judge_resolution") == "rule":
                    if _cache_policy_v2():
                        # CACHE-POLICY-V2: R-grace 不再阻止统一预算换出。
                        row["plan"] = ("evictable_by_budget "
                                       "(resolved judge audit only)")
                        row["lru_eligible"] = True
                        row["priority"] = round(_rule_rank(e, m, now), 4)
                        row["protected_mult"] = 1.0
                        row["keep"] = True
                        rows.append(row)
                        continue
                    row["plan"] = ("resolved_rule_grace (LRU in "
                                   f"{JUDGE_RESOLVED_RULE_GRACE_DAYS}d)")
                    row["keep"] = True
                    rows.append(row)
                    continue
                if m.get("type") == "state":
                    # CACHE-POLICY-V2: state 无 TTL 直接换出资格; 与 rule/stub 同池
                    # 由活性+预算换出。类型只作低初始权重先验。
                    row["plan"] = "evictable_by_budget (type_state low prior)"
                    row["lru_eligible"] = True
                    keep = True
                elif m.get("type") == "stub":
                    # Phase 3 S4: 指针驻留, 高压时最老优先 GC (全文在冷层)
                    row["plan"] = "stub_pointer (gc_oldest_on_hard_pressure)"
                    keep = True
                elif m.get("type_override") == "state":
                    # G-5 (2026-09-12 评审修复): type=rule 但被人工标为
                    # type_override=state 的条目, 下次溢流 S0 会立即冷迁;
                    # plan 视图与 _handle_typed_entry 实际行为对齐。
                    protected = _is_protected_rule(e, m)
                    # CACHE-POLICY-V2: type_override=state 不再 S0 直迁;
                    # 热层中的 state 与 rule/stub 同池, 由预算+活性换出。
                    row["plan"] = "evictable_by_budget (manual state override)"
                    row["protected"] = protected
                    row["weight"] = m.get("weight")
                    row["w_eff"] = round(_rule_weight_eff(m, now=now, entry=e), 4)
                    row["priority"] = round(_rule_rank(e, m, now), 4)
                    row["protected_mult"] = (WEIGHT_PROTECT_MULT if protected else 1.0)
                    row["last_active_at"] = m.get("last_active_at")
                    row["type_source"] = m.get("type_source")
                    row["lru_eligible"] = True
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
                    row["w_eff"] = round(_rule_weight_eff(m, now=now, entry=e), 4)
                    row["last_active_at"] = m.get("last_active_at")
                    row["residency_days"] = max(
                        0, RULE_MIN_RESIDENCY_DAYS - (age or 0))
                    # v2 (2026-09-12, DESIGN §Q3.5): 审计可见 — 被挤前可解释
                    row["type_source"] = m.get("type_source")
                    tier, min_age = _rule_activity_tier(m, e, queries_7d, now)
                    row["activity_tier"] = tier
                    row["min_retire_age"] = min_age  # 仅审计/tier 解释, 不再是资格门
                    row["lru_eligible"] = True  # CACHE-POLICY-V2: protected 不再是资格豁免
                    row["priority"] = round(_rule_rank(e, m, now), 4)
                    row["protected_mult"] = (WEIGHT_PROTECT_MULT if protected else 1.0)
                    row["next_gate"] = {
                        "compress_in_d": max(0, RULE_COMPRESS_DAYS - (age or 0)),
                        "lru_in_d": max(0, min_age - (age or 0)),
                    }
                    # 缺口2 (2026-08-28): 活性维度可沉判定 — weight 低 +
                    # last_active_at 久远 + 非保护 → sink_candidate。
                    # 复用与 weight/w_eff/last_active_at 列同一数据源
                    # (m.get 原始 meta), 不另算一套; 仅可见性, 不改溢流逻辑。
                    w_raw = m.get("weight")
                    laa_dt = _ts_anchor(m.get("last_active_at"),
                                        datetime.now(timezone.utc),
                                        field="last_active_at",
                                        sha=_entry_sha(e))
                    inactive_days = (
                        (datetime.now(timezone.utc) - laa_dt).days
                        if laa_dt else None)
                    if (w_raw is not None
                            and float(w_raw) < AUDIT_SINK_WEIGHT_THRESHOLD
                            and (inactive_days is None
                                 or inactive_days > AUDIT_SINK_INACTIVE_DAYS)):
                        # CACHE-POLICY-V2 Q5: protected 只是 ×3, 低活性下同样
                        # 是可换出候选; 这里用 sink_candidate 暴露审计信号。
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
        # CACHE-POLICY-V2: 统一候选池的 next_evict (只读; protected 不豁免)。
        try:
            _next = _select_retirement_candidates(
                _store, meta, t, max(1, rule_chars - RULE_BUDGET_CHARS), {})
            result[t]["next_evict"] = [e[:40] for e in _next]
        except Exception:
            result[t]["next_evict"] = []
        _stub_chars = sum(len(e) for e in entries
                          if (meta.get_entry(e) or {}).get("type") == "stub")
        _warnings = []
        if rule_chars > RULE_BUDGET_CHARS:
            _warnings.append("rule_chars>RULE_BUDGET_CHARS")
        if _stub_chars > RULE_BUDGET_CHARS:
            _warnings.append("stub_chars>RULE_BUDGET_CHARS")
        result[t]["audit_warning"] = ";".join(_warnings) if _warnings else None
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 4.4: set_entry_type — 人工标注 (只写 sidecar, DESIGN §Q1 第二判据)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_set_entry_type(target: str = "memory", match_text: str = "",
                              type_override: str = "",
                              protect_override: str = "") -> str:
    """人工标注热层条目判型/保护 (v2, 只写 sidecar 元数据, 永不修改 .md 内容)。

    用途 (DESIGN §Q1 第二判据): 词法判型拿不准时人工强制 type_override
    (优先于词法), 或对想保护/解除保护的条目写 protect_override。
    - type_override=state → 下次溢流 S0 立即冷迁移 (冷层写成功才删本地);
    - type_override=rule → 强制按 rule 处理 (不再被完成态词误判 state);
    - protect_override=true → 不进入自动沉候选池 (Q4);
    - protect_override=false → 仅降级文本类保护; 红线硬词与 importance≥0.9
      不可解除 (DESIGN-DEVIATIONS 第 4 条口径)。

    Args:
        target: 'memory' | 'user'
        match_text: 定位条目的唯一子串 (必须恰好命中一条; 取条目开头片段即可)
        type_override: '' (不改) | 'state' | 'rule'
        protect_override: '' (不改) | 'true' | 'false'
    Returns:
        JSON: {"status": "ok"|"not_found"|"ambiguous"|"noop"|"error", ...}
    """
    try:
        ms = _metastore_for(target)
        entries = _store.entries(target)
        needle = (match_text or "").strip()
        hits = [e for e in entries if needle and needle in e]
        if not hits:
            return json.dumps({"status": "not_found",
                               "detail": "match_text 未命中任何条目"},
                              ensure_ascii=False)
        if len(hits) > 1:
            return json.dumps({"status": "ambiguous",
                               "detail": f"match_text 命中 {len(hits)} 条, "
                                         "请提供更长唯一子串",
                               "previews": [e[:40] for e in hits[:5]]},
                              ensure_ascii=False)
        entry = hits[0]
        if not type_override and not protect_override:
            return json.dumps({"status": "noop",
                               "detail": "未提供任何标注 (type_override / "
                                         "protect_override)"},
                              ensure_ascii=False)
        if type_override and type_override not in ("state", "rule"):
            return json.dumps({"status": "error",
                               "detail": f"type_override 仅支持 state|rule, "
                                         f"got {type_override!r}"},
                              ensure_ascii=False)
        if protect_override and protect_override not in ("true", "false"):
            return json.dumps({"status": "error",
                               "detail": "protect_override 仅支持 true|false"},
                              ensure_ascii=False)
        old = ms.get_entry(entry) or {}
        # 只写 sidecar: 保留全部既有字段, 覆盖标注键
        ms.stamp(
            entry, old.get("type", "rule"),
            written_at=_ts_anchor(old.get("written_at"), field="written_at",
                             sha=_entry_sha(entry)),
            updated_at=_ts_anchor(old.get("updated_at"), field="updated_at",
                             sha=_entry_sha(entry)),
            origin=old.get("origin", "hermes"),
            importance=float(old.get("importance") or 0.8),
            weight=float(old.get("weight")) if old.get("weight") is not None else None,
            last_active_at=_ts_anchor(old.get("last_active_at"), field="last_active_at",
                        sha=_entry_sha(entry)),
            last_scan_at=_ts_anchor(old.get("last_scan_at"), field="last_scan_at",
                             sha=_entry_sha(entry)),
            cold_id=old.get("cold_id"),
            type_override=(type_override or old.get("type_override")),
            type_source=("manual_override" if type_override
                         else old.get("type_source")),
            protect_override=({"true": True, "false": False}[protect_override]
                              if protect_override else old.get("protect_override")),
            last_strong_hit_at=_ts_anchor(old.get("last_strong_hit_at"), field="last_strong_hit_at",
                        sha=_entry_sha(entry)),
            last_weak_hit_at=_ts_anchor(old.get("last_weak_hit_at"), field="last_weak_hit_at",
                        sha=_entry_sha(entry)),
            # FIX6 #5: 条件透传全部审计/写回字段, 不因人工标注重建 meta.
            last_recall_hit_at=_ts_anchor(old.get("last_recall_hit_at"), field="last_recall_hit_at",
                        sha=_entry_sha(entry)),
            last_injected_at=_ts_anchor(old.get("last_injected_at"), field="last_injected_at",
                        sha=_entry_sha(entry)),
            last_evicted_at=_ts_anchor(old.get("last_evicted_at"), field="last_evicted_at",
                        sha=_entry_sha(entry)),
            writeback_count=(int(old["writeback_count"])
                             if old.get("writeback_count") is not None else None),
            retire_count=(int(old["retire_count"])
                          if old.get("retire_count") is not None else None),
            handle=old.get("handle"),
            # P4 (FIX8): type_override 改型时显式清 ambiguous 审计键,
            # 不能让 judge_review_at/judge_reviewed_at/judge_resolved_at
            # 残留在新判型上 (旧 bug: quick-path 只清 judge_decision).
            judge_review_at=(None if type_override else _ts_anchor(
                old.get("judge_review_at"), field="judge_review_at",
                sha=_entry_sha(entry), allow_future=True)),
            judge_reviewed_at=(None if type_override else _ts_anchor(
                old.get("judge_reviewed_at"), field="judge_reviewed_at",
                sha=_entry_sha(entry))),
            judge_resolved_at=(None if type_override else _ts_anchor(
                old.get("judge_resolved_at"), field="judge_resolved_at",
                sha=_entry_sha(entry))),
            reconcile_anchor_fallback=old.get("reconcile_anchor_fallback"),
            schema=2,
            # SAFE-JUDGE v3: 人工标注优先; 写 override 时同步判型字段并清复审
            judge_decision=(type_override or old.get("judge_decision")),
            judge_band=("strong" if type_override
                        else old.get("judge_band")),
            judge_confidence=(1.0 if type_override
                              else old.get("judge_confidence")),
            judge_signals=old.get("judge_signals"),
            judge_reason=("manual_override" if type_override
                          else old.get("judge_reason")),
            judge_policy=old.get("judge_policy", "v3"),
            judge_review_count=(0 if type_override
                                else old.get("judge_review_count")),
            judge_resolution=("manual_override" if type_override
                              else old.get("judge_resolution")),
        )
        new = ms.get_entry(entry) or {}
        return json.dumps({
            "status": "ok",
            "target": target,
            "entry": entry[:60],
            "type_override": new.get("type_override"),
            "protect_override": new.get("protect_override"),
            "note": "只写 sidecar, .md 未修改; 下次溢流按标注接管",
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "detail": str(e)},
                          ensure_ascii=False)


# ---------------------------------------------------------------------------
# 工具 4.5: get_rule_weight — 规则权重分布 (只读, LRU 监控)
# ---------------------------------------------------------------------------

@mcp.tool()
def memorycore_get_rule_weight(target: str = "memory") -> str:
    """查看热层 rule 型条目权重分布 (只读, 不修改) — LRU 退役机制监控。

    w_eff = weight × 0.5^(距 last_active_at 天数 / 30) (惰性折现, 与退役排序
    同一口径); 列表按 w_eff 升序 = 超预算时最先被挤的退役顺序。
    next_eviction_candidates = 直接调用生产 _select_retirement_candidates
    得到的至多 MAX_EVICT_PER_RUN 条 (FIX8 统一池单一 rank; protected
    只乘 ×3; 实际换出以 enforce_rule_budget 的冷层写成功为准)。
    rules[].in_grace/grace_ts 为新鲜窗口只读审计; 新鲜窗口只影响 priority
    里的 ×GRACE_MULT 排序乘数, 不再是资格/分档.

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
        now = datetime.now(timezone.utc)
        for e in entries:
            m = ms.get_entry(e)
            if not m or m.get("type") != "rule":
                continue
            rule_chars += len(e)
            protected = _is_protected_rule(e, m)
            _grace_ts = _soft_residency_grace_ts(m, now, entry=e)
            rules.append({
                "text": e[:60],
                "chars": len(e),
                "weight": m.get("weight"),
                "w_eff": round(_rule_weight_eff(m, now=now, entry=e), 4),
                "priority": round(_rule_rank(e, m, now), 4),
                "last_active_at": m.get("last_active_at"),
                "last_strong_hit_at": m.get("last_strong_hit_at"),
                "last_weak_hit_at": m.get("last_weak_hit_at"),
                "last_recall_hit_at": m.get("last_recall_hit_at"),
                "last_evicted_at": m.get("last_evicted_at"),
                "writeback_count": int(m.get("writeback_count") or 0),
                "retire_count": int(m.get("retire_count") or 0),
                "age_days": entry_age_days(m, now=now, entry=e),
                "protected": protected,
                "protected_mult": (WEIGHT_PROTECT_MULT if protected else 1.0),
                "kw_sink": not should_keep_local(e),
                # FIX8: 新鲜窗口只读审计; priority/next_eviction_candidates
                # 都来自含 ×GRACE_MULT 的统一 rank, 实际换出以 enforce 为准。
                "in_grace": _grace_ts is not None,
                "grace_ts": _grace_ts.isoformat() if _grace_ts else None,
                "reconcile_anchor_fallback": bool(
                    m.get("reconcile_anchor_fallback")),
            })
        rules.sort(key=lambda r: (r["priority"], r["w_eff"], r["text"]))
        w_effs = [r["w_eff"] for r in rules]
        prs = [r["priority"] for r in rules]
        try:
            _next_need = max(1, rule_chars - RULE_BUDGET_CHARS)
            _next_evict = [
                _e[:60] for _e in _select_retirement_candidates(
                    _store, ms, t, _next_need, {})]
        except Exception:
            _next_evict = []
        out[t] = {
            "summary": {
                "rule_count": len(rules),
                "rule_chars": rule_chars,
                "rule_budget": RULE_BUDGET_CHARS,
                "over_budget": rule_chars > RULE_BUDGET_CHARS,
                "w_eff_min": round(min(w_effs), 4) if w_effs else None,
                "w_eff_avg": round(sum(w_effs) / len(w_effs), 4) if w_effs else None,
                "w_eff_max": round(max(w_effs), 4) if w_effs else None,
                "priority_min": round(min(prs), 4) if prs else None,
                "in_grace_count": sum(1 for r in rules if r["in_grace"]),
                "ts_anomaly": _ts_anomaly_snapshot(),
                # FIX6 #7: 不再用 priority 前 N 冒充选择器; 直接复用生产
                # 统一候选池单一 rank, 审计与实际淘汰顺序一致.
                "next_eviction_candidates": _next_evict,
            },
            "rules": rules,
        }
    return json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# P2: 预注册召回融合 (治理层, env 开关默认关)
# ---------------------------------------------------------------------------
# 预注册超参 (DESIGN-P2.md §1.3): 不得因 dev/holdout 结果调参。
_RECALL_FUSION_ENV = "MEMORYCORE_RECALL_FUSION"
_RECALL_FUSION_CANDIDATE_K_ENV = "MEMORYCORE_RECALL_FUSION_CANDIDATE_K"
_RECALL_FUSION_CANDIDATE_K_DEFAULT = 30
_RECALL_FUSION_CANDIDATE_K_MAX = 50
_RECALL_FUSION_RRF_K = 5
_RECALL_FUSION_W_ENGINE = 1.0
_RECALL_FUSION_W_DECAY = 1.5
_RECALL_FUSION_W_LEX = 0.25
_RECALL_FUSION_LEX_PRODUCT_LIMIT = 200_000

_RECALL_FUSION_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_RECALL_FUSION_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/#\-]*")
_RECALL_FUSION_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?")
_RECALL_FUSION_DIGIT2_RE = re.compile(r"\d{2,}")


def _recall_fusion_enabled():
    """env 开关默认关; 仅显式白名单真值启用。"""
    value = (os.environ.get(_RECALL_FUSION_ENV, "0") or "").strip().lower()
    return value in ("1", "true", "yes", "on")


def _recall_fusion_candidate_k():
    """预注册 candidate_k 默认 30; 允许 env 覆盖, 硬上限 50。"""
    raw = os.environ.get(_RECALL_FUSION_CANDIDATE_K_ENV)
    try:
        value = (int(raw) if raw is not None
                 else _RECALL_FUSION_CANDIDATE_K_DEFAULT)
    except (TypeError, ValueError):
        value = _RECALL_FUSION_CANDIDATE_K_DEFAULT
    if value < 1:
        value = 1
    return min(value, _RECALL_FUSION_CANDIDATE_K_MAX)


def _recall_fusion_longest_common_cjk(a, b):
    """LCS 项的公共连续中文字串长度 (只统计 CJK, 与预注册公式一致)。"""
    aa = "".join(_RECALL_FUSION_CJK_RE.findall(a or ""))
    bb = "".join(_RECALL_FUSION_CJK_RE.findall(b or ""))
    if not aa or not bb:
        return 0
    prev = [0] * (len(bb) + 1)
    best = 0
    for i in range(1, len(aa) + 1):
        cur = [0] * (len(bb) + 1)
        ai = aa[i - 1]
        for j in range(1, len(bb) + 1):
            if ai == bb[j - 1]:
                val = prev[j - 1] + 1
                cur[j] = val
                if val > best:
                    best = val
        prev = cur
    return best


def _recall_fusion_normalize_date(token):
    return re.sub(r"[年月/]", "-", token).rstrip("日")


def _recall_fusion_lex_score(query, content):
    """DESIGN-P2 §1.2 预注册词法/实体弱特征 (只读, 不调用冷层)。

    非字符串 query/content 一律按无词法证据处理 (score=0), 不让单条
    坏数据把整条融合召回变成报错/空结果。
    """
    q = query if isinstance(query, str) else ""
    c = content if isinstance(content, str) else ""
    cl = c.lower()
    score = 0.0
    for token in _RECALL_FUSION_LATIN_RE.findall(q):
        if len(token) < 2:
            continue
        tl = token.lower()
        if token.isalpha():
            if re.search(r"(?<![a-z0-9_])" + re.escape(tl) +
                         r"(?![a-z0-9_])", cl):
                score += 0.4
        elif tl in cl:
            score += 1.0
    if set(_RECALL_FUSION_DIGIT2_RE.findall(q)) & set(
            _RECALL_FUSION_DIGIT2_RE.findall(c)):
        score += 0.7
    q_dates = {_recall_fusion_normalize_date(x)
               for x in _RECALL_FUSION_DATE_RE.findall(q)}
    c_dates = {_recall_fusion_normalize_date(x)
               for x in _RECALL_FUSION_DATE_RE.findall(c)}
    if q_dates & c_dates:
        score += 1.0
    if len(q) * len(c) <= _RECALL_FUSION_LEX_PRODUCT_LIMIT:
        common = _recall_fusion_longest_common_cjk(q, c)
        if common >= 3:
            score += min(0.5 * (common - 2), 1.5)
    return round(score, 6)


def _fuse_recall_candidates(results, query, top_k):
    """对候选池做三路 RRF 融合, 返回截断到 top_k 的原候选 dict 列表。

    三路: R_eng=冷层返回序; R_dec=`_apply_decay` 后 final_score 序;
    R_lex=本地预注册词法证据序 (仅正证据). 只读, 不调冷层/不落盘/不扩候选。

    名次 key 是候选在 `pool` 中的位置 (行身份), 不是 `id`。因此重复 id
    各自保留独立名次, 缺失 id 的行同样按独立候选参与, 不会被 dict 折叠。
    """
    pool = list(results or [])
    if not pool:
        return []
    n = len(pool)
    # R_eng: 冷层返回序 (1-based, 位置即身份).
    engine_rank = list(range(1, n + 1))

    # R_lex: 仅正词法证据参与; 同分以 engine rank 稳定决胜。
    lex_scores = [_recall_fusion_lex_score(query, row.get("content"))
                  for row in pool]
    lex_items = sorted(
        [(-lex_scores[pos], engine_rank[pos], pos)
         for pos in range(n) if lex_scores[pos] > 0],
    )
    lex_rank = {pos: i + 1 for i, (_, _, pos) in enumerate(lex_items)}

    # R_dec: 复用现有 decay 路径; 按候选对象身份还原到原位置, 不被 id 折叠。
    decayed = _apply_decay(list(pool))
    positions_by_identity = {}
    for pos, row in enumerate(pool):
        positions_by_identity.setdefault(id(row), []).append(pos)
    decay_rank = {}
    for rank, row in enumerate(decayed, 1):
        pending = positions_by_identity.get(id(row))
        if pending:
            decay_rank[pending.pop(0)] = rank

    scored = []
    for pos, row in enumerate(pool):
        score = (_RECALL_FUSION_W_ENGINE /
                 (_RECALL_FUSION_RRF_K + engine_rank[pos])
                 + _RECALL_FUSION_W_DECAY /
                 (_RECALL_FUSION_RRF_K + decay_rank.get(pos, n + 1)))
        if pos in lex_rank:
            score += _RECALL_FUSION_W_LEX / (
                _RECALL_FUSION_RRF_K + lex_rank[pos])
        scored.append((score, engine_rank[pos], pos))
    scored.sort(key=lambda item: (-item[0], item[1]))
    limit = max(0, int(top_k))
    return [pool[pos] for _, _, pos in scored[:limit]]


def _recall_core(query, k, *, fusion_on=False, handle_cold_id=None,
                 safe_annotation=False, candidate_count_sink=None):
    """单一召回内核: 冷层单次 bump=False 读取 → 融合/原衰减 → 字段标注。

    生产 ``memorycore_recall`` (handle/写回/探针) 与只读评估
    ``recall_readonly`` 共用本函数; 开关关闭时逐字节保持原召回路径行为。
    返回 ``(results, candidate_count)``: candidate_count 为冷层本次实际返回
    条数 (融合开启且冷层只返回不足 candidate_k 时如实反映实际值)。
    ``candidate_count_sink`` (可选 list) 在字段标注前写入候选数, 供生产探针
    在标注/衰减失败时仍与关闭路径记录同一 candidate_count。
    """
    def _remember_candidate_count(value):
        if candidate_count_sink is not None:
            try:
                candidate_count_sink[0] = value
            except Exception:
                pass

    if fusion_on:
        results = _client.recall_results(
            query, top_k=_recall_fusion_candidate_k(), bump=False)
        try:
            candidate_count = len(results)
        except Exception:
            candidate_count = 0
        _remember_candidate_count(candidate_count)
        results = _fuse_recall_candidates(results, query, k)
    else:
        results = _client.recall_results(query, top_k=k, bump=False)
        try:
            candidate_count = len(results)
        except Exception:
            candidate_count = 0
        _remember_candidate_count(candidate_count)
        results = _apply_decay(results)

    try:
        # page_fault 标注: 结果命中本地任一 stub 的 cold_id 即为缺页写回候选。
        stub_ids = {}
        for _t, _ms in (("memory", _metastore_for("memory")),
                        ("user", _metastore_for("user"))):
            for _e in _store.entries(_t):
                _m = _ms.get_entry(_e) or {}
                if _m.get("type") == "stub" and _m.get("cold_id"):
                    stub_ids[_m["cold_id"]] = _m.get("handle") or ""
        for _r in results:
            # additive 透传: 缺失 keyword/fts 时补 0, 不改变原键值语义。
            _r["keyword_score"] = _probe_float(_r.get("keyword_score"))
            _r["fts_score"] = _probe_float(_r.get("fts_score"))
            _cid = _r.get("id")
            _r["page_fault"] = bool(_cid in stub_ids)
            _r["channel"] = _recall_channel_of(_r, handle_cold_id, query)
            if _cid in stub_ids:
                _r["handle"] = stub_ids[_cid]
    except Exception:
        # recall_readonly 原约定: 热层 stub 标注失败不影响冷层召回本身;
        # 生产路径保持原语义, 标注异常照常抛出由工具壳处理。
        if not safe_annotation:
            raise
    return results, candidate_count


# ---------------------------------------------------------------------------
# P2-step0: 评估/治理只读召回入口 (复用 memorycore_recall 的召回与排序路径)
# ---------------------------------------------------------------------------

def recall_readonly(query: str = "", top_k: int = 3, *, fusion_on=None):
    """只读评估入口: 与 ``memorycore_recall`` 共用 ``_recall_core`` 内核。

    ``fusion_on=None`` 时读取 env 开关 ``MEMORYCORE_RECALL_FUSION`` (默认关);
    env 关闭时与设计轮基线逐字节同路径: 单次
    ``_client.recall_results(bump=False)`` → ``_apply_decay`` → 同样的
    keyword/fts 收口与 page_fault/channel 标注。区别是本函数**不写热层**:
      * 不调用 ``log_activity_query`` (活动日志追加也属于写)；
      * 不调用 ``restore_stubs_from_results`` (因此不会 replace 热层 stub)；
      * 不写 ``record_recall_probe`` (观测落盘)。
    供 ``tools/run_recall_eval.py --recall-source server`` 测治理层排序。
    异常向上抛给 runner，由 runner 按 miss 计数。
    """
    k = max(1, min(int(top_k), 10))
    q = (query or "").strip()
    if not q:
        return []
    if fusion_on is None:
        fusion_on = _recall_fusion_enabled()
    results, _ = _recall_core(q, k, fusion_on=bool(fusion_on),
                              safe_annotation=True)
    return results


# ---------------------------------------------------------------------------
# 工具 5: memorycore_recall — 主动召回冷层 (只读, 冷层降权重排)
# ---------------------------------------------------------------------------

# ---- P0 只读观测探针本地 helper (零行为影响) --------------------------------

def _probe_float(value):
    """字段缺失/类型异常/非有限值容错: 一律视为 0 (严格 JSON 安全)。"""
    try:
        if isinstance(value, bool):
            num = float(value)
        else:
            num = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return num if math.isfinite(num) else 0.0


def _probe_int(value, default=0):
    """非有限/异常 candidate_count/top_k 降级, 不允许拖垮整条事件。"""
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(num):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(num)


def _probe_id(value):
    """id 契约: None -> ""; 0/False/bytes 等保留可表示文本, 不折叠。"""
    if value is None:
        return ""
    return str(value)


def _recall_channel_of(result, handle_cold_id=None, query=""):
    """S=dense 分数命中; K=keyword/fts 或本地 _lex_evidence; H=handle 直查。

    H>K>S; 本地 bigram 证据用于兼容旧冷层只返回 dense 字段的情况 (§5)。
    """
    if handle_cold_id and result.get("id") == handle_cold_id:
        return "H"
    if (_probe_float(result.get("keyword_score")) > 0
            or _probe_float(result.get("fts_score")) > 0):
        return "K"
    content = result.get("content")
    if query and isinstance(content, str) and content.strip():
        try:
            if _lex_evidence(query, content):
                return "K"
        except Exception:
            pass
    return "S"


def _k_source_of(result, query="", channel=None):
    """P1 观测: 单条结果的 K 证据来源 (只进探针事件, 不参与判断)。

    冷层 keyword/fts 分数优先; 两者都缺失/异常时才尝试本地 bigram 证据。
    任何字段缺失/类型异常一律降级为 "" (无 K 证据), 不抛异常。

    k_source 必须服从 channel 优先级: H 行 (句柄直查) 一律返回 "",
    不把已判 H 的行再染成 cold_kw/cold_kw_fts; K/S 行沿用原有证据口径。
    """
    try:
        if channel == "H":
            return ""
        if not isinstance(result, dict):
            return ""
        kw = _probe_float(result.get("keyword_score")) > 0
        fts = _probe_float(result.get("fts_score")) > 0
        if kw and fts:
            return "cold_kw_fts"
        if kw:
            return "cold_kw"
        if fts:
            return "cold_fts"
        content = result.get("content")
        if query and isinstance(content, str) and content.strip():
            try:
                if _lex_evidence(query, content):
                    return "local_lex"
            except Exception:
                pass
        return ""
    except Exception:
        return ""


def _probe_candidate_of(result):
    """候选快照白名单 (R3): 只含 id/渠道/分数/page_fault, 不含正文。"""
    try:
        return {
            "id": _probe_id(result.get("id")),
            "channel": result.get("channel"),
            "keyword_score": _probe_float(result.get("keyword_score")),
            "fts_score": _probe_float(result.get("fts_score")),
            "dense_score": _probe_float(result.get("dense_score")),
            "page_fault": bool(result.get("page_fault")),
            "handle": result.get("handle"),
        }
    except Exception:
        return {"id": "", "channel": "S", "keyword_score": 0.0,
                "fts_score": 0.0, "dense_score": 0.0,
                "page_fault": False, "handle": None}


def _json_safe_response(value):
    """MCP 返回串严格 JSON 化: 非有限 float 归 0, 不可序列化对象转文本。"""
    try:
        if isinstance(value, float):
            return value if math.isfinite(value) else 0.0
        if isinstance(value, dict):
            return {str(k): _json_safe_response(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [_json_safe_response(v) for v in value]
        if isinstance(value, (bytes, bytearray)):
            return bytes(value).decode("utf-8", errors="replace")
        return value
    except Exception:
        return str(value)


def _build_recall_probe_event(*, query, top_k, candidate_count, results,
                              page_fault, restore, latency_ms, error=None,
                              candidates=None, restored_ids=None,
                              handle_cold_id=None):
    """构造探针事件 (只入白名单字段; query 只以 sha256/len 形式存在)。

    R3: candidates/restored_ids 为 restore 前快照与成功恢复 id, page_fault 由
    调用方按快照计算, 不由 restore 后的 results 反推。
    R8: rows = list(results or []) 一次物化, 所有数组严格同长; id 不折叠
    0/False, 不可 JSON 序列化字段在探针落盘层再降级, 不丢整条事件。
    """
    try:
        if results is None:
            rows = []
        else:
            rows = list(results)
        if len(rows) > 512:
            rows = rows[:512]
        candidate_rows = [] if candidates is None else list(candidates)
        if len(candidate_rows) > 512:
            candidate_rows = candidate_rows[:512]
        restored = [] if restored_ids is None else list(restored_ids)
        # P1/F1: channel 与 k_source 同一行、同一口径; channel 先算,
        # k_source(..., channel=...) 再算, 保证 H 行 k_source 必为空。
        channels = []
        for _r in rows:
            if isinstance(_r, dict):
                _ch = _r.get("channel")
                if not _ch:
                    _ch = _recall_channel_of(_r, handle_cold_id, query)
            else:
                _ch = "S"
            channels.append(_ch)
        err = error
        if err is not None and query:
            # 即使上游异常文本意外带上 query, 探针也不落明文。
            try:
                err = str(err).replace(str(query), "<query>")
            except Exception:
                err = str(err)
        return {
            "source": "server_recall",
            "query_sha256": _probe_query_sha256(query or ""),
            "query_len": len((query or "")),
            "top_k": _probe_int(top_k, 0),
            "candidate_count": _probe_int(candidate_count, 0),
            "returned_ids": [_probe_id(_r.get("id")) if isinstance(_r, dict)
                             else _probe_id(_r) for _r in rows],
            "dense_scores": [_probe_float(_r.get("dense_score"))
                             if isinstance(_r, dict) else 0.0 for _r in rows],
            "keyword_scores": [_probe_float(_r.get("keyword_score"))
                               if isinstance(_r, dict) else 0.0 for _r in rows],
            "fts_scores": [_probe_float(_r.get("fts_score"))
                           if isinstance(_r, dict) else 0.0 for _r in rows],
            "channel": channels,
            # P1/F1: 每条结果的 K 证据来源; channel=H 时强制为空,
            # 与同位置 channel 数组严格自洽 (只进事件, 不参与判断)。
            "k_source": [_k_source_of(_r, query=query, channel=channels[_i])
                         if isinstance(_r, dict) else ""
                         for _i, _r in enumerate(rows)],
            # 与 returned_ids 同长; selected 只做 raw-selected 占位。
            "selected": [True] * len(rows),
            "page_fault": bool(page_fault),
            "restore": _probe_int(restore, 0),
            "latency_ms": round(max(0.0, _probe_float(latency_ms)), 3),
            "error": err,
            "candidate_ids": [_probe_id(_r.get("id")) if isinstance(_r, dict)
                              else _probe_id(_r) for _r in candidate_rows],
            "candidate_channels": [_r.get("channel") or "S"
                                   if isinstance(_r, dict) else "S"
                                   for _r in candidate_rows],
            "candidate_page_fault": [bool(_r.get("page_fault"))
                                     if isinstance(_r, dict) else False
                                     for _r in candidate_rows],
            "candidates": [_probe_candidate_of(_r) if isinstance(_r, dict)
                           else _r for _r in candidate_rows],
            "restored_ids": [_probe_id(_rid) for _rid in restored],
        }
    except Exception:
        # 观测构造自身失败也不得改变工具行为。
        return {"source": "server_recall", "error": "probe_build_error"}

@mcp.tool()
def memorycore_recall(query: str = "", top_k: int = 3, handle: str = "") -> str:
    """主动召回冷层记忆 (只读, 不写冷层/不 remember/update/forget)。

    支持 CACHE-POLICY-V2 的句柄直查: 传入 handle 时先用本地 stub 目录定位
    cold_id, 再按 stub 主题召回并标注 page_fault/channel; 常规 query 保留冷层
    降权重排。冷层读取统一 bump=False (只读)。

    P0 观测: 返回前向 core.recall_probe 落一条独立 JSONL (默认关闭);
    不改返回顺序/内容, 不新增 MCP 工具。

    口径: 返回内容为候选记忆 (按相似度排序, 未经核实), 不保证与查询
    意图相关; 涉及精确事实/数值时请先交叉核对, 不应直接作为事实依据。

    Args:
        query: 查询内容 (自然语言, 语义召回)
        top_k: 返回条数 (默认 3, 最大 10)
        handle: 可选本地 stub 句柄 (来自常驻目录 [#xxxx]); 提供时绕过 0.48
    Returns:
        JSON: {"results": [...], "handle": ..., "page_fault": bool}
              冷层不可达时 {"error": "..."}
    """
    _probe_t0 = time.perf_counter()
    _probe_k = 0
    _probe_candidate_count = 0
    _probe_candidate_count_sink = [0]
    q = ""
    try:
        k = max(1, min(int(top_k), 10))
        _probe_k = k
        handle = (handle or "").strip()
        handle_map = {}
        for _t in ("memory", "user"):
            for _e in _store.entries(_t):
                _m = _metastore_for(_t).get_entry(_e) or {}
                if _m.get("type") == "stub" and _m.get("cold_id"):
                    _h = _m.get("handle") or ""
                    if _h:
                        handle_map[_h] = {"target": _t, "stub": _e, "meta": _m}
        q = (query or "").strip()
        if handle:
            hit = handle_map.get(handle)
            if not hit:
                record_recall_probe(_build_recall_probe_event(
                    query=q, top_k=k, candidate_count=0, results=[],
                    page_fault=False, restore=0,
                    latency_ms=(time.perf_counter() - _probe_t0) * 1000.0,
                    error=f"handle not found: {handle}"))
                return json.dumps({"error": f"handle not found: {handle}",
                                   "handle": handle}, ensure_ascii=False)
            q = _stub_topic(hit["stub"]) or q
        if q:
            log_activity_query(q)
        if not q:
            record_recall_probe(_build_recall_probe_event(
                query=q, top_k=k, candidate_count=0, results=[],
                page_fault=False, restore=0,
                latency_ms=(time.perf_counter() - _probe_t0) * 1000.0))
            return json.dumps({"results": [], "handle": handle,
                               "page_fault": False}, ensure_ascii=False)
        # P2: 单一召回内核, 与 recall_readonly 同源; handle 直查不启用融合。
        _handle_cold_id = (hit["meta"].get("cold_id")
                           if (handle and hit) else None)
        _fusion_on = bool(not handle and _recall_fusion_enabled())
        results, _ = _recall_core(
            q, k, fusion_on=_fusion_on, handle_cold_id=_handle_cold_id,
            candidate_count_sink=_probe_candidate_count_sink)
        _probe_candidate_count = _probe_candidate_count_sink[0]
        # Phase 4: 召回结果命中 stub 的 cold_id → 自动恢复全文到热层。
        # R3: restore 前先快照候选; page_fault 按快照计算, restore 是独立成功计数。
        _probe_candidates = [_probe_candidate_of(_r) if isinstance(_r, dict)
                             else {"id": _probe_id(_r)} for _r in results]
        _probe_page_fault = bool(handle) or any(
            bool(_c.get("page_fault")) for _c in _probe_candidates)
        _restore_before = len(results)
        _restore_before_ids = {_probe_id(_r.get("id")) for _r in results
                               if isinstance(_r, dict)}
        try:
            results = restore_stubs_from_results(
                _store, {t: _metastore_for(t) for t in ("memory", "user")}, results)
        except Exception:
            pass  # 恢复失败不影响召回返回
        _restore_after_ids = {_probe_id(_r.get("id")) for _r in results
                              if isinstance(_r, dict)}
        _restored_ids = [rid for rid in
                         [(_r.get("id") if isinstance(_r, dict) else _r)
                          for _r in (_probe_candidates)]
                         if _probe_id(rid) in _restore_before_ids
                         and _probe_id(rid) not in _restore_after_ids]
        _restored = max(0, _restore_before - len(results))
        record_recall_probe(_build_recall_probe_event(
            query=q, top_k=k, candidate_count=_probe_candidate_count,
            results=results, page_fault=_probe_page_fault, restore=_restored,
            candidates=_probe_candidates,
            restored_ids=_restored_ids or (
                [] if _restored == 0 else _restored_ids),
            handle_cold_id=_handle_cold_id,
            latency_ms=(time.perf_counter() - _probe_t0) * 1000.0))
        return json.dumps({"results": _json_safe_response(results),
                           "handle": handle,
                           "page_fault": bool(handle),
                           "mode": "handle" if handle else "semantic"},
                          ensure_ascii=False)
    except Exception as e:
        # 探针自身 fail-silent; 不吞掉工具原有错误返回。
        _probe_candidate_count = _probe_candidate_count_sink[0]
        record_recall_probe(_build_recall_probe_event(
            query=q, top_k=_probe_k,
            candidate_count=_probe_candidate_count, results=[],
            page_fault=False, restore=0,
            latency_ms=(time.perf_counter() - _probe_t0) * 1000.0,
            error=str(e)[:280]))
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def __getattr__(name: str):
    """兼容旧 Python 属性名 (运行时拼装, 发布树不落地旧 token).

    MCP 工具名同样由 _STORE_FACT_LEGACY_TOOL 显式保留, 调用方行为不变。
    """
    if name == _STORE_FACT_LEGACY_TOOL:
        return memorycore_store_entry
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    mcp.run(transport="stdio")
