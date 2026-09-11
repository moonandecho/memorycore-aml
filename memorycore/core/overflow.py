#!/usr/bin/env python3
"""core/overflow.py — 六步溢流流程 v2 完整版

把本地热层 (MEMORY.md / USER.md) 中低频/过时数据安全迁移到冷层 Mnemosyne。

六步:
  1. 容量基线统计
  2. 同主题查重 (冷层已有 → 不重复写)
  3. 过时事实过滤 (STALE → forget 冷层 + 删本地)
  4. 同类事实合并 (本地碎片合并再下沉)
  5. 安全溢流写入 (先写冷层确认 → 再删本地)
  6. 验证 (返回统计)
"""
from __future__ import annotations

import difflib
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .classifier import classify, classify_user_pref, should_keep_local, HOT, COLD, STALE
from . import llm_config  # noqa: E402  LLM single source of truth (lazy+observable+guards)
from . import llm_rot  # noqa: E402  LLM candidate rotation (final-audit low-risk D2 tail-starvation fix)
from .config import (
    STATE_TTL_DAYS, RULE_COMPRESS_DAYS,
    SOFT_THRESHOLD, HARD_THRESHOLD, TARGET_RATIO,
    RULE_RETYPE_DAYS, RULE_RETYPE_MIN_DONE_MARKERS,
    RULE_RETYPE_DONE_MARKERS, RULE_RETYPE_BEHAVIOR_MARKERS,
    RULE_STUB_IDLE_DAYS, IMPORTANCE_PROTECT, MAX_STUB_PER_RUN,
    STUB_MAX_CHARS, STUB_PREFIX, STUB_GC_MIN_AGE_DAYS,
    ACTIVITY_WINDOW_DAYS, CROSS_DEDUP_MIN_IDLE_DAYS,
    CLUSTER_EMBED_THRESHOLD,
    RULE_BUDGET_CHARS, RULE_MIN_RESIDENCY_DAYS,
    WEIGHT_INIT, WEIGHT_HIT_INCREMENT, WEIGHT_MAX, WEIGHT_HALF_LIFE_DAYS,
    WEIGHT_PROTECT_MULT, WEIGHT_KWSINK_MULT, MAX_EVICT_PER_RUN,
    HIT_STRONG_COS, HIT_WEAK_COS, HIT_STRONG_INCREMENT, HIT_WEAK_INCREMENT,
    HIT_CAP_PER_SCAN, LEX_EVIDENCE_BIGRAMS,
    FRESH_QUERY_SCAN_CAP, EMBED_BATCH_MAX, EMBED_TIMEOUT,
    EMBED_MODEL, MEMORY_FILE, USER_FILE,
)
# E8 (2026-09-12): ACTIVITY_LOG_ENABLED / RULE_BUDGET_ENABLED / HIT_WEAK_MODE /
# EMBED_BACKEND delegate lazily via module __getattr__ to core.config.
from . import config as _cfg
log = logging.getLogger("memorycore.overflow")

from .metadata import (MetaStore, entry_age_days, parse_embedded_date,
                       _parse_iso, load_recent_queries,
                       load_recent_queries_with_ts)

# ---- 相似度阈值 ------------------------------------------------------------

_SAME_FACT_RATIO = 0.85       # 内容几乎相同 → 同一事实
_SIMILAR_TOPIC_RATIO = 0.50   # 同主题但细节不同 → 需 merge
_RECALL_SCORE_SAME = 0.80     # recall dense_score >= 此值视为高度匹配
_RECALL_SCORE_SIMILAR = 0.48  # recall dense_score >= 此值视为同主题
_RECALL_TOP_K = 3             # 查重时的召回数
_MIN_TEXT_RATIO = 0.15        # 最低字面相似度门禁 (防向量误匹配)

# ---- 反转检测 (2026-08-05: 写入侧反转覆盖, 与 maintenance 一致) ------------

_REVERSAL_NEG_WORDS = ["不", "没", "无", "非", "否", "别", "不再",
                       "停止", "取消", "不要", "拒绝", "禁", "讨厌",
                       "反对", "不喜", "不爱", "不感兴趣"]
_REVERSAL_WEAK_NEG = ["不太", "不大", "不怎么", "未必", "难免", "不常", "不同", "不仅", "不过", "不可", "不必", "不止", "不再"]


def _topic_overlap(a: str, b: str) -> bool:
    """判定两条是否同主题 (与 maintenance _is_reversal_pair 判定一致)。

    bigram 覆盖率 >= 0.55 或 ratio >= 0.40。
    """
    if not a or not b:
        return False
    na = _norm_sentence(a)
    nb = _norm_sentence(b)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    if ratio >= 0.40:
        return True
    bg_b = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if _bigram_coverage(na, bg_b) >= 0.55:
        return True
    return False

# ---- 用户偏好摘要锚点 (P4, 2026-08-05 设计定稿 + 实验验证) ------------------

_ANCHOR_PREFIX = "[用户偏好摘要]"   # 内容前缀: 识别/查重/召回锚点
_ANCHOR_IMPORTANCE = 0.8        # 高 importance → prefetch 分层注入高档
_ANCHOR_MAX_CHARS = 800         # 锚点长度上限 (超限截断保留头部)
_ANCHOR_QUERY = _ANCHOR_PREFIX  # P3-4: 从 _ANCHOR_PREFIX 派生 (原硬编码 "[用户偏好摘要]")


# ---- Phase 3: rule 失效信号 (2026-08-20 设计定稿"分层保护") -----------------
# 设计: /tmp/rule-stale-design.md
# 约束 1 修正: B 类按失效证据分层放弃; A 类/红线类/importance≥0.9 绝不误伤。

# S6 保护线: A 类元行为准则 (每轮适用, 主题活性信号无判别力) —
# 与 classifier strong_keep_markers/interact_words 同源 (词表可校准, 设计 §4.6)
_RULE_META_MARKERS = [
    "行为准则", "交互习惯", "写作风格", "回答风格", "措辞", "汇报", "沟通",
    "大白话", "结论先行", "验证", "准确", "严谨", "覆盖", "抑郁", "信任",
    "尊重", "纠正", "红线", "零容忍",
]
# S6 红线类 (硬性词汇, 不论 A/B 类) — 绝对保护
_RULE_REDLINE_MARKERS = ["红线", "零容忍", "绝不", "禁止", "纠正"]

# S3 嵌入通道 (ollama qwen3, prefetch 同款; 不可用 → 纯词法降级)
_EMBED_URL = os.environ.get("MEMORYCORE_EMBED_URL", "http://localhost:11434")
_EMBED_MODEL = os.environ.get("MEMORYCORE_EMBED_MODEL", "qwen3-embedding:0.6b")

# S4: 单轮休眠判定评估的候选上限 (LLM 调用量护栏)
_STUB_EVAL_CAP = 10


def _is_protected_rule(entry: str, meta: dict) -> bool:
    """S6: A 类/红线类/高 importance → 保护判定 (不被直接 stub/retype/跨层删)。

    只允许合并/压缩 (信息保留路径); 经 LRU 挤权仍可能退役为 stub
    (×WEIGHT_PROTECT_MULT 更难挤, 非豁免)。"""
    if meta.get("importance", 0.8) >= IMPORTANCE_PROTECT:
        return True
    if any(kw in entry for kw in _RULE_META_MARKERS):
        return True
    if any(kw in entry for kw in _RULE_REDLINE_MARKERS):
        return True
    return False


def _rule_retype_eligible(entry: str) -> bool:
    """S2: rule 完成态复核资格 — 内嵌日期 ≥60d + ≥2 完成态词 + 零行为指令词。"""
    d = parse_embedded_date(entry)
    if d is None:
        return False
    days = (datetime.now(timezone.utc) - d).days
    if days < RULE_RETYPE_DAYS:
        return False
    hits = [m for m in RULE_RETYPE_DONE_MARKERS if m in entry]
    # 嵌套去重 ("退役" ⊂ "已退役" 只算一次)
    distinct = [m for m in hits
                if not any(o != m and m in o for o in hits)]
    if len(distinct) < RULE_RETYPE_MIN_DONE_MARKERS:
        return False
    if any(m in entry for m in RULE_RETYPE_BEHAVIOR_MARKERS):
        return False
    return True


def _try_cross_layer_dedup(store, client, target: str, entry: str,
                           stat: dict) -> bool:
    """S5 (L1): 冷层已有等价全文 (same 级匹配) → 删本地 (信息零丢失)。"""
    try:
        existing = _recall_safe(client, entry)
    except Exception:
        return False
    if not existing:
        return False
    matched = _find_best_match(entry, existing)
    if not matched or matched["level"] != "same":
        return False
    _safe_remove_local(store, target, entry, stat)
    if entry not in store.entries(target):
        stat["overflowed"] += 1
        return True
    return False


def _make_stub(entry: str) -> str:
    """S4: stub 指针 (≤STUB_MAX_CHARS, 词法生成零 LLM 依赖)。

    格式: [规则指针]{主题词≤10字}→recall("{主题词}") — 指针+召回钩子。
    """
    kw = re.sub(r"\s+", "", (entry or "").strip().split("。")[0][:10])
    if not kw:
        kw = re.sub(r"\s+", "", (entry or "").strip()[:10])
    stub = f"{STUB_PREFIX}{kw}→recall(\"{kw}\")"
    if len(stub) > STUB_MAX_CHARS:
        stub = stub[:STUB_MAX_CHARS - 1] + ")"
    return stub


def _llm_judge_dormant(entries: List[str],
                       queries: List[str]) -> Dict[str, bool]:
    """S4: LLM 判休眠 (≤5 条/批, 复用 config LLM 通道)。

    返回 {entry: dormant}; 失败/解析不过 → 该条不出现 (调用方按活跃处理)。
    prompt 硬约束: 拿不准判活跃; 只判主题是否被讨论过, 不判价值。
    """
    if not entries:
        return {}
    qs = "\n".join(f"- {q[:120]}" for q in queries[-60:])[:1500]
    out: Dict[str, bool] = {}
    import json
    for i in range(0, len(entries), 5):
        chunk = entries[i:i + 5]
        cfg = llm_config.acquire("休眠判定")
        if cfg is None:
            # 未配置/上限/退避 → 已判定部分生效, 其余按活跃 (保守方向)
            return out
        items = "\n".join(
            f'<entry index="{j}">{e[:300]}</entry>'
            for j, e in enumerate(chunk))
        prompt = (
            "你是记忆治理助手。判断下列记忆准则涉及的主题, 近期是否被用户讨论过。\n"
            f"<queries>近 30 天用户消息样本:\n{qs}\n</queries>\n"
            f"<entries>{items}</entries>\n"
            "要求:\n"
            '1. 只判断"准则涉及的主题是否在近期查询中被讨论/涉及", 不判断准则价值\n'
            "2. 拿不准一律判活跃 (dormant=false)\n"
            "3. 不添加/不修改任何事实\n"
            '4. 只输出 JSON: {"results": [{"entry_index": 0, '
            '"dormant": true, "reason": "..."}]}'
        )
        try:
            payload = {
                "model": cfg.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 800,
                "response_format": {"type": "json_object"},
            }
            data = llm_config.chat(cfg, payload)
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
            results = json.loads(content).get("results", [])
            for r in results:
                idx = r.get("entry_index")
                d = r.get("dormant")
                if (isinstance(idx, int) and 0 <= idx < len(chunk)
                        and isinstance(d, bool)):
                    out[chunk[idx]] = d
            llm_config.note_success()
        except llm_config.LLMError as e:
            llm_config.note_failure(e.category, e.detail)
            return out  # 退避: 剩余批次全部跳过 (评审 C2)
        except Exception:
            llm_config.note_failure("bad_response")
            return out
    return out


def _rule_topic_dormant(entry: str, queries: List[str]) -> bool:
    """S4 休眠证据链: 词法活跃→False; 词法不活跃→LLM 确认; 失败→False (活跃)。

    休眠是负断言: 任何证据不足一律按活跃处理, 宁可不动绝不误沉。"""
    if not queries:
        return False
    for q in queries:
        if _topic_overlap(q, entry) or _topic_overlap(entry, q):
            return False  # 词法活跃 (零成本快筛)
    res = _llm_judge_dormant([entry], queries)
    return bool(res.get(entry, False))


def _plan_stub_candidates(metastore, client, entries: List[str]) -> List[str]:
    """L2 预规划: B 类 + idle ≥ RULE_STUB_IDLE_DAYS + 休眠确认 → 候选 (≤3)。

    排序: 闲置最久优先, 同闲置字符长的先沉 (单位省字最高)。
    """
    if not _cfg.ACTIVITY_LOG_ENABLED:
        return []
    queries = load_recent_queries()
    cands = []
    for e in entries:
        meta = metastore.get_entry(e)
        if not meta or meta.get("type") != "rule":
            continue
        if _is_protected_rule(e, meta):
            continue
        age = entry_age_days(meta)
        if age is None or age < RULE_STUB_IDLE_DAYS:
            continue
        cands.append((age, len(e), e))
    cands.sort(key=lambda t: (-t[0], -t[1]))
    picked: List[str] = []
    for _, _, e in cands[:_STUB_EVAL_CAP]:
        if len(picked) >= MAX_STUB_PER_RUN:
            break
        if _rule_topic_dormant(e, queries):
            picked.append(e)
    return picked


def _handle_rule_retype(store, client, target: str, entry: str, meta: dict,
                        metastore, stat: dict, anchor_parts: List[str]) -> None:
    """S2 (L1): 完成态复核 — 重盖 state (origin=retype_overflow) → TTL 下沉。

    安全顺序: 盖章 → 冷迁移 (冷层确认成功才删本地)。
    冷层失败 → 恢复原 rule 章 (原时间戳/importance/origin), 下次再试。
    """
    d = parse_embedded_date(entry)
    try:
        metastore.stamp(entry, "state", written_at=d, origin="retype_overflow")
    except Exception:
        pass  # F1 语义: 盖章失败不阻塞 (下次 reconcile 兜底)
    before = len(store.entries(target))
    if target == "user" and classify_user_pref(
            entry, sentence_level=True) == "sink":
        anchor_parts.append(entry)
    _handle_cold_migration(store, client, target, entry, stat)
    if len(store.entries(target)) < before:
        stat["aged_sunk"] += 1
        stat["retyped"] += 1
        return
    # 回退: 恢复原 rule 章 (设计 §4.2 失败回退)
    # L2 (2026-08-26): 透传 weight/last_active_at — 否则回退重盖
    # 把 rule 的 weight 重置为 1.0、last_active_at 刷成 now (stamp 默认),
    # 冷层反复故障期间候选规则权重被反复"洗白", 排序退化。
    try:
        metastore.stamp(
            entry, "rule",
            written_at=(_parse_iso(str(meta["written_at"]))
                        if meta.get("written_at") else None),
            updated_at=(_parse_iso(str(meta["updated_at"]))
                        if meta.get("updated_at") else None),
            importance=meta.get("importance", 0.8),
            origin=meta.get("origin", "legacy"),
            weight=float(meta.get("weight") or WEIGHT_INIT),
            last_active_at=(_parse_iso(str(meta["last_active_at"]))
                            if meta.get("last_active_at") else None),
        )
    except Exception:
        pass


def _handle_rule_stub_sink(store, client, target: str, entry: str,
                           metastore, stat: dict,
                           anchor_parts: List[str]) -> None:
    """S4 (L2): 休眠 B 类 rule → 全文先写冷层确认 → 本地替换为 stub 指针。

    安全顺序: 冷层保有全文 (P8: remember 前查重, same 不重复写/
    similar merge-update) → replace 本地; 任一失败 → 原条目原样 (errors+1)。
    Phase 4: 捕获冷层 memory_id → stub meta 写 cold_id (召回恢复链路用, 见
    restore_stubs_from_results)。
    """
    if entry not in store.entries(target):
        return  # 已被本轮其他动作处理 (如 S2/S5), 保守跳过
    # P8 (2026-09): remember 前查重 — 冷层已有等价全文 (same) 不重复写,
    # similar merge-update; cold_id 一律指向冷层真实存在的 id。
    try:
        cold_ok, cold_id = _cold_write_with_dedup(client, entry)
    except Exception:
        stat["errors"] += 1
        return  # 冷层不可达/查重异常 → 保留原样, 不 stub (保守语义)
    if not cold_ok:
        stat["errors"] += 1
        return
    stub = _make_stub(entry)
    if store.replace(target, entry, stub).get("success"):
        try:
            # L3: stub meta 记录原 importance — 恢复时透传保护线
            orig_imp = float((metastore.get_entry(entry) or {}).get(
                "importance") or 0.8)
            metastore.stamp(stub, "stub", origin="stub_sink",
                            cold_id=cold_id or None, importance=orig_imp)
        except Exception:
            pass  # 盖章失败下次 reconcile 补 (STUB_PREFIX 识别)
        stat["stubbed"] += 1
        if target == "user":
            anchor_parts.append(entry)  # 偏好全文进锚点 (与下沉同语义)
    else:
        stat["errors"] += 1


def _stub_gc(store, metastore, target: str, stat: dict) -> None:
    """§6: stub 回收 — 最老优先删本地指针, ≤MAX_STUB_PER_RUN/轮。

    只删指针: 冷层零调用 (全文不受影响, forget 零调用)。
    年龄 < STUB_GC_MIN_AGE_DAYS 的指针不回收 (防刚建即被 GC 抖振)。
    """
    entries = store.entries(target)
    stubs = []
    for e in entries:
        m = metastore.get_entry(e)
        if m and m.get("type") == "stub":
            age = entry_age_days(m)
            stubs.append((age if age is not None else 9999, e))
    stubs.sort(key=lambda t: -t[0])  # 最老优先
    removed = 0
    for age, e in stubs:
        if removed >= MAX_STUB_PER_RUN:
            break
        if age < STUB_GC_MIN_AGE_DAYS:
            continue
        if store.usage_pct(target) < HARD_THRESHOLD * 100:
            break
        before = len(store.entries(target))
        _safe_remove_local(store, target, e, stat)
        if len(store.entries(target)) < before:
            stat["stub_gc"] += 1
            removed += 1


# S3 嵌入通道 (可选增强; 不可用 → 纯词法降级, 溢流永不阻塞)
def _embed_batch(texts: List[str]) -> Optional[Dict[str, List[float]]]:
    """ollama /api/embed 批量嵌入; 不可用/失败 → None (降级纯词法)。"""
    if not texts:
        return None
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            _EMBED_URL.rstrip("/") + "/api/embed",
            data=json.dumps({"model": _EMBED_MODEL, "input": texts}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        vecs = data.get("embeddings") or []
        if len(vecs) != len(texts):
            return None
        return {t: v for t, v in zip(texts, vecs)}
    except Exception:
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    """向量余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na > 0 and nb > 0 else 0.0


# ---- 主入口 ----------------------------------------------------------------

def run_overflow(store, client, target: str) -> dict:
    """对单个文件 (memory / user) 执行完整六步溢流。

    Args:
        store: LocalStore 实例
        client: MnemosyneClient 实例
        target: "memory" | "user"

    Returns:
        dict: {overflowed, updated, deleted, merged, kept,
               usage_before, usage_after, chars_before, chars_after, errors}
    """
    stat: Dict[str, Any] = {
        "overflowed": 0,
        "updated": 0,
        "deleted": 0,
        "merged": 0,
        "compressed": 0,  # B: LLM 压缩条数 (2026-08-07)
        "kept": 0,
        "errors": 0,
        "aged_sunk": 0,          # Phase 2: 因年龄到期退役的 state 条目数
        "metadata_stamped": 0,   # Phase 2: reconcile 补盖的 legacy 条目数
        "stubbed": 0,            # Phase 3 S4: 休眠 B 类 rule → stub 指针数
        "stub_gc": 0,            # Phase 3 §6: 回收的 stub 指针数
        "retyped": 0,            # Phase 3 S2: rule → state 完成态复核数
        "trash_fail": 0,         # E3: recycle-bin write failures (recoverability alert)
        "embed_fail": 0,         # E5: embedding failures (activity/merge paths)
    }

    # LLM guard session (review C2): per-run call cap + fail backoff +
    # observable three states; close() writes stat["llm"] (MCP tool output /
    # weekly report readable).
    # Low-risk fix (final audit): close in finally — exception paths also
    # write stat["llm"] and reset the contextvar (no leak to a dead guard).
    llm_guard = llm_config.start_session(stat=stat, name="overflow")
    try:
        return _run_overflow(store, client, target, stat)
    finally:
        llm_guard.close()


def _run_overflow(store, client, target: str, stat: Dict[str, Any]) -> dict:
    """run_overflow body (guard-session lifecycle owned by run_overflow)."""

    # P4: 收集本次下沉的用户偏好内容, 溢流末统一更新冷层摘要锚点
    anchor_parts: List[str] = []

    # ---- Step 1: 容量基线统计 --------------------------------------------
    entries = store.entries(target)
    if not entries:
        stat["usage_before"] = f"{store.usage_pct(target)}%"
        stat["usage_after"] = f"{store.usage_pct(target)}%"
        stat["chars_before"] = 0
        stat["chars_after"] = 0
        return stat

    stat["chars_before"] = store.char_count(target)
    stat["usage_before"] = f"{store.usage_pct(target)}%"

    # ---- Step 0 (仅 user): 长条目拆分 (B 溢流治理) -------------------
    # 对 USER.md 长条目 (>150字) 逐句分类, 核心句合并留本地, 长尾句沉冷层。
    # 安全顺序: 先写冷层确认 stored → 再替换本地; 任一失败 → 本地保留不丢。
    if target == "user":
        split_new_entries = []
        for entry in entries:
            if len(entry) <= 150:
                split_new_entries.append(entry)
                continue
            # 按句切分
            raw_sentences = re.split(r"[。！？;；\n]", entry)
            sentences = [s.strip() for s in raw_sentences if s.strip()]
            if not sentences:
                split_new_entries.append(entry)
                continue
            core_sents, sink_sents = [], []
            for s in sentences:
                if classify_user_pref(s, sentence_level=True) == "core":
                    core_sents.append(s)
                else:
                    sink_sents.append(s)
            sink_chars = sum(len(s) for s in sink_sents)
            # 值得拆: 可沉 >= 30% 且 >= 40 字
            if sink_chars < max(40, len(entry) * 0.3):
                split_new_entries.append(entry)  # 不值得拆, 整条保留
                continue
            # 下沉: 所有 sink 句先尝试冷迁移 (原子性: 任一失败 → 整条保留原样)
            all_sink_ok = True
            for s in sink_sents:
                cold_ok = False
                try:
                    existing = _recall_safe(client, s)
                except Exception:
                    existing = None
                if existing:
                    matched = _find_best_match(s, existing)
                    if matched and matched["level"] == "same":
                        cold_ok = True  # 冷层已有, 算已存
                    elif matched and matched["level"] == "similar":
                        merged_s = _merge_two_entries(s, matched["content"])
                        try:
                            r = client.update(matched["id"], merged_s)
                            if r.get("status") == "updated":
                                cold_ok = True
                        except Exception:
                            pass
                if not cold_ok:
                    try:
                        r = client.remember(s, importance=0.6, scope="global")
                        if r.get("status") == "stored":
                            cold_ok = True
                    except Exception:
                        pass
                if not cold_ok:
                    all_sink_ok = False
                    break  # 任一 sink 句失败 → 中止, 整条保留

            if not all_sink_ok:
                # 任一 sink 句冷迁移失败 → 整条保留原样 (不拆不沉)
                split_new_entries.append(entry)
                stat["errors"] += 1
                continue

            # 全部 sink 句冷迁移成功 → 应用拆分
            stat["overflowed"] += len(sink_sents)
            anchor_parts.extend(sink_sents)  # P4: 下沉的偏好长尾进锚点
            if core_sents:
                new_entry = "。".join(core_sents) + "。"
                split_new_entries.append(new_entry)
            # 核心句为空 + sink 全成功 → 纯长尾条目全部下沉, 本地不留 (预期行为)
        # 重建文件反映拆分结果
        _rebuild_file(store, target, split_new_entries)
        entries = split_new_entries
        stat["chars_after_split"] = store.char_count(target)

    # ---- Step 4: 同类事实合并 (先于下沉) ---------------------------------
    merged_entries, merge_count = _merge_local_fragments(entries, stat)
    if merge_count > 0:
        _rebuild_file(store, target, merged_entries)
        stat["merged"] = merge_count
        entries = merged_entries

    # ---- Step 0.5 (Phase 2): 元数据 reconcile — legacy 补盖 / 孤儿 GC ----
    # 放在拆分/合并之后 (条目定稿, 拆分与合并产物也纳入), 幂等;
    # 只写 sidecar 不碰 .md。之后主循环按元数据优先决策。
    metastore = MetaStore(target, memory_path=store.memory_path,
                          user_path=store.user_path)
    try:
        meta_stat = metastore.reconcile(entries)
        stat["metadata_stamped"] = meta_stat.get("stamped", 0)
    except Exception:
        # F1 修复 (终审): sidecar 故障 (磁盘满/锁文件不可开) 不阻塞溢流 —
        # 降级 legacy 关键词路径, get_entry 读损坏文件亦返回 None。
        stat["errors"] += 1
        stat["metadata_stamped"] = 0

    # ---- Phase 4 Step A: 活性命中扫描 (语义两级判定; 失败降级不阻塞) ----
    try:
        apply_activity_hits({target: metastore}, {target: entries}, client, stat)
    except Exception as e:
        # E4 可见化: 活性扫描失败不再零信号 (原 except: pass 静默)
        stat["errors"] += 1
        log.warning("ACTIVITY: 活性命中扫描失败 / activity-hit scan failed (%s) → no new signal this round, historical weights continue", e)

    # ---- L2 预规划 (Phase 3): 高压下休眠 B 类 rule → stub 候选 ------------
    stub_candidates: Set[str] = set()
    if store.usage_pct(target) >= HARD_THRESHOLD * 100:
        try:
            stub_candidates = set(_plan_stub_candidates(
                metastore, client, entries))
        except Exception:
            stub_candidates = set()  # 规划失败 → 保守不 stub (机制降级)

    # ---- Step 2+3+5: 逐条处理 --------------------------------------------
    # L2 low-risk closure (2026-09-12): rotation cursor — per-round LLM cap
    # truncation leaves no persistent marker; when head candidates stay
    # "success-but-unresolvable" (compress validation fails), the next round
    # rebuilt from stable file order would re-consume the budget from the
    # same head and starve the tail permanently (measured pre-fix). Rotation
    # guarantees every candidate is attempted at least once per ceil(N/C)
    # rounds.
    entries = llm_rot.rotate(f"overflow:{target}", entries)
    for entry in entries:
        # Phase 2 (2026-08-16): 元数据优先 — 有元数据按"年龄+类型"退役,
        # 关键词表不参与 (根治词表两周一复发); 无元数据回退 legacy 关键词路径。
        meta = metastore.get_entry(entry)
        if meta and _handle_typed_entry(store, client, target, entry, meta,
                                        metastore, stat, anchor_parts,
                                        stub_candidates):
            continue

        if should_keep_local(entry):
            # B: 长条目压缩优先 (2026-08-07 设计定稿) — keep 但 >200 字:
            # 先尝试 LLM 压缩成精简版留本地, 原始细节沉冷层; 失败保留原样。
            if len(entry) > _COMPRESS_MIN_CHARS:
                compressed = _llm_compress(client, entry)
                if compressed is not None and compressed != entry:
                    # 校验 2: 压缩确实更短 (省字目标)
                    if len(compressed) < len(entry) * 0.8:
                        # 先沉原始细节到冷层 (原子性: 失败则保留本地原样)
                        # P8: remember 前查重 — 冷层已有等价原文不重复写
                        cold_ok = False
                        try:
                            cold_ok, _ = _cold_write_with_dedup(client, entry)
                        except Exception:
                            cold_ok = False
                        if cold_ok:
                            # F3 修复 (终审): 校验 replace 成功才计数
                            if store.replace(target, entry, compressed).get("success"):
                                stat["compressed"] += 1
                                anchor_parts.append(entry)
                                continue
                        stat["errors"] += 1
                        stat["kept"] += 1
                        continue
            stat["kept"] += 1
            continue

        decision = classify(entry, importance=0.6)
        d = decision["decision"]

        # 2026-08-09 修复: should_keep_local 已精确判定为下沉候选的条目,
        # 不再让 classify 的宽泛 HOT_KEYWORDS ("必须/要求"等) 拦回热层 —
        # 否则技术记录 (GPU 方案等含"必须独占") 会永远 kept。
        # 此时 classify 仅用于区分 STALE (过时) vs COLD (下沉)。
        if d == HOT:
            d = COLD

        if d == STALE:
            _handle_stale(store, client, target, entry, stat)
        elif d == COLD:
            if target == "user" and classify_user_pref(
                    entry, sentence_level=True) == "sink":
                anchor_parts.append(entry)  # P4: 整条下沉的用户侧偏好进锚点
            _handle_cold_migration(store, client, target, entry, stat)
        else:
            stat["kept"] += 1

    # ---- Step 5.5 (Phase 3): stub GC — 本轮未 stub 且仍高压 → 最老指针回收 --
    if (stat.get("stubbed", 0) == 0
            and store.usage_pct(target) >= HARD_THRESHOLD * 100):
        try:
            _stub_gc(store, metastore, target, stat)
        except Exception:
            pass  # GC 是机会性回收, 失败不阻塞溢流 (下次再试)

    # ---- Phase 4 Step B: 规则预算挤权 (LRU; 冷层失败 break 不丢数据) ----
    try:
        enforce_rule_budget(store, client, target, metastore, stat)
    except Exception:
        pass  # 预算挤权失败不阻塞溢流 (下轮重试)

    # ---- Step 6: 验证 ----------------------------------------------------
    if target == "user" and anchor_parts:
        _update_pref_anchor(client, store, anchor_parts, stat)
    stat["chars_after"] = store.char_count(target)
    stat["usage_after"] = f"{store.usage_pct(target)}%"
    stat["target"] = target
    return stat


# ---- Phase 2: 有元数据条目的决策 (2026-08-16) --------------------------------

def _handle_typed_entry(store, client, target: str, entry: str,
                        meta: dict, metastore, stat: dict,
                        anchor_parts: List[str],
                        stub_candidates: Optional[Set[str]] = None) -> bool:
    """有元数据条目的退役判定 (元数据优先, 关键词不参与)。

    - stub: 指针永久驻留 (回收走 Step 5.5 stub GC, 不参与放弃路径)。
    - state: age >= STATE_TTL_DAYS → 到期退役, 走 _handle_cold_migration
      安全路径 (冷层写成功才删本地); 未到期 → 暂留。
    - rule (Phase 3 分层保护, 2026-08-20):
        * S6: A 类/红线类/importance≥0.9 → 只走长压缩, 不 stub/不 retype/不跨层删
        * S2 (L1 ≥60%): 完成态复核 → retype state → TTL 下沉
        * S5 (L1 ≥60%, 闲置≥30d): 冷层已有等价全文 → 删本地
        * S4 (L2 ≥80%): 休眠确认 → 全文沉冷层 + stub 指针留本地
        * 既有: age ≥ RULE_COMPRESS_DAYS 长条目 → LLM 压缩 (失败保留原样)
    - 未知类型 → 返回 False, 调用方回退 legacy 关键词路径。

    Returns: True = 已处理 (调用方 continue); False = 回退 legacy。
    """
    etype = meta.get("type")
    age = entry_age_days(meta)

    if etype == "stub":
        stat["kept"] += 1  # 指针永久驻留; 高压回收走 Step 5.5 stub GC
        return True

    if etype == "state":
        if age is not None and age >= STATE_TTL_DAYS:
            before = len(store.entries(target))
            if target == "user" and classify_user_pref(
                    entry, sentence_level=True) == "sink":
                anchor_parts.append(entry)
            _handle_cold_migration(store, client, target, entry, stat)
            if len(store.entries(target)) < before:
                stat["aged_sunk"] += 1  # 只有条目真的离开热层才计数
            return True
        stat["kept"] += 1  # 未到期, 暂留
        return True

    if etype == "rule":
        protected = _is_protected_rule(entry, meta)
        usage = store.usage_pct(target)

        # S0 (2026-08-26 修复): 关键词视图已判可沉 (强 sink 组合) → 即时冷迁移
        # 修复前: 元数据优先旁路 should_keep_local, audit 判 keep=False 的
        # rule 条目溢流永不沉 (热层 88% 只增不减的结构性空转)
        # 2026-08-26 修正: 门槛 HARD(80%) — SOFT(60%) 会让 kw 可沉 rule 在
        # 低占用即查冷层, 绕过 S5 的 CROSS_DEDUP_MIN_IDLE_DAYS 闲置门槛
        # (test_s5_skips_recent_rule 语义: 闲置 <30 天不查冷层)。
        if (not protected and usage >= HARD_THRESHOLD * 100
                and not should_keep_local(entry)):
            _handle_cold_migration(store, client, target, entry, stat)
            return True

        # S2 (L1): 完成态复核 → retype → TTL 下沉 (冷层失败恢复原 rule 章)
        if (not protected and usage >= SOFT_THRESHOLD * 100
                and _rule_retype_eligible(entry)):
            _handle_rule_retype(store, client, target, entry, meta,
                                metastore, stat, anchor_parts)
            return True

        # S5 (L1): 跨层冗余 — 闲置 ≥ CROSS_DEDUP_MIN_IDLE_DAYS 才查冷层
        # (历史冗余面向, 省 recall 开销; 冷层已有等价全文 → 删本地零丢失)
        if (not protected and usage >= SOFT_THRESHOLD * 100
                and age is not None and age >= CROSS_DEDUP_MIN_IDLE_DAYS
                and _try_cross_layer_dedup(store, client, target, entry, stat)):
            return True

        # S4 (L2): 休眠 stub-sink (候选由 run_overflow L2 预规划给出)
        if (not protected and usage >= HARD_THRESHOLD * 100
                and stub_candidates is not None
                and entry in stub_candidates):
            _handle_rule_stub_sink(store, client, target, entry,
                                   metastore, stat, anchor_parts)
            return True

        if (age is not None and age >= RULE_COMPRESS_DAYS
                and len(entry) > _COMPRESS_MIN_CHARS):
            compressed = _llm_compress(client, entry)
            if (compressed is not None and compressed != entry
                    and len(compressed) < len(entry) * 0.8):
                # P8: remember 前查重 — 冷层已有等价原文不重复写
                cold_ok = False
                try:
                    cold_ok, _ = _cold_write_with_dedup(client, entry)
                except Exception:
                    cold_ok = False
                if cold_ok:
                    # F3 修复 (终审): 校验 replace 成功才计数/盖章
                    if store.replace(target, entry, compressed).get("success"):
                        try:
                            metastore.stamp(compressed, "rule",
                                            origin="overflow")
                        except Exception as e:
                            # F1/E9 可见化: 盖章失败不再静默 (下次 reconcile 补)
                            stat["stamp_skipped"] = stat.get("stamp_skipped", 0) + 1
                            log.debug("META: stamp failed (%s) → next reconcile heals", e)
                        stat["compressed"] += 1
                        anchor_parts.append(entry)
                        return True
                stat["errors"] += 1
                stat["kept"] += 1
                return True
        stat["kept"] += 1
        return True

    return False  # 未知类型 → legacy 回退


# ---- P4: 用户偏好摘要锚点 (2026-08-05 设计定稿) ----------------------------

def _update_pref_anchor(client, store, new_parts: List[str], stat: dict) -> None:
    """维护冷层用户偏好摘要锚点。

    用户偏好内容下沉时, 不散落丢失, 合并成一条高 importance 锚点,
    作为 prefetch 召回的用户偏好画像总索引 (隔离库实验: 8/10 偏好查询
    top-1 命中 + rerank 全部穿透 -3.5 线; 现状无锚点时仅 1/10 可达)。

    规则拼接 + 增量更新 (自包含零依赖, 关键词覆盖优先):
      - 首次建立: 并入 USER.md 当前 core 偏好 → 初始即完整画像
      - 后续溢流: 合入本次下沉的偏好内容 (句级去重)
      - 已有锚点用 recall 前缀查询定位 (实测 4/4 top-1, 避开词法盲区)
      - 失败只记 errors, 不阻塞溢流 (散条已正常下沉, 锚点是附加索引)
    """
    if not new_parts:
        return
    # 新内容内部去重
    dedup_parts = []
    for p in new_parts:
        if p not in dedup_parts:
            dedup_parts.append(p)
    # 查已有锚点
    anchor_item = None
    try:
        for m in client.recall_results(_ANCHOR_QUERY, top_k=5, bump=False):  # 遗留2: 内部锚点维护, 只读
            if _ANCHOR_PREFIX in (m.get("content") or ""):
                anchor_item = m
                break
    except Exception:
        stat["errors"] += 1
        return
    # 基础内容: 已有锚点正文 或 (首次) USER.md 当前 core 偏好
    if anchor_item:
        base = (anchor_item.get("content") or "").replace(
            _ANCHOR_PREFIX, "", 1).strip()
    else:
        base = ""
        try:
            core_entries = [
                e for e in store.entries("user")
                if classify_user_pref(e, sentence_level=True) == "core"
            ]
            base = "。".join(core_entries)
        except Exception as e:
            # E7 可见化: 锚点基础内容读取失败不再静默 (散条已沉, 索引降级)
            stat["anchor_degraded"] = stat.get("anchor_degraded", 0) + 1
            log.warning("ANCHOR: preference-anchor base read failed (%s) → rebuilt from new parts only", e)
    # 合入新内容 (句级去重)
    merged = base
    for p in dedup_parts:
        if p not in merged:
            merged = f"{merged}。{p}" if merged else p
    merged = merged.strip("。 ")
    if len(merged) > _ANCHOR_MAX_CHARS:
        merged = merged[:_ANCHOR_MAX_CHARS - 1].rstrip("。 ") + "。"
    content = f"{_ANCHOR_PREFIX} {merged}"
    # 写冷层 (update 或 remember)
    try:
        if anchor_item:
            r = client.update(anchor_item["id"], content)
            if r.get("status") == "updated":
                stat["anchor_updated"] = stat.get("anchor_updated", 0) + 1
            else:
                stat["errors"] += 1
        else:
            r = client.remember(content, importance=_ANCHOR_IMPORTANCE,
                                scope="global")
            if r.get("status") == "stored":
                stat["anchor_created"] = stat.get("anchor_created", 0) + 1
            else:
                stat["errors"] += 1
    except Exception:
        stat["errors"] += 1


# ---- Step 2+5: 冷迁移 ----------------------------------------------------

def _handle_cold_migration(store, client, target: str, entry: str,
                           stat: dict) -> None:
    """冷候选条目: 查重 → (跳过/update/remember) → 删本地。"""
    try:
        existing = _recall_safe(client, entry)
    except Exception:
        stat["errors"] += 1
        return  # 冷层不可达 → 保留本地

    if existing:
        matched = _find_best_match(entry, existing)
        if matched:
            if matched["level"] == "same":
                # 冷层已有相同事实 → 不重复写, 直接删本地
                _safe_remove_local(store, target, entry, stat)
                stat["overflowed"] += 1  # 算溢流 (已在冷层)
                return
            elif matched["level"] == "similar":
                # 反转检测 (P1-3 双向化): 本地条或冷层旧条任一侧含否定词 + 同主题 → 覆盖
                has_neg = False
                cold_content = matched["content"]
                for w in _REVERSAL_NEG_WORDS:
                    # 检查本地新条
                    if w in entry:
                        is_weak = False
                        for wn in _REVERSAL_WEAK_NEG:
                            if wn in entry and w in wn:
                                is_weak = True
                                break
                        if not is_weak:
                            has_neg = True
                            break
                    # 检查冷层旧条 (P1-3: 旧条否定+新条正向 → 正向替代)
                    if w in cold_content:
                        is_weak = False
                        for wn in _REVERSAL_WEAK_NEG:
                            if wn in cold_content and w in wn:
                                is_weak = True
                                break
                        if not is_weak:
                            has_neg = True
                            break
                if has_neg and _topic_overlap(entry, cold_content):
                    try:
                        # P1-6 + E3: 写入侧反转覆盖前, 旧条先进回收站
                        # (写成功才许 update 覆盖; 写失败 → 计数 + 中止)
                        from trash_store import TrashStore, add_observed
                        if not add_observed(
                                TrashStore(), matched["id"], cold_content,
                                "reversal_obsolete", "write_side_reversal", stat):
                            return
                        r = client.update(matched["id"], entry)
                        if r.get("status") == "updated":
                            _safe_remove_local(store, target, entry, stat)
                            stat["updated"] += 1
                            return
                    except Exception:
                        stat["errors"] += 1
                        return
                # 原合并逻辑
                merged = _merge_two_entries(entry, matched["content"])
                try:
                    r = client.update(matched["id"], merged)
                    if r.get("status") == "updated":
                        _safe_remove_local(store, target, entry, stat)
                        stat["updated"] += 1
                        return
                except Exception:
                    stat["errors"] += 1
                    return  # update 失败 -> 本地保留
            # 匹配不满足阈值或 update 返回非 updated → 降级到 remember

    # 无匹配 → 新写入冷层
    try:
        r = client.remember(entry, importance=0.6, scope="global")
        if r.get("status") == "stored":
            _safe_remove_local(store, target, entry, stat)
            stat["overflowed"] += 1
        else:
            stat["errors"] += 1
    except Exception:
        stat["errors"] += 1


# ---- Step 3: 过时处理 ----------------------------------------------------

def _handle_stale(store, client, target: str, entry: str, stat: dict) -> None:
    """过时条目: 冷层匹配条目先进回收站再 forget, 然后删本地。

    P2-1 (2026-08-23): 与治理路径 (_clean_stale 短条目) 对齐 — forget 前写入
    TrashStore, 保留 30 天恢复窗口。reason 按长度分级: ≤80 字 "stale_short",
    >80 字 "stale_long"; source_decision 统一 "rule_stale" (同治理路径语义)。
    """
    try:
        existing = _recall_safe(client, entry)
        if existing:
            reason = "stale_short" if len(entry) <= 80 else "stale_long"
            for ex in existing:
                ratio = difflib.SequenceMatcher(
                    None, entry, ex.get("content", "")
                ).ratio()
                if ratio > _SAME_FACT_RATIO:
                    try:
                        # E3: 回收站写成功才许 forget (写失败 → 计数, 源保留)
                        from trash_store import TrashStore, add_observed
                        if not add_observed(
                                TrashStore(), ex["id"], ex.get("content", ""),
                                reason, "rule_stale", stat):
                            continue
                        client.forget(ex["id"])
                    except Exception:
                        pass  # forget 失败不阻塞 (回收站已有备份)
    except Exception:
        pass  # 冷层不可达, 至少删本地

    _safe_remove_local(store, target, entry, stat)
    stat["deleted"] += 1


# ---- Step 4: 本地碎片合并 ------------------------------------------------

def _smart_ratio(a: str, b: str) -> float:
    """智能相似度: 长条目 (>200字) 额外对比头部 150 字避免尾部稀释。

    返回 max(全文 ratio, 头部 ratio)。长条目尾部细节差异不应掩盖同主题。
    """
    full = difflib.SequenceMatcher(None, a, b).ratio()
    if len(a) > 200 or len(b) > 200:
        head = difflib.SequenceMatcher(None, a[:150], b[:150]).ratio()
        return max(full, head)
    return full


def _merge_local_fragments(entries: List[str],
                           stat: dict = None) -> Tuple[List[str], int]:
    """检测本地同主题碎片并合并。返回 (merged, merge_count)。

    Phase 3 (2026-08-20): stub 指针 ([规则指针] 前缀) 不参与合并 —
    指针共享固定格式前缀, 词法相似度天然 ≥0.5, 参与合并会造成指针丢失;
    它们是透明检索钩子而非散文条目, 合并零收益 (防误伤: 设计 §6)。
    """
    if len(entries) <= 1:
        return list(entries), 0

    n = len(entries)
    skip = {i for i, e in enumerate(entries) if e.startswith(STUB_PREFIX)}
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    # Phase 3 S3: 可选嵌入通道 (词法不达标时补充同主题判定; 嵌入不可用 → 纯词法)
    # E5: 嵌入失败与 activity 路径同口径计数 (embed_fail), 不再静默纯词法
    mergeable = [e for i, e in enumerate(entries) if i not in skip]
    emb_map = _embed_batch(mergeable) if len(mergeable) >= 2 else None
    if emb_map is None and len(mergeable) >= 2 and stat is not None:
        stat["embed_fail"] = stat.get("embed_fail", 0) + 1
    for i in range(n):
        if i in skip:
            continue
        for j in range(i + 1, n):
            if j in skip:
                continue
            sim_ok = _smart_ratio(entries[i], entries[j]) > _SIMILAR_TOPIC_RATIO
            if not sim_ok and emb_map:
                va, vb = emb_map.get(entries[i]), emb_map.get(entries[j])
                if (va is not None and vb is not None
                        and _cosine(va, vb) >= CLUSTER_EMBED_THRESHOLD):
                    sim_ok = True
            if sim_ok:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        if i in skip:
            continue
        root = find(i)
        groups.setdefault(root, []).append(i)

    merged = []
    merge_count = 0
    emitted = set()
    for i in range(n):
        if i in skip:
            merged.append(entries[i])
            continue
        root = find(i)
        if root in emitted:
            continue
        emitted.add(root)
        indices = groups[root]
        if len(indices) == 1:
            merged.append(entries[indices[0]])
        else:
            group_entries = [entries[k] for k in indices]
            merged.append(_merge_group(group_entries))
            merge_count += len(indices) - 1

    return merged, merge_count


def _norm_sentence(s: str) -> str:
    """规范化句子用于去重: 去空白/标点, 保留括号内容, 小写。

    括号内容 (路径/注释/别名如 Code Drive、SMB 共享) 是语义核心,
    删除会导致同义句 \"D 盘=/home/user/D (Code Drive...)\" 与
    \"D 盘=Code Drive (path=/home/user/D...)\" 规范化后反而不同。
    """
    s = re.sub(r"[\s，。！？；;、,：:·\-—/\\=_]+", "", s)
    return s.lower()


def _bigram_coverage(short: str, long_bigrams: set) -> float:
    """短句 bigram 在长文本 bigram 集中的覆盖率 (0.0-1.0)。"""
    if len(short) < 2:
        return 0.0
    s_b = {short[i:i + 2] for i in range(len(short) - 1)}
    if not s_b:
        return 0.0
    hit = sum(1 for b in s_b if b in long_bigrams)
    return hit / len(s_b)


def _sentence_is_dup(s: str, base_norm: str, base: str) -> bool:
    """判断句子 s 是否与 base 语义重复。

    判定链: 规范化子串包含 → SequenceMatcher → bigram 覆盖率。
    覆盖率针对\"同义但语序/用词不同\"的重复 (如 D 盘两种表述),
    短句的大多数字符片段出现在主体中即视为重复; 含独特信息的
    句子覆盖率低, 会被保留。太短 (<=6 字) 不判重。
    """
    sn = _norm_sentence(s)
    if not sn:
        return True
    if len(sn) <= 6:
        return False
    if sn in base_norm or base_norm in sn:
        return True
    if difflib.SequenceMatcher(None, sn, base_norm).ratio() > 0.8:
        return True
    base_bigrams = {base_norm[i:i + 2] for i in range(len(base_norm) - 1)}
    if _bigram_coverage(sn, base_bigrams) > 0.55:
        return True
    return False


def _merge_group(entries: List[str]) -> str:
    """合并一组同主题条目: 最长为主体, 追加不重复句子。

    去重分两级:
    1. 整条级: 与主体高度相似 (ratio > 0.85) 的条目整条跳过
       (如同一事实的多条近似重复记录);
    2. 句子级: 规范化后子串包含 / SequenceMatcher / bigram Jaccard,
       与主体及已追加句子都判重, 消除逗号句内的同义重复。
    """
    if len(entries) == 1:
        return entries[0]

    sorted_entries = sorted(entries, key=len, reverse=True)
    base = sorted_entries[0]
    base_norm = _norm_sentence(base)

    kept = []
    for entry in sorted_entries[1:]:
        if _smart_ratio(entry, base) > 0.85:
            continue
        for s in re.split(r"[。！？;；\n]", entry):
            s = s.strip()
            if not s:
                continue
            if _sentence_is_dup(s, base_norm, base):
                continue
            dup = False
            for k in kept:
                if _sentence_is_dup(s, _norm_sentence(k), k):
                    dup = True
                    break
            if not dup:
                kept.append(s)

    if len(kept) >= 2 and len(sorted_entries) >= 3:
        # 方案 C: 复杂组 (≥2 条候选新句 且 ≥3 条同主题) 交给 LLM 智能整合;
        # LLM 不可用 / 超时 / 校验不过 → 返回 None, 回退下方规则拼接。
        llm_result = _llm_merge(base, kept)
        if llm_result is not None:
            return llm_result

    if kept:
        base = base.rstrip("。！？;；\n") + "。" + "。".join(kept) + "。"
    return base


def _llm_merge(base: str, new_sentences: List[str]) -> Optional[str]:
    """LLM 整合同主题组 (去重 + 保持全部独特信息)。

    边界:
    - 只做"重复整合", 不自由发挥 (prompt 硬约束: 保留全部独特信息,
      不添加/不推断/不修改事实);
    - 失败路径 (无 key / 网络错误 / 超时 / 解析失败 / 信息保留校验不过)
      一律返回 None, 由调用方回退纯规则拼接, 溢流永不阻塞。
    """
    cfg = llm_config.acquire("合并")
    if cfg is None:
        return None

    supplements = "\n".join(f"- {s}" for s in new_sentences)
    prompt = (
        "你是记忆整理助手。以下是一组同主题的记忆条目, 内容重复或互为补充:\n"
        f"<base>{base}</base>\n"
        f"<supplements>\n{supplements}\n</supplements>\n"
        "要求合并成一条简洁完整的记忆:\n"
        "1. 保留全部独特信息 (路径/数字/账号/专有名词等细节不能丢)\n"
        "2. 重复表述只保留一次, 用最清晰的一种\n"
        "3. 不添加任何新事实, 不推断, 不修改事实\n"
        '4. 只输出 JSON: {"merged": "合并后的单条文本"}'
    )

    try:
        import json

        payload = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
        }
        data = llm_config.chat(cfg, payload)
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        text = json.loads(content).get("merged", "").strip()
        llm_config.note_success()
    except llm_config.LLMError as e:
        llm_config.note_failure(e.category, e.detail)
        return None
    except Exception:
        llm_config.note_failure("bad_response")
        return None

    # 校验 1: 长度合理
    if not text or len(text) < 30 or len(text) > 5000:
        return None
    # 校验 2: 信息保留 — 规则认定的每条新句核心内容须出现在 LLM 输出中
    merged_bigrams = {
        _norm_sentence(text)[i:i + 2]
        for i in range(len(_norm_sentence(text)) - 1)
    }
    scores = []
    for s in new_sentences:
        sn = _norm_sentence(s)
        if len(sn) <= 6:
            continue
        scores.append(_bigram_coverage(sn, merged_bigrams))
    if not scores:
        return None
    if sum(scores) / len(scores) < 0.5:
        return None  # 信息保留不足, 回退规则

    return text


# ---- 辅助函数 ------------------------------------------------------------

# B: 长条目压缩阈值 (2026-08-07) — keep 且超过此长度的条目, 溢流时尝试 LLM 压缩
_COMPRESS_MIN_CHARS = 200

def _llm_compress(client, entry: str) -> Optional[str]:
    """LLM 压缩长记忆条目为精简版 (保留全部关键信息, 细节已沉冷层)。

    边界 (与 _llm_merge 同款, 遵循 LLM 增强方案 C 原则):
    - 只做\"压缩\", 不自由发挥 (prompt 硬约束: 保留路径/数字/日期/专有名词,
      不添加/不推断/不修改事实);
    - 失败路径 (无 key / 网络错误 / 超时 / 解析失败 / 信息保留校验不过 /
      压缩不省字) 一律返回 None, 由调用方保留原条目, 溢流永不阻塞。
    """
    cfg = llm_config.acquire("压缩")
    if cfg is None:
        return None

    prompt = (
        "你是记忆整理助手。下面是一条过长的记忆条目, 请压缩成精简版:\n"
        f"<entry>{entry}</entry>\n"
        "要求:\n"
        "1. 只保留核心结论和必须长期记住的关键点: 日期/时间、数字、"
        "路径、端口、专有名词、命令名、人名\n"
        "2. 主要删除对象: 背景解释、过程描述、中间步骤、过时的注记、"
        "参考文档路径、括号解释、例子细节、教训的具体经过、重复表述\n"
        "3. 若条目是行为准则/教训: 删掉举例和事故经过, 只留规则本体\n"
        "4. 目标长度: 压缩到 60-110 字 (原条目约 " + str(len(entry)) + " 字, 必须删掉 60% 以上)\n"
        "5. 压缩是删除修饰语, 不是改写事实: 所有专有名词、数字、日期、"
        "命令必须原样保留, 不添加任何新事实, 不推断, 不改变语义\n"
        "6. 保持中文, 必须自包含可读\n"
        '7. 只输出 JSON: {"compressed": "压缩后的单条文本"}'
    )

    try:
        import json

        payload = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 3000,
            # 压缩是纯提取任务, 禁用推理链 (v4-flash 推理会吃光 token 导致空输出)
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        # 压缩输出长、单条耗时高, 超时给足 45s (原实现同值)
        data = llm_config.chat(cfg, payload, timeout=45.0)
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        text = json.loads(content).get("compressed", "").strip()
        llm_config.note_success()
    except llm_config.LLMError as e:
        llm_config.note_failure(e.category, e.detail)
        return None
    except Exception:
        llm_config.note_failure("bad_response")
        return None

    # 校验 1: 长度合理 (不能过短丢语义, 不能反而变长)
    if not text or len(text) < 30:
        return None
    if len(text) >= len(entry):
        return None

    # 校验 2: 信息保留 — 原条目关键 bigram 须出现在压缩输出中
    orig_norm = _norm_sentence(entry)
    comp_norm = _norm_sentence(text)
    if len(orig_norm) <= 6:
        return None
    coverage = _bigram_coverage(orig_norm, {
        comp_norm[i:i + 2]
        for i in range(len(comp_norm) - 1)
    })
    if coverage < 0.45:
        return None  # 信息保留不足, 回退保留原条目

    return text


def _recall_safe(client, entry: str) -> List[Dict[str, Any]]:
    """安全调用 recall_results, 截前 200 字作查询。"""
    # P3-5: 长条目用首尾拼接 (前150+后100), 保留首部语义 + 尾部关键信息
    if len(entry) > 250:
        query = entry[:150] + " " + entry[-100:]
    else:
        query = entry[:200]
    # 遗留2 (2026-08-23): 内部查重召回只读 — _recall_safe 仅服务溢流内部
    # 决策路径 (跨层查重/下沉匹配/冷迁移/过时处理), 不服务用户召回路径
    # (用户查询走 server.py recall 工具, 默认 bump=True 不受影响);
    # bump=False 避免污染 last_recalled (与 P1-1 同根因)。
    return client.recall_results(query, top_k=_RECALL_TOP_K, bump=False)


def _find_best_match(entry: str,
                     candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """在召回候选中找最佳匹配。返回 {level, id, content, dense_score} 或 None。"""
    best = None
    best_score = 0.0
    best_ratio = 0.0

    for c in candidates:
        c_content = c.get("content", "")
        if not c_content:
            continue
        dense_score = c.get("dense_score", 0)
        text_ratio = difflib.SequenceMatcher(None, entry, c_content).ratio()
        # 加权组合: dense_score (语义) + text_ratio (字面), 避免单信号虚高
        combined = 0.6 * dense_score + 0.4 * text_ratio

        if combined > best_score:
            best_score = combined
            best_ratio = text_ratio
            best = {"id": c.get("id"), "content": c_content,
                    "dense_score": dense_score}

    # 门禁: 即使向量分很高, 字面完全不匹配也拒掉 (防 recall 误召回)
    if best is not None and best_ratio < _MIN_TEXT_RATIO:
        best = None

    if best is None or best_score < _RECALL_SCORE_SIMILAR:
        return None

    if best_score >= _RECALL_SCORE_SAME or best_ratio >= _SAME_FACT_RATIO:
        best["level"] = "same"
    else:
        best["level"] = "similar"
    return best


def _merge_two_entries(local: str, cold: str) -> str:
    """合并本地新条目与冷层已有条目。冲突取最新(本地 newer)。"""
    base = local
    # P8 修复 (2026-09): 过滤 split 产生的空串成员 — 句末标点结尾时
    # re.split 会产出 "", 而 "" in s 恒 True 使冷层所有新句子被误判重复,
    # 合并结果丢失冷层独有细节 (实测 "A记录。"+"B记录。新增信息。" → "A记录。")。
    base_sentences = {s for s in re.split(r"[。！？;；\n]", base) if s.strip()}

    extra = []
    for s in re.split(r"[。！？;；\n]", cold):
        s = s.strip()
        if not s:
            continue
        is_new = True
        for bs in base_sentences:
            if (s in bs or bs in s or
                    difflib.SequenceMatcher(None, s, bs).ratio() > 0.8):
                is_new = False
                break
        if is_new:
            extra.append(s)

    if extra:
        base = base.rstrip("。！？;；\n") + "。" + "。".join(extra) + "。"
    return base


def _cold_write_with_dedup(client, entry: str) -> Tuple[bool, str]:
    """P8 (2026-09): 冷层写前查重 — remember/update 前 recall+匹配。

    语义与 _handle_cold_migration 查重一致 (同口径, 避免双源漂移):
      - 无匹配 → remember (importance=0.6); 返回 (是否 stored, 新 memory_id)
      - same → 冷层已有等价全文, 零调用不重复写; 返回 (True, 已有 id)
      - similar → merge-update; 返回 (是否 updated, 已有 id)
    冷层不可达/recall 异常 → 抛异常, 由调用方保守保留原样 (errors)。

    cold_id 一律指向冷层真实存在的 id — stub 路径 stamp 后
    restore_stubs_from_results 才能命中 (Phase 4 恢复链路契约)。
    """
    existing = _recall_safe(client, entry)
    matched = _find_best_match(entry, existing)
    if matched is None:
        r = client.remember(entry, importance=0.6, scope="global")
        if not isinstance(r, dict) or r.get("status") != "stored":
            return False, ""
        return True, r.get("memory_id") or ""
    if matched["level"] == "same":
        return True, matched.get("id") or ""
    merged = _merge_two_entries(entry, matched["content"])
    r = client.update(matched["id"], merged)
    if not isinstance(r, dict) or r.get("status") != "updated":
        return False, ""
    return True, matched.get("id") or ""


def _safe_remove_local(store, target: str, entry: str, stat: dict) -> None:
    """安全删本地条目 (失败仅累加 errors, 不抛异常)。"""
    try:
        result = store.remove_by_exact(target, entry)
        if not result.get("success"):
            stat["errors"] += 1
    except Exception:
        stat["errors"] += 1


def _rebuild_file(store, target: str, entries: List[str]) -> None:
    """合并后重建文件 (调用 store 的原子写)。"""
    path = store._path_for(target)
    store._write_entries(path, entries)


# =============================================================================
# Phase 4: 热层规则预算制 (LRU 缓存模型, 2026-08-26 设计定稿)
# 设计: /tmp/memorycore-lru-design.md + /tmp/memorycore-lru-signal-design.md
# 铁律: 无永久规则; 保护=乘数非豁免; 寿命由活性决定 (LRU 触达语义)
# =============================================================================

# ---- 系统噪声前缀黑名单 (fresh 查询清洗; 实测占日志 3.2%) -------------
_NOISE_PREFIXES = (
    "[IMPORTANT:", "[ASYNC DELEGATION", "[SUBAGENT", "[TOOL ",
    "[BACKGROUND", "[OUT-OF-BAND",
)


def _emb_cache_path(target: str) -> Path:
    """规则向量缓存文件 (MEMORY.emb.json / USER.emb.json, 与 .md 同目录)。"""
    base = USER_FILE if target == "user" else MEMORY_FILE
    return base.with_suffix(".emb.json")


def _emb_cache_load(target: str) -> Dict[str, List[float]]:
    """读向量缓存; 损坏/缺失 → {} (视为全缺失, 重嵌一次, 失败方向安全)。"""
    p = _emb_cache_path(target)
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _emb_cache_save(target: str, data: Dict[str, List[float]]) -> None:
    """原子写向量缓存 (tempfile + os.replace; 与 MetaStore 同模式)。"""
    p = _emb_cache_path(target)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".memcore-emb-",
                                   suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception:
        pass  # 缓存失败不影响机制 (下次重嵌)


def _rule_weight_eff(meta: Dict[str, Any],
                     now: Optional[datetime] = None) -> float:
    """惰性折现权重: w_eff = weight × 0.5 ^ ((now-last_active)/half_life)。

    排序时才计算, 不写 sidecar (与冷层 decay.py 同模式, 无后台任务)。
    缺失字段回退: weight=WEIGHT_INIT, last_active=written_at (零迁移成本)。
    """
    now = now or datetime.now(timezone.utc)
    weight = float(meta.get("weight") or WEIGHT_INIT)
    last = meta.get("last_active_at") or meta.get("written_at") or meta.get("updated_at")
    dt = _parse_iso(str(last)) if last else None
    if dt is None:
        return weight
    days = max((now - dt).days, 0)
    return weight * (0.5 ** (days / WEIGHT_HALF_LIFE_DAYS))


def _bump_weight(meta: Dict[str, Any], increment: float,
                 refresh_anchor: bool, now: Optional[datetime] = None) -> None:
    """命中加分 (幂等): 先折现再加 → weight = min(w_eff+inc, WEIGHT_MAX)。

    强命中 (refresh_anchor=True) 刷新 last_active_at; 弱命中只小幅托底,
    不刷新锚点 (弱命中不破坏区分度的关键设计)。
    """
    now = now or datetime.now(timezone.utc)
    eff = _rule_weight_eff(meta, now)
    meta["weight"] = min(eff + increment, WEIGHT_MAX)
    if refresh_anchor:
        meta["last_active_at"] = now.isoformat()


def _lex_evidence(a: str, b: str) -> bool:
    """词法弱命中判据: 归一化后共享 bigram ≥ LEX_EVIDENCE_BIGRAMS。

    用户 FP 例 (sb=2) 保住弱命中语义; sb≥1 太宽 (37.2%), ratio 系判据
    真实数据全灭 (实测 780 对通过 0 对, 仅作审计/降级用)。
    """
    na, nb = _norm_sentence(a), _norm_sentence(b)
    if len(na) < 2 or len(nb) < 2:
        return False
    sa = {na[i:i + 2] for i in range(len(na) - 1)}
    sb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    return len(sa & sb) >= LEX_EVIDENCE_BIGRAMS


def _load_fresh_queries(metastores: Dict[str, MetaStore]) -> List[str]:
    """fresh 查询清洗管线 (扫描端, 不动采集端)。Phase 4 命中扫描输入。

    1. 30 天窗口 (ts, query) 元组 (load_recent_queries_with_ts)
    2. fresh 过滤: ts > 各规则 last_scan_at 下界 (min, 保守不丢查询)
    3. 系统噪声前缀过滤 + 内容去重 (保留最新) + 截断 200 字
    4. 取**最近** FRESH_QUERY_SCAN_CAP 条 (增量语义; 追加式日志尾部=最新)
    """
    if not _cfg.ACTIVITY_LOG_ENABLED:
        return []
    try:
        rows = load_recent_queries_with_ts(days=ACTIVITY_WINDOW_DAYS)
    except Exception:
        return []
    if not rows:
        return []
    now = datetime.now(timezone.utc)
    # fresh 下界 = max(最晚扫描点, now-30d) — 增量语义:
    # 全部规则扫过后 (last_scan_at=now) → 下界=now → 下一轮只扫新查询;
    # 新规则 (written_at=now, 无 last_scan_at) → 出生前查询不扫 (设计 §6.1)。
    scan_lower = now - timedelta(days=ACTIVITY_WINDOW_DAYS)
    scan_upper: Optional[datetime] = None
    for ms in metastores.values():
        for m in _iter_meta_values(ms):
            ts = m.get("last_scan_at") or m.get("written_at")
            dt = _parse_iso(str(ts)) if ts else None
            if dt and (scan_upper is None or dt > scan_upper):
                scan_upper = dt
    if scan_upper is not None:
        scan_lower = max(scan_lower, scan_upper)
    # 倒序遍历 (最近优先), 增量过滤 + 噪声过滤 + 去重 (保留最新)
    fresh: List[str] = []
    seen = set()
    for ts, q in reversed(rows):
        if ts is not None and ts < scan_lower:
            continue
        q = (q or "").strip()
        if not q:
            continue
        if q.startswith(_NOISE_PREFIXES):
            continue
        h = hashlib.sha256(q.encode()).hexdigest()[:16]
        if h in seen:
            continue
        seen.add(h)
        fresh.append(q[:200])
        if len(fresh) >= FRESH_QUERY_SCAN_CAP:
            break
    fresh.reverse()  # 恢复时间升序 (嵌入/排序无影响, 语义一致)
    return fresh


def _iter_meta_values(ms: MetaStore):
    """枚举 MetaStore sidecar 的元数据值 (供 last_scan_at 下界扫描)。

    直接读 sidecar data values, 不依赖条目内容 (键是 sha256, 内容不可反推)。
    """
    try:
        return list(ms._load_unlocked().values())
    except Exception:
        return []


def _embed_batch_mnemosyne(client, texts: List[str]) -> Optional[List[List[float]]]:
    """方案 b: 经 Mnemosyne embed_texts 批量嵌入 (与 recall 同分数空间)。

    按 EMBED_BATCH_MAX 分片; 任一异常 → None (降级纯词法, 保守方向)。
    EMBED_BACKEND 接线 (M3, 2026-08-26):
      mnemosyne (默认) → client.embed_texts (方案 b, 分数空间一致)
      ollama      → 本地 _embed_batch (方案 c, 需重校准阈值)
      off         → None (强制降级纯词法)
    """
    if _cfg.EMBED_BACKEND == "off":
        return None
    if _cfg.EMBED_BACKEND == "ollama":
        # 本地 ollama 通道 (S3 聚簇嵌入复用): 可用 → list of vectors
        try:
            res = _embed_batch(texts)
            if isinstance(res, dict):
                emb = res.get("embeddings")
                if (isinstance(emb, list) and emb
                        and isinstance(emb[0], list)):
                    return emb
        except Exception:
            return None
        return None
    out: List[List[float]] = []
    try:
        for i in range(0, len(texts), EMBED_BATCH_MAX):
            chunk = texts[i:i + EMBED_BATCH_MAX]
            vecs = client.embed_texts(chunk)
            if not vecs or len(vecs) != len(chunk):
                return None
            out.extend(vecs)
        return out
    except Exception:
        return None


def _rule_vectors(metastores: Dict[str, MetaStore], rules: Dict[str, List[str]],
                  client) -> Dict[str, Dict[str, List[float]]]:
    """规则向量: sidecar 缓存命中 + 缺失批量补嵌 (惰性, 每轮 ≤1 批)。

    返回 {target: {entry: [f32...]}}。embedding 不可达 → 该 target 返回 {}。
    L5 (2026-08-26): save 前用当前 meta 键集合过滤孤儿键 —
    规则内容变更后旧 sha256 键永存会缓慢膨胀, 此处对齐 GC。
    """
    result: Dict[str, Dict[str, List[float]]] = {}
    for target, entries in rules.items():
        if not entries:
            result[target] = {}
            continue
        cache = _emb_cache_load(target)
        vecs: Dict[str, List[float]] = {}
        missing: List[str] = []
        for e in entries:
            h = hashlib.sha256((e or "").strip().encode()).hexdigest()
            if h in cache and isinstance(cache[h], list):
                vecs[e] = cache[h]
            else:
                missing.append(e)
        if missing:
            mvecs = _embed_batch_mnemosyne(client, missing)
            if mvecs:
                for e, v in zip(missing, mvecs):
                    vecs[e] = v
                    cache[hashlib.sha256((e or "").strip().encode()).hexdigest()] = v
                # 孤儿 GC: 只保留当前规则集的键 (内容变更/删除的旧键清除)
                live = {hashlib.sha256((e or "").strip().encode()).hexdigest()
                        for e in entries}
                stale = [k for k in cache if k not in live]
                for k in stale:
                    cache.pop(k, None)
                _emb_cache_save(target, cache)
        result[target] = vecs
    return result


def apply_activity_hits(metastores: Dict[str, MetaStore],
                        entries_by_target: Dict[str, List[str]],
                        client, stat: dict) -> None:
    """Phase 4 活性信号: 语义两级判定 (实测校准, 2026-08-26)。

    fresh 查询 → 批量嵌入 → 与规则向量全对余弦 (缓存后 ≈ 免费)
      cos_max ≥ HIT_STRONG_COS → 强命中 +1.0/轮 (封顶, 刷新锚点)
      embedding 不可达 → 降级纯词法弱命中 +0.3 (HIT_WEAK_MODE=degraded)
    词法证据 (sb≥2) 正常模式仅作审计字段 (hits_lex), 不加分。
    """
    if not _cfg.RULE_BUDGET_ENABLED:
        return
    rules: Dict[str, List[str]] = {}
    for t, entries in entries_by_target.items():
        rs = []
        for e in entries:
            m = metastores[t].get_entry(e)
            if m and m.get("type") == "rule":
                rs.append(e)
        if rs:
            rules[t] = rs
    if not rules:
        return
    queries = _load_fresh_queries(metastores)
    if not queries:
        return
    stat["scan_queries"] = len(queries)
    # 查询嵌入 (每轮唯一真实成本, 1 批)
    qvecs = _embed_batch_mnemosyne(client, queries)
    if qvecs is None:
        # 降级: 纯词法弱命中 (degraded 模式)
        if _cfg.HIT_WEAK_MODE == "degraded":
            _degraded_lexical_hits(metastores, rules, queries, stat)
        stat["embed_fail"] = stat.get("embed_fail", 0) + 1
        return
    rvecs = _rule_vectors(metastores, rules, client)
    now = datetime.now(timezone.utc)
    for target, rs in rules.items():
        for e in rs:
            meta = metastores[target].get_entry(e)
            if not meta:
                continue
            rv = rvecs.get(target, {}).get(e)
            if not rv:
                continue
            cos_max = max((_cosine(qv, rv) for qv in qvecs), default=0.0)
            if cos_max >= HIT_STRONG_COS:
                _bump_weight(meta, HIT_STRONG_INCREMENT, refresh_anchor=True, now=now)
                stat["hits_strong"] = stat.get("hits_strong", 0) + 1
            elif (_cfg.HIT_WEAK_MODE == "grey" and cos_max >= HIT_WEAK_COS
                    and any(_lex_evidence(q, e) for q in queries)):
                # grey 档 (用户可开): 灰区语义 + 词法佐证 → 弱命中 (不刷新锚点)
                _bump_weight(meta, HIT_WEAK_INCREMENT, refresh_anchor=False, now=now)
                stat["hits_weak"] = stat.get("hits_weak", 0) + 1
            else:
                if any(_lex_evidence(q, e) for q in queries):
                    stat["hits_lex"] = stat.get("hits_lex", 0) + 1
            meta["last_scan_at"] = now.isoformat()
            try:
                metastores[target].stamp(
                    e, meta.get("type", "rule"),
                    written_at=_parse_iso(meta.get("written_at")) if meta.get("written_at") else None,
                    updated_at=_parse_iso(meta.get("updated_at")) if meta.get("updated_at") else None,
                    origin=meta.get("origin", "hermes"),
                    importance=float(meta.get("importance") or 0.8),
                    weight=float(meta.get("weight") or WEIGHT_INIT),
                    last_active_at=_parse_iso(meta.get("last_active_at")) if meta.get("last_active_at") else None,
                    last_scan_at=now,
                    cold_id=meta.get("cold_id"),
                )
            except Exception:
                pass  # 盖章失败不影响机制 (下次 reconcile 补)


def _degraded_lexical_hits(metastores: Dict[str, MetaStore],
                           rules: Dict[str, List[str]],
                           queries: List[str], stat: dict) -> None:
    """降级模式弱命中: 任一 fresh 查询共享 bigram ≥2 → +0.3/轮 (封顶, 不刷新锚点)。"""
    now = datetime.now(timezone.utc)
    for target, rs in rules.items():
        for e in rs:
            meta = metastores[target].get_entry(e)
            if not meta:
                continue
            if any(_lex_evidence(q, e) for q in queries):
                _bump_weight(meta, HIT_WEAK_INCREMENT, refresh_anchor=False, now=now)
                stat["hits_weak"] = stat.get("hits_weak", 0) + 1
            meta["last_scan_at"] = now.isoformat()
            try:
                metastores[target].stamp(
                    e, meta.get("type", "rule"),
                    written_at=_parse_iso(meta.get("written_at")) if meta.get("written_at") else None,
                    updated_at=_parse_iso(meta.get("updated_at")) if meta.get("updated_at") else None,
                    weight=float(meta.get("weight") or WEIGHT_INIT),
                    last_active_at=_parse_iso(meta.get("last_active_at")) if meta.get("last_active_at") else None,
                    last_scan_at=now,
                    origin=meta.get("origin", "hermes"),
                    importance=float(meta.get("importance") or 0.8),
                    cold_id=meta.get("cold_id"))
            except Exception:
                pass


def _rule_rank(entry: str, meta: Dict[str, Any], now: datetime) -> float:
    """退役排序权重: w_rank = w_eff × mult (升序 = 先被挤)。

    mult: A类/红线/importance≥0.9 → ×WEIGHT_PROTECT_MULT (更难挤, 非豁免);
    kw 可沉型 (should_keep_local=False) → ×WEIGHT_KWSINK_MULT (优先挤)。
    """
    eff = _rule_weight_eff(meta, now)
    mult = WEIGHT_PROTECT_MULT if _is_protected_rule(entry, meta) else 1.0
    if not should_keep_local(entry):
        mult *= WEIGHT_KWSINK_MULT
    return eff * mult


def _select_retirement_candidates(store, metastore, target: str,
                                  need_chars: int, stat: dict) -> List[str]:
    """按 w_rank 升序选择退役候选 (驻留期是唯一硬门槛)。

    tie-break 三级: w_rank → last_active_at 早 → 字符长 → sha256 (幂等确定)。
    返回按挤出顺序排列的候选条目列表; 冷层失败由调用方 break。

    Phase 4 (2026-08-26 设计定稿, 方案 B): protected 规则 (A 类/红线/importance≥0.9)
    参与 LRU 挤权 — 铁律"热层无永久保留"; 保护只是权重乘数 (×WEIGHT_PROTECT_MULT
    更难挤, 非豁免), 衰减后同样可退役 (代码内无"长期失活降级"路径)。
    """
    now = datetime.now(timezone.utc)
    # 词法活跃保护: 近 7 天查询中 sb≥2 词法命中的规则不参与挤权
    # (纯词法零 LLM — 避免 _rule_topic_dormant 的 LLM 确认段: 无 API key 时
    #  全部判活跃 → 挤权失效; 有 key 时同步路径 LLM 瀑布时延失控)
    # 窗口用 7 天而非全量 30 天: 实测 sb≥2 在 30 查询窗口覆盖 24/26 规则,
    # 全量 350 条会几乎全活跃 → 挤权名存实亡。
    try:
        _active_queries = (load_recent_queries(days=7)
                           if _cfg.ACTIVITY_LOG_ENABLED else [])
    except Exception:
        _active_queries = []
    cands = []
    for e in store.entries(target):
        m = metastore.get_entry(e)
        if not m or m.get("type") != "rule":
            continue
        # 驻留期硬门槛 (新规则/恢复规则 7 天内不挤) — 最先过滤,
        # 避免刚盖章的填充/新条目先触发词法判定 (零成本原则)
        age = entry_age_days(m, now=now)
        if age is not None and age < RULE_MIN_RESIDENCY_DAYS:
            continue
        if _active_queries and any(_lex_evidence(q, e) for q in _active_queries):
            continue  # 词法活跃 (sb≥2) → 不挤 (宁可不挤, 不误伤在用规则)
        # 2026-08-26 设计定稿 (方案 B): protected 规则 (A 类/红线/importance≥0.9)
        # 也参与挤权 — 铁律"热层无永久保留"; 保护只是权重乘数 (×3.0 更难挤,
        # 衰减后同样退役)。不豁免。
        wrank = _rule_rank(e, m, now)
        last = m.get("last_active_at") or m.get("written_at") or ""
        cands.append((wrank, last, -len(e), hashlib.sha256(e.encode()).hexdigest(), e))
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    selected = []
    freed = 0
    for wrank, last, neglen, h, e in cands:
        if freed >= need_chars:
            break
        if len(selected) >= MAX_EVICT_PER_RUN:
            break
        selected.append(e)
        freed += len(e)
    return selected


def enforce_rule_budget(store, client, target: str, metastore,
                        stat: dict) -> None:
    """规则预算检查 + 挤权 (Phase 4 核心): rule+stub 字符超预算 → 退役最低权重规则。

    安全顺序: 冷层写成功才动本地 (stub-sink/retype 现有语义);
    冷层失败 → break (不丢数据, 下轮再试); 每轮 ≤ MAX_EVICT_PER_RUN。
    """
    if not _cfg.RULE_BUDGET_ENABLED:
        return
    # 无活性信号 (日志关闭) → 预算机制禁用 (LRU 依赖活性才有意义;
    # 退化为纯年龄排序又回到年龄不可靠的旧问题)。靠 5000 硬上限兜底。
    if not _cfg.ACTIVITY_LOG_ENABLED:
        return
    # 独立调用 (store_fact 写后) 传空 dict — 初始化被调函数依赖的计数键
    for _k in ("stubbed", "errors", "retyped", "overflowed", "kept",
               "aged_sunk"):
        stat.setdefault(_k, 0)
    entries = store.entries(target)
    rule_chars = 0
    for e in entries:
        m = metastore.get_entry(e)
        if m and m.get("type") in ("rule", "stub"):
            rule_chars += len(e)
    if rule_chars <= RULE_BUDGET_CHARS:
        return
    need = rule_chars - RULE_BUDGET_CHARS
    anchor_parts: List[str] = []
    for e in _select_retirement_candidates(store, metastore, target, need, stat):
        if rule_chars <= RULE_BUDGET_CHARS:
            break
        before_n = (stat.get("stubbed", 0) + stat.get("retyped", 0)
                    + stat.get("overflowed", 0))
        m = metastore.get_entry(e)
        if m and _rule_retype_eligible(e):
            # 完成态记录 → 全文冷迁移, 不留指针 (历史记录无需召回钩子)
            try:
                _handle_rule_retype(store, client, target, e, m,
                                    metastore, stat, anchor_parts)
            except Exception:
                stat["errors"] = stat.get("errors", 0) + 1
            released = len(e)
        else:
            try:
                _handle_rule_stub_sink(store, client, target, e, metastore,
                                       stat, anchor_parts)
            except Exception:
                stat["errors"] = stat.get("errors", 0) + 1
            # L1: stub 后本地仍占 ~40 字指针 — 实际释放 = len(e) - len(stub)
            released = len(e) - len(_make_stub(e))
        after_n = (stat.get("stubbed", 0) + stat.get("retyped", 0)
                   + stat.get("overflowed", 0))
        if after_n == before_n:
            break  # 冷层失败/未动 → 停止 (不丢数据)
        rule_chars -= max(released, 0)
        stat["lru_evicted"] = stat.get("lru_evicted", 0) + 1
    # L7 (2026-08-26): flush 挤掉的 user 偏好进 [用户偏好摘要] 锚点 —
    # 否则 store_fact/direct_write 路径挤掉的偏好不进锚点 (仅 run_overflow 末尾 flush)
    if target == "user" and anchor_parts:
        try:
            _update_pref_anchor(client, store, anchor_parts, stat)
        except Exception:
            pass


def restore_stubs_from_results(store, metastores: Dict[str, MetaStore],
                               results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """召回恢复 (Phase 4): recall 结果命中 stub 的 cold_id → 替换指针为全文。

    恢复全文是新 sidecar 键 (sha256 不同) → written_at=now → 天然 7 天驻留
    + 高权重 (WEIGHT_INIT + HIT_STRONG_INCREMENT), 不会"恢复即被再挤"。
    返回未恢复的结果列表 (已恢复的从注入中剔除, 防重复注入)。
    """
    if not results:
        return results
    # 建 cold_id → (target, stub) 映射
    id2stub = {}
    for target, ms in metastores.items():
        try:
            for e in store.entries(target):
                m = ms.get_entry(e)
                if m and m.get("type") == "stub" and m.get("cold_id"):
                    id2stub[m["cold_id"]] = (target, e)
        except Exception:
            continue
    if not id2stub:
        return results
    now = datetime.now(timezone.utc)
    restored_ids = set()
    for r in results:
        rid = r.get("id")
        if not rid or rid not in id2stub:
            continue
        target, stub = id2stub[rid]
        content = r.get("content") or ""
        if not content:
            continue
        try:
            if store.replace(target, stub, content).get("success"):
                # L3 (2026-08-26): 从 stub meta 透传原 importance —
                # 否则 importance≥0.9 的受保护规则被挤后恢复, 保护线丢失
                # (文本标记保护仍在, 但 importance 保护线归零)。
                stub_meta = metastores[target].get_entry(stub) or {}
                orig_imp = float(stub_meta.get("importance") or 0.8)
                metastores[target].stamp(
                    content, "rule", origin="stub_restore",
                    weight=WEIGHT_INIT + HIT_STRONG_INCREMENT,
                    last_active_at=now,
                    importance=orig_imp,
                )
                restored_ids.add(rid)
        except Exception:
            continue
    if restored_ids:
        return [r for r in results if r.get("id") not in restored_ids]
    return results

# ---- E8: env switches lazy delegation (2026-09-12) --------------------------
def __getattr__(name):
    if name in ("ACTIVITY_LOG_ENABLED", "RULE_BUDGET_ENABLED",
                "HIT_WEAK_MODE", "EMBED_BACKEND"):
        return getattr(_cfg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
