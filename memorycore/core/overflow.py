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

from .classifier import (classify, classify_user_pref, should_keep_local,
                         should_keep_local_rule_view, judge_engine_enabled,
                         HOT, COLD, STALE)
from . import llm_config  # noqa: E402  LLM 唯一真相源 (惰性解析+观测三态+安全阀)
from . import llm_rot  # noqa: E402  LLM 候选轮转 (终审低危 D2 长尾饿死修复)
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
    RULE_MIN_RESIDENCY_IDLE_DAYS, RULE_MIN_RESIDENCY_WARM_DAYS,
    RULE_MIN_RESIDENCY_ACTIVE_DAYS,
    WEIGHT_INIT, WEIGHT_HIT_INCREMENT, WEIGHT_MAX, WEIGHT_HALF_LIFE_DAYS,
    WEIGHT_PROTECT_MULT, WEIGHT_KWSINK_MULT, MAX_EVICT_PER_RUN,
    GRACE_MULT, TS_ANOMALY_TOLERANCE_SECONDS,
    HIT_STRONG_COS, HIT_WEAK_COS, HIT_STRONG_INCREMENT, HIT_WEAK_INCREMENT,
    HIT_CAP_PER_SCAN, LEX_EVIDENCE_BIGRAMS,
    FRESH_QUERY_SCAN_CAP, EMBED_BATCH_MAX, EMBED_TIMEOUT,
    EMBED_MODEL, MEMORY_FILE, USER_FILE,
    JUDGE_AMBIGUOUS_LRU_DAYS, JUDGE_RESOLVED_RULE_GRACE_DAYS,
    JUDGE_AMBIGUOUS_MAX_REVIEWS,
)
# E8 (2026-09-12): ACTIVITY_LOG_ENABLED / RULE_BUDGET_ENABLED / HIT_WEAK_MODE /
# EMBED_BACKEND 由模块尾部 __getattr__ 惰性委托 core.config (运行中改 env
# 即时生效, 修 import 时冻结; monkeypatch.setattr 覆盖仍有效)。
from . import config as _cfg
log = logging.getLogger("memorycore.overflow")


def _cache_policy_v2() -> bool:
    """CACHE-POLICY-V2 (2026-09-13) 总开关: False = 回滚本轮前换出资格语义。

    默认 True: protected 只乘 3、无年龄门、ambiguous 不豁免、rule/stub/state 同池。
    置 0 仅用于对照测试; 冷写成功才删本地的铁律在所有路径不变。
    """
    return bool(getattr(_cfg, "CACHE_POLICY_V2", True))



def _effective_rule_budget_chars() -> int:
    """运行时规则预算: 兼容 monkeypatch overflow.RULE_BUDGET_CHARS 或 config 常量。

    生产两者同值; 验收测试可能只改其一, 取较小值以保证预算压力不被抵消。
    """
    cfg_val = int(getattr(_cfg, "RULE_BUDGET_CHARS", RULE_BUDGET_CHARS))
    return min(int(RULE_BUDGET_CHARS), cfg_val)

def _make_handle(entry: str) -> str:
    """生成目录句柄 (≤20 字符): # + sha256 前 8 位。

    句柄只做本地页表索引, 不具语义; 插件目录用 [handle] 主题 展示。
    """
    return "#" + hashlib.sha256((entry or "").encode("utf-8")).hexdigest()[:8]


def _stub_topic(stub: str) -> str:
    """从 stub 指针提取主题词 (≤10 字), 供目录/句柄直查。"""
    body = (stub or "")
    if body.startswith(STUB_PREFIX):
        body = body[len(STUB_PREFIX):]
    body = body.split("→")[0]
    return re.sub(r"\s+", "", body)[:10]


from .metadata import (MetaStore, entry_age_days, parse_embedded_date,
                       _parse_iso, _ts_anchor, _ts_anchor_status,
                       load_recent_queries, load_recent_queries_with_ts)

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
# 设计: 源树 rule-stale-design (见 DESIGN-DEVIATIONS.md)
# 约束 1 修正: B 类按失效证据分层放弃; A 类/红线类/importance≥0.9 绝不误伤。

# CACHE-POLICY-V2 (2026-09-13): protected 仅作为 WEIGHT_PROTECT_MULT=3.0
# 排序乘数来源, 不再是资格豁免; 词表沿用 Q4 v2 收紧后的判据。
#   - 删除泛词 准确/严谨/验证/覆盖 (历史记录高频词, 曾把 #25 "覆盖交互需求"
#     误标 protected)
#   - 新增 A 类短语 澄清用户/问题清单/用户多次强调 + 用户(日期)?明确
#   - 新增用户偏好前缀 (head25) 与 protect_override 人工加保
_RULE_META_MARKERS = [
    "行为准则", "交互习惯", "写作风格", "回答风格", "措辞", "汇报", "沟通",
    "大白话", "结论先行", "抑郁", "信任", "尊重", "最高准则", "澄清用户",
    "问题清单", "用户多次强调",
]
# S6 红线类 (硬性词汇, 不论 A/B 类) — 排序时 ×3 (非豁免)
_RULE_REDLINE_MARKERS = ["红线", "零容忍", "绝不", "禁止", "纠正"]
# 用户偏好前缀 (head25, Q4 判据 5)
_RULE_USER_PREF_PREFIXES = ["用户喜欢", "用户偏好", "用户希望", "用户要求",
                            "用户不喜欢", "用户习惯", "用户纠正", "用户明确"]
_RULE_USER_DUI_ASPECTS = ["期望", "态度", "兴趣", "偏好", "要求"]
# 用户(20xx-xx-xx)?明确 — A 类短语 (Q4 判据 4, 日期可选)
_RULE_USER_EXPLICIT_RE = re.compile(r"用户(?:20\d\d-\d\d-\d\d)?明确")

# S3 嵌入通道 (ollama qwen3, prefetch 同款; 不可用 → 纯词法降级)
_EMBED_URL = os.environ.get("MEMORYCORE_EMBED_URL", "http://localhost:11434")
_EMBED_MODEL = os.environ.get("MEMORYCORE_EMBED_MODEL", "qwen3-embedding:0.6b")

# S4: 单轮休眠判定评估的候选上限 (LLM 调用量护栏)
_STUB_EVAL_CAP = 10

# E5/FIX (R5): 统一嵌入入口的失败原因通道。_embed_texts 失败时写入,
# merge/activity 调用方经 _record_embed_fail 转存 stat["embed_fail_reason"]。
_LAST_EMBED_FAIL_REASON: Optional[str] = None


def _embed_backend() -> str:
    """运行时读取 EMBED_BACKEND (E8 惰性解析 + monkeypatch 契约)。

    优先模块字典的显式覆盖 (monkeypatch.setattr(overflow, "EMBED_BACKEND", v));
    未覆盖时惰性委托 core.config, 保证运行中改 env 即时生效。
    """
    value = globals().get("EMBED_BACKEND")
    if value is None:
        value = getattr(_cfg, "EMBED_BACKEND", "mnemosyne")
    return str(value or "mnemosyne")


def _record_embed_fail(stat: Optional[dict] = None,
                       reason: Optional[str] = None) -> None:
    """merge/activity 同口径: 计数 + 首次原因 + 首次 warning(不刷屏)。"""
    if stat is None:
        return
    stat["embed_fail"] = int(stat.get("embed_fail", 0)) + 1
    if not stat.get("embed_fail_reason"):
        stat["embed_fail_reason"] = str(
            reason or _LAST_EMBED_FAIL_REASON
            or f"{_embed_backend()}: unavailable")
        log.warning("EMBED: 嵌入失败 (%s) → 降级纯词法",
                    stat["embed_fail_reason"])



def _is_protected_rule(entry: str, meta: dict) -> bool:
    """protected 判定 — 仅提供排序乘数来源 (CACHE-POLICY-V2)。

    全满足任一即 protected (rank 乘 ×WEIGHT_PROTECT_MULT=3.0, 更难挤但
    在足够预算压力下必然可换出; 不再是资格豁免):
      1. importance >= IMPORTANCE_PROTECT (用户显式高价值)
      2. meta.protect_override is True (人工显式加保)
      3. 红线硬词: 红线/零容忍/绝不/禁止/纠正
      4. A 类元行为准则短语 (_RULE_META_MARKERS) 或 用户(20xx-xx-xx)?明确
      5. 用户偏好前缀 (head25): 用户喜欢/偏好/希望/要求/不喜欢/习惯/纠正/明确;
         或 用户对 + 期望/态度/兴趣/偏好/要求
    已删除泛词 准确/严谨/验证/覆盖 (历史记录高频词, 误标 protected 锁死出口)。
    """
    if meta.get("protected") is True:
        # F2-④ (2026-09-13): stub 指针继承的 protected 标记 —
        # 原文的文本标记在指针中不可见, 继承布尔位保证换出后仍 ×3。
        return True
    if meta.get("importance", 0.8) >= IMPORTANCE_PROTECT:
        return True
    if meta.get("protect_override") is True:
        return True
    if any(kw in entry for kw in _RULE_REDLINE_MARKERS):
        return True
    if any(kw in entry for kw in _RULE_META_MARKERS):
        return True
    if _RULE_USER_EXPLICIT_RE.search(entry):
        return True
    head25 = (entry or "")[:25]
    if any(p in head25 for p in _RULE_USER_PREF_PREFIXES):
        return True
    if "用户对" in head25 and any(a in head25 for a in _RULE_USER_DUI_ASPECTS):
        return True
    return False


def _rule_retype_eligible(entry: str) -> bool:
    """S2: rule 完成态复核资格 — 内嵌日期 ≥60d + ≥2 完成态词 + 零行为指令词。"""
    d = parse_embedded_date(entry)
    if d is None:
        return False
    # P3 (FIX8): 内嵌日期也过统一锚点 (field=embedded_date); 未来日期夹 now
    # 且 ts_anomaly 可见, 不制造“未来写死→永不 qualify”的隐形路径.
    d = _ts_anchor(d, datetime.now(timezone.utc),
                   field="embedded_date",
                   sha=hashlib.sha256((entry or "").encode()).hexdigest())
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


def _stub_fingerprint(entry: str, salt: str = "") -> str:
    """stub 内容指纹: sha256(全文 + optional salt), 返回 64 hex。

    FIX4 P2: `_make_stub` 只截取前 16 hex (64 bit) 做可见指纹, 另在
    写回路径保留碰撞检测兜底 (salted 重新派生)。16 hex 的碰撞概率在
    任何实际规则集上可忽略, 且真实碰撞时仍不会把两条规则并成同一指针。
    """
    raw = (entry or "").strip()
    payload = raw + ("\x00" + salt if salt else "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _make_stub(entry: str, *, salt: str = "") -> str:
    """S4: stub 指针 (≤STUB_MAX_CHARS, 词法生成零 LLM 依赖)。

    格式: [规则指针]{主题10字}→recall:{sha256前16hex} —
    指针+内容指纹召回钩子 (FIX4 P2 唯一化):
      - 指纹来自全文 sha256 (可选 salt), 同前缀规则也生成不同 stub;
      - 前 10 字相同、仅后缀不同的两条规则不会碰撞 (旧 4-hex 已废弃);
      - `_stub_topic` 依旧提取 `→` 前完整主题词 (≤10 字), 目录/H 通道
        口径不变; recall: 后为不可逆内容指纹, 不暴露全文;
      - 长度 ≤STUB_MAX_CHARS: 6(prefix)+10(topic)+1(→)+7(recall:)+16 = 40。
    """
    raw = (entry or "").strip()
    kw = re.sub(r"\s+", "", raw.split("。")[0][:10])
    if not kw:
        kw = re.sub(r"\s+", "", raw[:10])
    fp = _stub_fingerprint(raw, salt)[:16]
    stub = f'{STUB_PREFIX}{kw}→recall:{fp}'
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
    now = datetime.now(timezone.utc)
    cands = []
    ambiguous_cands = set()
    for e in entries:
        meta = metastore.get_entry(e)
        if not meta or meta.get("type") != "rule":
            continue
        if _is_protected_rule(e, meta):
            continue
        # B-2 A1-stub: ambiguous 达 21d/2 次审 → 候选 (无需休眠 LLM, 只做 stub)
        if meta.get("judge_decision") == "ambiguous":
            if _ambiguous_a1_eligible(meta, now, entry=e):
                age = entry_age_days(meta, now=now, entry=e) or 0
                cands.append((age, len(e), e))
                ambiguous_cands.add(e)
            continue
        age = entry_age_days(meta, now=now, entry=e)
        if age is None or age < RULE_STUB_IDLE_DAYS:
            continue
        cands.append((age, len(e), e))
    cands.sort(key=lambda t: (-t[0], -t[1]))
    picked: List[str] = []
    for _, _, e in cands[:_STUB_EVAL_CAP]:
        if len(picked) >= MAX_STUB_PER_RUN:
            break
        if e in ambiguous_cands:
            picked.append(e)
            continue
        if _rule_topic_dormant(e, queries):
            picked.append(e)
    return picked


def _meta_to_stamp_kwargs(meta: Dict[str, Any], entry: str,
                          now: Optional[datetime] = None,
                          *, protected: Optional[bool] = None,
                          origin: Optional[str] = None) -> Dict[str, Any]:
    """把既有 meta 转成 stamp 显式 kwargs (R1 原文改写/压缩路径继承用).

    仅用于"同一条目换内容" (旧 key 会因 sha 变化成孤儿), 所以必须显式
    继承而不是依赖新 key 的默认值。所有时间字段过 _ts_anchor 统一入口。
    """
    now = now or datetime.now(timezone.utc)
    sha = _entry_sha(entry)

    def _t(key: str):
        val = meta.get(key)
        if not val:
            return None
        return _ts_anchor(val, now, field=key, sha=sha)

    kw: Dict[str, Any] = {
        "origin": origin if origin is not None else meta.get("origin", "hermes"),
        "written_at": _t("written_at"),
        "updated_at": _t("updated_at"),
        "importance": (float(meta.get("importance"))
                       if meta.get("importance") is not None else None),
        "weight": (float(meta.get("weight"))
                   if meta.get("weight") is not None else None),
        "last_active_at": _t("last_active_at"),
        "last_scan_at": _t("last_scan_at"),
        "cold_id": meta.get("cold_id"),
        "type_override": meta.get("type_override"),
        "type_source": meta.get("type_source"),
        "protect_override": meta.get("protect_override"),
        "last_strong_hit_at": _t("last_strong_hit_at"),
        "last_weak_hit_at": _t("last_weak_hit_at"),
        "last_recall_hit_at": _t("last_recall_hit_at"),
        "last_injected_at": _t("last_injected_at"),
        "last_evicted_at": _t("last_evicted_at"),
        "writeback_count": (int(meta["writeback_count"])
                            if meta.get("writeback_count") is not None else None),
        "retire_count": (int(meta["retire_count"])
                         if meta.get("retire_count") is not None else None),
        "handle": meta.get("handle"),
        "reconcile_anchor_fallback": meta.get("reconcile_anchor_fallback"),
        "schema": (int(meta["schema"]) if meta.get("schema") is not None else None),
    }
    if protected is not None:
        kw["protected"] = bool(protected)
    elif meta.get("protected") is not None:
        kw["protected"] = bool(meta.get("protected"))
    # judge_* 审计字段原样继承 (compression 是同一判型结果改内容, 不是重判)
    for _k in ("judge_decision", "judge_band", "judge_confidence",
               "judge_signals", "judge_reason", "judge_policy"):
        if meta.get(_k) is not None:
            kw[_k] = meta.get(_k)
    if meta.get("judge_review_at"):
        kw["judge_review_at"] = _ts_anchor(
            meta.get("judge_review_at"), now, field="judge_review_at",
            sha=sha, allow_future=True)
    if meta.get("judge_review_count") is not None:
        kw["judge_review_count"] = int(meta.get("judge_review_count") or 0)
    if meta.get("judge_resolved_at"):
        kw["judge_resolved_at"] = _t("judge_resolved_at")
    if meta.get("judge_resolution") is not None:
        kw["judge_resolution"] = meta.get("judge_resolution")
    if meta.get("judge_reviewed_at"):
        kw["judge_reviewed_at"] = _t("judge_reviewed_at")
    return kw


def _handle_rule_retype(store, client, target: str, entry: str, meta: dict,
                        metastore, stat: dict, anchor_parts: List[str]) -> None:
    """S2 (L1): 完成态复核 — 重盖 state (origin=retype_overflow) → TTL 下沉。

    安全顺序: 盖章 → 冷迁移 (冷层确认成功才删本地)。
    冷层失败 → 恢复原 rule 章 (原时间戳/importance/origin), 下次再试。
    """
    d = parse_embedded_date(entry)
    if d is not None:
        # P3 (FIX8): 内嵌日期写入前统一过 _ts_anchor(field="embedded_date");
        # 未来日期夹 now 且 ts_anomaly 可见, 不再把 2099 写进 written_at.
        d = _ts_anchor(d, field="embedded_date", sha=_entry_sha(entry),
                       sink=stat)
    # FIX7 I7: 改型到 state 时必须清掉 ambiguous 判型键, 否则内容未变
    # (sha 相同) 的情况下 stale judge_decision 会粘住, 后续仍可能被当 A1
    # 快通候选。仅清"改型已判定消解"的字段; judge_band/reason 等审计保留。
    _clear_judge = meta.get("judge_decision") == "ambiguous"
    _state_kw: Dict[str, Any] = {
        "written_at": d, "origin": "retype_overflow"}
    if _clear_judge:
        _state_kw.update({
            "judge_decision": None,
            "judge_resolution": None,
            "judge_review_at": None,
            "judge_reviewed_at": None,
            "judge_review_count": None,
        })
    try:
        metastore.stamp(entry, "state", **_state_kw)
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
        _sha = _entry_sha(entry)
        _restore_kw: Dict[str, Any] = {
            "written_at": _ts_anchor(meta.get("written_at"),
                                     field="written_at", sha=_sha, sink=stat),
            "updated_at": _ts_anchor(meta.get("updated_at"),
                                     field="updated_at", sha=_sha, sink=stat),
            "importance": meta.get("importance", 0.8),
            "origin": meta.get("origin", "legacy"),
            "weight": float(meta.get("weight") or WEIGHT_INIT),
            "last_active_at": _ts_anchor(meta.get("last_active_at"),
                                         field="last_active_at", sha=_sha,
                                         sink=stat),
            # FIX7 I4/R2: 同物理条目回退, reconcile 兜底标记必须原样继承;
            # 否则 future/缺失出生戳的旧条目回退时会洗掉不可信标记。
            "reconcile_anchor_fallback": meta.get(
                "reconcile_anchor_fallback"),
        }
        if _clear_judge:
            # 冷迁移失败: 原 rule + 原 ambiguous 判型必须一起恢复, 不能
            # 因为已经写过 state 章就丢失 A0 安全出口。
            _restore_kw["judge_decision"] = meta.get("judge_decision")
            _restore_kw["judge_resolution"] = meta.get("judge_resolution")
            _restore_kw["judge_review_count"] = meta.get(
                "judge_review_count")
            _jra = meta.get("judge_review_at")
            _restore_kw["judge_review_at"] = (
                _ts_anchor(_jra, field="judge_review_at", sha=_sha,
                           sink=stat, allow_future=True) if _jra else None)
            _jrvd = meta.get("judge_reviewed_at")
            _restore_kw["judge_reviewed_at"] = (
                _ts_anchor(_jrvd, field="judge_reviewed_at", sha=_sha,
                           sink=stat) if _jrvd else None)
        metastore.stamp(entry, "rule", **_restore_kw)
    except Exception:
        pass


def _handle_rule_stub_sink(store, client, target: str, entry: str,
                           metastore, stat: dict,
                           anchor_parts: List[str],
                           *, force_cold_only: bool = False) -> None:
    """S4 (L2): 全文先写冷层确认 → 本地替换为 stub 指针/T3 冷层-only。

    安全顺序: 冷层保有全文 (P8: remember 前查重, same 不重复写/
    similar merge-update) → replace 本地; 任一失败 → 原条目原样 (errors+1)。
    Phase 4: 捕获冷层 memory_id → stub meta 写 cold_id (召回恢复链路用, 见
    restore_stubs_from_results)。

    FIX4 P1/P2 增量:
      - stub 唯一化: `_make_stub` 用全文 sha256 前 16 hex, 且写回前做
        同前缀碰撞检测; 真实碰撞时 salted 重新派生, 仍碰撞则退回
        cold-only, 绝不把两条规则合并成同一指针。
      - 极短条目 replace 因 5000 硬顶失败时: 冷写成功后直接 T3 cold-only
        删除本地, 不再每轮 "冷写成功→replace 失败→errors" 卡死。
      - stub 预算显式为 0 (`cfg.RULE_BUDGET_CHARS <= 0`, 由
        `--budget 0` 同时置零) 或 replace 因硬顶失败时: 冷写已成功,
        走 cold-only fallback; 不每轮 errors 卡死, 也不阻塞后续候选。
    """
    if entry not in store.entries(target):
        return  # 已被本轮其他动作处理 (如 S2/S5), 保守跳过
    # true-zero stub budget: 本地不保留任何指针 (T3 cold-only 语义)。
    try:
        _configured_stub_budget = int(
            getattr(_cfg, "RULE_BUDGET_CHARS", RULE_BUDGET_CHARS))
    except (TypeError, ValueError):
        _configured_stub_budget = RULE_BUDGET_CHARS
    if _configured_stub_budget <= 0:
        force_cold_only = True

    # P8 (2026-09): remember 前查重 — 冷层已有等价全文 (same) 不重复写,
    # similar merge-update; cold_id 一律指向冷层真实存在的 id。
    try:
        cold_ok, cold_id = _cold_write_with_dedup(client, entry)
    except Exception:
        stat["errors"] = stat.get("errors", 0) + 1
        stat["cold_errors"] = stat.get("cold_errors", 0) + 1
        return  # 冷层不可达/查重异常 → 保留原样, 不 stub (保守语义)
    if not cold_ok:
        stat["errors"] = stat.get("errors", 0) + 1
        stat["cold_errors"] = stat.get("cold_errors", 0) + 1
        return

    def _cold_only_remove(reason: str) -> bool:
        """冷写已确认后的安全 T3 删本地; 失败时 errors 已由 _safe_remove_local 计。"""
        _safe_remove_local(store, target, entry, stat)
        if entry not in store.entries(target):
            stat["cold_only"] = stat.get("cold_only", 0) + 1
            stat[reason] = stat.get(reason, 0) + 1
            if target == "user":
                anchor_parts.append(entry)
            return True
        return False

    stub = _make_stub(entry)
    # 显式 0 指针预算: 冷写成功后直接 cold-only (T3, 不制造本地指针)。
    if force_cold_only:
        _cold_only_remove("forced_cold_only")
        return

    # FIX4 P2 碰撞检测兜底: 同 stub 文本已存在且 cold_id 不同 → salted
    # 重新派生; 连续尝试仍冲突则 cold-only, 不覆盖既有映射。
    entries_now = store.entries(target)
    existing_meta = metastore.get_entry(stub) or {}
    if stub in entries_now and existing_meta.get("cold_id") != cold_id:
        resolved = False
        for i in range(8):
            salt = f"{cold_id}:{i}" if i else (cold_id or "collision")
            cand = _make_stub(entry, salt=str(salt))
            if cand not in entries_now:
                stub = cand
                stat["stub_collision_fallback"] =                     stat.get("stub_collision_fallback", 0) + 1
                resolved = True
                break
        if not resolved:
            _cold_only_remove("stub_collision_cold_only")
            return

    if store.replace(target, entry, stub).get("success"):
        try:
            # L3: stub meta 记录原 importance — 恢复时透传保护线
            orig_meta = metastore.get_entry(entry) or {}
            orig_imp = float(orig_meta.get("importance") or 0.8)
            # F2-④ (2026-09-13): 原文的文本 protected 标记 (红线/行为准则/
            # 用户明确/... ) 在 ≤40 字指针里不可见; 显式继承布尔位, 否则
            # 换出后 stub 的 _rule_rank 丢失 ×3, 可能被下一轮优先 GC。
            orig_protected = bool(_is_protected_rule(entry, orig_meta))
            _stub_kw: Dict[str, Any] = {}
            if orig_meta.get("judge_decision"):
                # C-2: ambiguous 兜底 stub 保留 judge 字段; 召回恢复后仍可审计
                _stub_kw = {
                    "judge_decision": orig_meta.get("judge_decision"),
                    "judge_band": orig_meta.get("judge_band"),
                    "judge_confidence": orig_meta.get("judge_confidence"),
                    "judge_signals": orig_meta.get("judge_signals"),
                    "judge_reason": orig_meta.get("judge_reason"),
                    "judge_review_count": int(
                        orig_meta.get("judge_review_count") or 0),
                    "judge_policy": orig_meta.get("judge_policy", "v3"),
                }
                if orig_meta.get("judge_decision") == "ambiguous":
                    _stub_kw["judge_resolution"] = "stubbed_ambiguous"
            # CACHE-POLICY-V2 Q4c: stub 必须承载原条目的活性/判型字段,
            # 否则 stamp 默认会把指针刷成"刚活跃"的 WEIGHT_INIT, 指针比全文更难挤。
            _sha = _entry_sha(entry)

            def _dt(key: str):
                # FIX6 R2 + FIX7 I2: 原文 -> 指针换形读旧时间戳也必须走
                # 唯一锚点入口; 未来 > 容差 → 夹 now + ts_anomaly。
                val = orig_meta.get(key)
                if not val:
                    return None
                return _ts_anchor(val, field=key, sha=_sha, sink=stat)
            _orig_written = _dt("written_at")
            _orig_updated = _dt("updated_at")
            metastore.stamp(
                stub, "stub", origin="stub_sink",
                written_at=_orig_written,
                updated_at=_orig_updated,
                cold_id=cold_id or None, importance=orig_imp,
                weight=float(orig_meta.get("weight") or WEIGHT_INIT),
                last_active_at=_dt("last_active_at"),
                last_strong_hit_at=_dt("last_strong_hit_at"),
                last_weak_hit_at=_dt("last_weak_hit_at"),
                last_recall_hit_at=_dt("last_recall_hit_at"),
                last_injected_at=_dt("last_injected_at"),
                last_evicted_at=datetime.now(timezone.utc),
                writeback_count=int(orig_meta.get("writeback_count") or 0),
                retire_count=int(orig_meta.get("retire_count") or 0) + 1,
                type_override=orig_meta.get("type_override"),
                type_source=orig_meta.get("type_source", "stub_sink"),
                protect_override=orig_meta.get("protect_override"),
                protected=orig_protected,
                handle=_make_handle(cold_id or stub),
                # FIX6 R1/R3: 原文 -> 指针是同一物理条目的换形, 继承宽限
                # 让位计数与 reconcile 兜底标记 (R3 计数不能在换形时洗白).
                reconcile_anchor_fallback=orig_meta.get(
                    "reconcile_anchor_fallback"),
                **_stub_kw)
        except Exception:
            pass  # 盖章失败下次 reconcile 补 (STUB_PREFIX 识别)
        stat["stubbed"] = stat.get("stubbed", 0) + 1
        if target == "user":
            anchor_parts.append(entry)  # 偏好全文进锚点 (与下沉同语义)
    else:
        # FIX4 P1: 冷写已成功, replace 失败通常是本地 5000 硬顶/容量;
        # 走 cold-only 释放本地, 不让失败候选每轮阻塞整批。
        _cold_only_remove("stub_replace_cold_only")


def _stub_gc(store, metastore, target: str, stat: dict,
             force: bool = False) -> None:
    """§6: stub 回收 — 最老优先删本地指针, ≤MAX_STUB_PER_RUN/轮。

    只删指针: 冷层零调用 (全文不受影响, forget 零调用)。
    年龄 < STUB_GC_MIN_AGE_DAYS 的指针不回收 (防刚建即被 GC 抖振)。
    v2 (2026-09-12): 触发条件从 "usage>=HARD" 放宽为 "usage>=HARD 或
    (rule_chars>RULE_BUDGET_CHARS 且无未保护候选)" — force=True 时
    不看 80% 水位 (冷层已有全文, 删指针无数据风险; DESIGN §3)。

    FIX8 口径: 指针是页表不是缓存内容; stub GC 与全文候选共用同一个
    `_rule_rank` (含 protected/新鲜窗口乘数), 另用 `last_evicted_at` 年龄门槛
    防止本轮新建指针被立即 GC。没有单独的 stub 宽限档位。
    """
    entries = store.entries(target)
    now = datetime.now(timezone.utc)
    stubs = []
    for e in entries:
        m = metastore.get_entry(e)
        if m and m.get("type") == "stub":
            # F2-② (2026-09-13): 本轮新建 stub 不参选。指针的 updated_at
            # 继承原文 (可能很旧), 因此必须优先用 last_evicted_at 计算真实
            # 指针年龄; 缺字段时按 retire_count/sidecar 兼容。
            last_evicted = (_ts_anchor(
                m.get("last_evicted_at"), now, field="last_evicted_at",
                sha=_entry_sha(e)) if m.get("last_evicted_at") else None)
            if last_evicted is not None:
                age = max((now - last_evicted).days, 0)
            elif (int(m.get("retire_count") or 0) == 1
                  and m.get("origin") == "stub_sink"):
                age = 0  # 本轮盖章失败兜底: 仍按新指针保护一个 GC 周期
            else:
                age = entry_age_days(m, now=now, entry=e)
            # CACHE-POLICY-V2: 指针 GC 与 rule 换出同一 _rule_rank 排序, 不再单看 age。
            rank = _rule_rank(e, m, now)
            stubs.append((rank, age if age is not None else 9999, e))
    stubs.sort(key=lambda t: (t[0], -t[1], -len(t[2]),
                              hashlib.sha256(t[2].encode()).hexdigest()))
    removed = 0
    for _rank, age, e in stubs:
        if removed >= MAX_STUB_PER_RUN:
            break
        if age < STUB_GC_MIN_AGE_DAYS:
            continue
        if not force and store.usage_pct(target) < HARD_THRESHOLD * 100:
            break
        before = len(store.entries(target))
        _safe_remove_local(store, target, e, stat)
        if len(store.entries(target)) < before:
            stat["stub_gc"] += 1
            removed += 1


# S3 嵌入通道 (可选增强; 不可用 → 纯词法降级, 溢流永不阻塞)
def _embed_batch(texts: List[str]) -> Optional[Dict[str, List[float]]]:
    """ollama /api/embed 批量嵌入; 不可用/失败 → None (降级纯词法)。

    失败原因写入模块级 _LAST_EMBED_FAIL_REASON, 供统一入口 _embed_texts
    转成 stat["embed_fail_reason"]; 本函数自身仍保证不抛。
    """
    global _LAST_EMBED_FAIL_REASON
    if not texts:
        return None
    _LAST_EMBED_FAIL_REASON = None
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
            _LAST_EMBED_FAIL_REASON = "ollama: batch size mismatch"
            return None
        return {t: v for t, v in zip(texts, vecs)}
    except Exception as e:
        _LAST_EMBED_FAIL_REASON = f"ollama: {type(e).__name__}"
        return None


def _embed_texts(client, texts: List[str]) -> Optional[List[List[float]]]:
    """统一嵌入入口: merge/activity 只经过这里, 不再各自直连 ollama。

    路由:
      mnemosyne -> client.embed_texts, 按 EMBED_BATCH_MAX 分片, 返回
                   List[List[float]] (与 recall 同分数空间);
      ollama    -> 本地 _embed_batch, dict 按输入顺序转 list;
      off       -> None (显式纯词法);
    任何失败/异常 -> None, 原因写入 _LAST_EMBED_FAIL_REASON 供调用方
    转存 stat["embed_fail_reason"]; 绝不抛出, 保持安全降级方向。
    """
    global _LAST_EMBED_FAIL_REASON
    _LAST_EMBED_FAIL_REASON = None
    if not texts:
        return []
    backend = _embed_backend()
    if backend == "off":
        _LAST_EMBED_FAIL_REASON = "off: disabled"
        return None
    if backend == "ollama":
        try:
            res = _embed_batch(texts)  # _embed_batch 内部已写具体错误类型
        except Exception as e:
            _LAST_EMBED_FAIL_REASON = f"ollama: {type(e).__name__}"
            return None
        if not isinstance(res, dict):
            if not _LAST_EMBED_FAIL_REASON:
                _LAST_EMBED_FAIL_REASON = "ollama: unavailable"
            return None
        out: List[List[float]] = []
        for text in texts:
            vec = res.get(text)
            if not isinstance(vec, list) or not vec:
                _LAST_EMBED_FAIL_REASON = "ollama: invalid vector"
                return None
            out.append(vec)
        return out
    if backend != "mnemosyne":
        _LAST_EMBED_FAIL_REASON = f"{backend}: unsupported backend"
        return None
    if client is None or not hasattr(client, "embed_texts"):
        _LAST_EMBED_FAIL_REASON = "mnemosyne: client unavailable"
        return None
    out: List[List[float]] = []
    try:
        for i in range(0, len(texts), EMBED_BATCH_MAX):
            chunk = texts[i:i + EMBED_BATCH_MAX]
            vecs = client.embed_texts(chunk)
            if (not isinstance(vecs, (list, tuple))
                    or len(vecs) != len(chunk)):
                _LAST_EMBED_FAIL_REASON = "mnemosyne: invalid batch result"
                return None
            for vec in vecs:
                if not isinstance(vec, (list, tuple)):
                    _LAST_EMBED_FAIL_REASON = "mnemosyne: invalid vector"
                    return None
            out.extend([list(v) for v in vecs])
        return out
    except Exception as e:
        _LAST_EMBED_FAIL_REASON = f"mnemosyne: {type(e).__name__}"
        return None


# 兼容旧内部调用名: 仍可 import/直接调用, 行为收敛到统一入口。
def _embed_batch_mnemosyne(client, texts):
    return _embed_texts(client, texts)


def _cosine(a: List[float], b: List[float]) -> float:
    """向量余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return (dot / (na * nb)) if na > 0 and nb > 0 else 0.0


# ---- 主入口 ----------------------------------------------------------------

def _compute_plateau_reason(store, metastore, target: str,
                            stat: dict) -> Optional[str]:
    """Q6 (2026-09-12): 合法平台期上报。

    仅当 usage_after > TARGET_RATIO 时计算:
      - cold_backstop: 本轮存在冷层不可达/冷写失败兜底条目;
      - protected_only: errors==0、审计口径 sink_candidates==0、
        LRU 候选==0, 且残余条目全部属于 {protected rule, 未到期 state,
        stub 指针}。
    其余情况不输出 plateau_reason (不算合法平台期)。
    """
    if _cache_policy_v2():
        # CACHE-POLICY-V2 (Q1/Q5): 只允许真实数据安全暂停原因;
        # 删除 protected_only / ambiguous_hold 平台期 (无永久驻留)。
        # 冷层不可达优先上报, 即使热层未超水位也必须是可见降级态。
        if stat.get("cold_errors", 0) > 0:
            return "cold_backstop"
        if store.usage_pct(target) <= TARGET_RATIO * 100:
            return None
        return None
    if store.usage_pct(target) <= TARGET_RATIO * 100:
        return None
    if stat.get("cold_errors", 0) > 0:
        return "cold_backstop"
    if stat.get("errors", 0) > 0:
        return None
    entries = store.entries(target)
    if not entries:
        return None
    now = datetime.now(timezone.utc)
    sink_candidates = 0
    ambiguous_hold = 0
    residual_allowed = True
    for e in entries:
        m = metastore.get_entry(e)
        if not m:
            residual_allowed = False
            continue
        etype = m.get("type")
        override = m.get("type_override")
        age = entry_age_days(m, now=now, entry=e)
        # B-2/C-4: A0-hold 是带 review_at 期限的合法平台期; A1 交 stub 通道
        if (m.get("judge_decision") == "ambiguous"
                and override not in ("state", "rule")
                and m.get("judge_resolution") != "rule"):
            if _ambiguous_hold_valid(m, now, entry=e) and not _ambiguous_a1_eligible(m, now, entry=e):
                ambiguous_hold += 1
                continue
            residual_allowed = False
            continue
        if etype == "stub":
            continue
        if etype == "state":
            if override == "state" or (age is not None and age >= STATE_TTL_DAYS):
                sink_candidates += 1
            continue
        if etype == "rule":
            if not _is_protected_rule(e, m):
                residual_allowed = False
                if not should_keep_local(e):
                    sink_candidates += 1
            continue
        residual_allowed = False
    try:
        # 只读审计: 不向主 stat 写入宽限让位等内部计数 (FIX6 R3 已落盘).
        lru_candidates = _select_retirement_candidates(
            store, metastore, target, 10 ** 9, {})
    except Exception:
        lru_candidates = []
    if sink_candidates == 0 and not lru_candidates and residual_allowed:
        if ambiguous_hold:
            return "ambiguous_hold"
        return "protected_only"
    return None


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
        "trash_fail": 0,         # E3: 回收队列写入失败数 (可恢复性保护告警)
        "embed_fail": 0,         # E5: 嵌入失败数 (activity/merge 路径同口径)
        "cold_errors": 0,        # Q6: 冷层不可达/冷写失败兜底计数
    }

    # LLM 护栏会话 (评审 C2): 每轮调用上限 + 连续失败退避 + 观测三态,
    # 状态经 close() 写入 stat["llm"] (进 MCP 工具返回 / weekly 报告)。
    # 低危修复 (终审): close 进 finally — 异常路径也写 stat["llm"] 并复位
    # contextvar, 防泄漏到死 guard (下次 start_session 自愈前不再静默)。
    llm_guard = llm_config.start_session(stat=stat, name="overflow")
    try:
        return _run_overflow(store, client, target, stat)
    finally:
        llm_guard.close()


def _run_overflow(store, client, target: str, stat: Dict[str, Any]) -> dict:
    """run_overflow 主体 (护栏会话生命周期由 run_overflow 管理)。"""

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
                stat["errors"] = stat.get("errors", 0) + 1
                stat["cold_errors"] = stat.get("cold_errors", 0) + 1
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
    merged_entries, merge_count = _merge_local_fragments(entries, stat, client)
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
        stat["errors"] = stat.get("errors", 0) + 1
        stat["metadata_stamped"] = 0

    # ---- Phase 4 Step A: 活性命中扫描 (语义两级判定; 失败降级不阻塞) ----
    try:
        apply_activity_hits({target: metastore}, {target: entries}, client, stat)
    except Exception as e:
        # E4 可见化: 活性扫描失败不再零信号 (原 except: pass 静默)
        stat["errors"] = stat.get("errors", 0) + 1
        log.warning("ACTIVITY: 活性命中扫描失败 (%s) → 本轮无新信号, 历史权重继续", e)

    # ---- L2 预规划 (Phase 3): 高压下休眠 B 类 rule → stub 候选 ------------
    stub_candidates: Set[str] = set()
    if store.usage_pct(target) >= HARD_THRESHOLD * 100:
        try:
            stub_candidates = set(_plan_stub_candidates(
                metastore, client, entries))
        except Exception:
            stub_candidates = set()  # 规划失败 → 保守不 stub (机制降级)

    # ---- Step 2+3+5: 逐条处理 --------------------------------------------
    # L2 低危收尾 (2026-09-12): 轮转游标 — 每轮 LLM 上限截断的候选无持久
    # 标记, 若头部候选持续"成功但不可消解" (压缩校验不过), 下一轮按稳定
    # 文件序重建会从同一头部再消耗预算, 尾部永久饿死 (实测修复前)。
    # 轮转保证任一候选在 ceil(N/C) 轮内至少被尝试一次。
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
                        else:
                            stat["cold_errors"] = stat.get("cold_errors", 0) + 1
                        stat["errors"] = stat.get("errors", 0) + 1
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
    # F2 (2026-09-13): 若 rule 全文仍超内容预算, 指针 GC 必须让位于
    # enforce_rule_budget 的两阶段全文换出, 防"删指针保全文"。
    _full_rule_chars = 0
    for _e in store.entries(target):
        _m = metastore.get_entry(_e) or {}
        if _m.get("type") == "rule":
            _full_rule_chars += len(_e)
    if (stat.get("stubbed", 0) == 0
            and _full_rule_chars <= _effective_rule_budget_chars()
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
    reason = _compute_plateau_reason(store, metastore, target, stat)
    if reason:
        stat["plateau_reason"] = reason
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
      v2 (2026-09-12): type_override=state 的人工标注条目 → S0 写入即迁
      (冷层可达时立即冷迁移, 不等待 TTL; 冷层失败留热层兜底)。
    - rule (Phase 3 分层保护, 2026-08-20):
        * S6: A 类/红线类/importance≥0.9 → 只走长压缩, 不 stub/不 retype/不跨层删
        * S0 (v2 2026-09-12): kw-sink 门槛 HARD(80%)→SOFT(60%) (DESIGN §Q2 S1)
        * S2 (L1 ≥60%): 完成态复核 → retype state → TTL 下沉
        * S5 (L1 ≥60%, 闲置≥30d): 冷层已有等价全文 → 删本地
        * S4 (L2 ≥80%): 休眠确认 → 全文沉冷层 + stub 指针留本地
        * 既有: age ≥ RULE_COMPRESS_DAYS 长条目 → LLM 压缩 (失败保留原样)
    - 未知类型 → 返回 False, 调用方回退 legacy 关键词路径。

    Returns: True = 已处理 (调用方 continue); False = 回退 legacy。
    """
    etype = meta.get("type")
    # v2 (2026-09-12): 人工标注 type_override 优先于词法盖章
    # (memorycore_set_entry_type 只写 sidecar; 下次溢流按标注接管)。
    override = meta.get("type_override")
    if override in ("state", "rule"):
        etype = override
    now = datetime.now(timezone.utc)
    age = entry_age_days(meta, now=now, entry=entry)

    # SAFE-JUDGE v3 B-2: ambiguous 分支优先于普通 rule/state 路径; 永不全文冷迁
    # CACHE-POLICY-V2: ambiguous 不再有资格豁免/独立 hold, 作为低先验条目
    # 与 rule 同池参与统一排序换出; 旧语义仅在回滚开关下保留。
    if (not _cache_policy_v2()
            and meta.get("judge_decision") == "ambiguous"
            and override not in ("state", "rule")
            and getattr(_cfg, "JUDGE_AMBIGUOUS_HOLD", True)):
        if meta.get("judge_resolution") == "rule":
            # R-grace (C-1/C-2): LLM 终审判 rule 后 14d 内不参与任何自动退役;
            # grace 过期回到普通 rule 的 S0/S2/S5/S4 阶梯。
            _ra = _ts_anchor(meta.get("judge_resolved_at"),
                             datetime.now(timezone.utc),
                             field="judge_resolved_at", sha=_entry_sha(entry))
            if _ra is not None and (datetime.now(timezone.utc) - _ra) < timedelta(
                    days=JUDGE_RESOLVED_RULE_GRACE_DAYS):
                stat["kept"] += 1
                stat["resolved_rule_grace"] = stat.get(
                    "resolved_rule_grace", 0) + 1
                return True
        else:
            return _handle_ambiguous_entry(store, client, target, entry, meta,
                                           metastore, stat, anchor_parts)

    if etype == "stub":
        stat["kept"] += 1  # 指针永久驻留; 高压回收走 Step 5.5 stub GC
        return True

    if etype == "state":
        if _cache_policy_v2():
            # CACHE-POLICY-V2: 删除 state/ambiguous 直接换出分支。
            # 热层中的 state 与 rule/stub 同池, 由活性+预算统一换出; 类型只作
            # 初始权重先验 (冷层优先写入由写路径/直写通道负责)。
            stat["kept"] += 1
            return True
        if age is not None and age >= STATE_TTL_DAYS:
            before = len(store.entries(target))
            if target == "user" and classify_user_pref(
                    entry, sentence_level=True) == "sink":
                anchor_parts.append(entry)
            _handle_cold_migration(store, client, target, entry, stat)
            if len(store.entries(target)) < before:
                stat["aged_sunk"] += 1  # 只有条目真的离开热层才计数
            return True
        if override == "state":
            # S0 (Q2, 2026-09-12): 人工标注 state → 写入即迁 (冷层可达时),
            # 不等待 7 天 TTL; 冷层失败留热层兜底 (下次溢流再试)。
            before = len(store.entries(target))
            if target == "user" and classify_user_pref(
                    entry, sentence_level=True) == "sink":
                anchor_parts.append(entry)
            _handle_cold_migration(store, client, target, entry, stat)
            if len(store.entries(target)) < before:
                stat["aged_sunk"] += 1
            return True
        stat["kept"] += 1  # 未到期, 暂留
        return True

    if etype == "rule":
        protected = _is_protected_rule(entry, meta)
        usage = store.usage_pct(target)

        # S0 (2026-08-26 修复; v2 2026-09-12 门槛 HARD→SOFT): 关键词视图已判
        # 可沉 (强 sink 组合) → 即时冷迁移。Q2 S1: 非 protected 时占用 ≥60%
        # 即冷迁移 (不再等到 80% — 技术/环境/服务器事实类规则在 60% 水位就沉)。
        if (not protected
                and usage >= SOFT_THRESHOLD * 100
                and not should_keep_local_rule_view(entry)):
            _handle_cold_migration(store, client, target, entry, stat)
            return True

        # S2 (L1): 完成态复核 → retype → TTL 下沉 (冷层失败恢复原 rule 章)
        if (not protected
                and usage >= SOFT_THRESHOLD * 100
                and _rule_retype_eligible(entry)):
            _handle_rule_retype(store, client, target, entry, meta,
                                metastore, stat, anchor_parts)
            return True

        # S5 (L1): 跨层冗余 — 闲置 ≥ CROSS_DEDUP_MIN_IDLE_DAYS 才查冷层
        # (历史冗余面向, 省 recall 开销; 冷层已有等价全文 → 删本地零丢失)
        if (not protected
                and usage >= SOFT_THRESHOLD * 100
                and age is not None and age >= CROSS_DEDUP_MIN_IDLE_DAYS
                and _try_cross_layer_dedup(store, client, target, entry, stat)):
            return True

        # S4 (L2): 休眠 stub-sink (候选由 run_overflow L2 预规划给出)
        if (not protected
                and usage >= HARD_THRESHOLD * 100
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
                            _inherit_kw = _meta_to_stamp_kwargs(
                                meta, entry, origin="overflow",
                                protected=protected)
                            metastore.stamp(compressed, "rule", **_inherit_kw)
                        except Exception as e:
                            # F1/E9 可见化: 盖章失败不再静默 (下次 reconcile 补)
                            stat["stamp_skipped"] = stat.get("stamp_skipped", 0) + 1
                            log.debug("META: stamp 失败 (%s) → 下次 reconcile 补", e)
                        stat["compressed"] += 1
                        anchor_parts.append(entry)
                        return True
                else:
                    stat["cold_errors"] = stat.get("cold_errors", 0) + 1
                stat["errors"] = stat.get("errors", 0) + 1
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
        stat["errors"] = stat.get("errors", 0) + 1
        stat["cold_errors"] = stat.get("cold_errors", 0) + 1
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
            log.warning("ANCHOR: 偏好锚点基础内容读取失败 (%s) → 仅用本轮新增内容重建", e)
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
                stat["errors"] = stat.get("errors", 0) + 1
                stat["cold_errors"] = stat.get("cold_errors", 0) + 1
        else:
            r = client.remember(content, importance=_ANCHOR_IMPORTANCE,
                                scope="global")
            if r.get("status") == "stored":
                stat["anchor_created"] = stat.get("anchor_created", 0) + 1
            else:
                stat["errors"] = stat.get("errors", 0) + 1
                stat["cold_errors"] = stat.get("cold_errors", 0) + 1
    except Exception:
        stat["errors"] = stat.get("errors", 0) + 1
        stat["cold_errors"] = stat.get("cold_errors", 0) + 1


# ---- Step 2+5: 冷迁移 ----------------------------------------------------

def _handle_cold_migration(store, client, target: str, entry: str,
                           stat: dict) -> None:
    """冷候选条目: 查重 → (跳过/update/remember) → 删本地。"""
    try:
        existing = _recall_safe(client, entry)
    except Exception:
        stat["errors"] = stat.get("errors", 0) + 1
        stat["cold_errors"] = stat.get("cold_errors", 0) + 1
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
                        # P1-6 + E3: 写入侧反转覆盖前, 旧条先进回收队列
                        # (写成功才许 update 覆盖; 写失败 → 计数 + 中止)
                        from ..trash_store import TrashStore, add_observed
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
                        stat["errors"] = stat.get("errors", 0) + 1
                        stat["cold_errors"] = stat.get("cold_errors", 0) + 1
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
                    stat["errors"] = stat.get("errors", 0) + 1
                    stat["cold_errors"] = stat.get("cold_errors", 0) + 1
                    return  # update 失败 -> 本地保留
            # 匹配不满足阈值或 update 返回非 updated → 降级到 remember

    # 无匹配 → 新写入冷层
    try:
        r = client.remember(entry, importance=0.6, scope="global")
        if r.get("status") == "stored":
            _safe_remove_local(store, target, entry, stat)
            stat["overflowed"] += 1
        else:
            stat["errors"] = stat.get("errors", 0) + 1
            stat["cold_errors"] = stat.get("cold_errors", 0) + 1
    except Exception:
        stat["errors"] = stat.get("errors", 0) + 1
        stat["cold_errors"] = stat.get("cold_errors", 0) + 1


# ---- Step 3: 过时处理 ----------------------------------------------------

def _handle_stale(store, client, target: str, entry: str, stat: dict) -> None:
    """过时条目: 冷层匹配条目先进回收队列再 forget, 然后删本地。

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
                        # E3: 回收队列写成功才许 forget (写失败 → 计数, 源保留)
                        from ..trash_store import TrashStore, add_observed
                        if not add_observed(
                                TrashStore(), ex["id"], ex.get("content", ""),
                                reason, "rule_stale", stat):
                            continue
                        client.forget(ex["id"])
                    except Exception:
                        pass  # forget 失败不阻塞 (回收队列已有备份)
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
                           stat: dict = None,
                           client=None) -> Tuple[List[str], int]:
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
    # E5/R5: merge 路径走统一入口 _embed_texts(client, ...); 失败与 activity
    # 路径同口径计数 (embed_fail), 原因只记首次 (embed_fail_reason), 不静默。
    mergeable = [e for i, e in enumerate(entries) if i not in skip]
    emb_map = None
    if len(mergeable) >= 2:
        emb_vecs = _embed_texts(client, mergeable)
        if emb_vecs is not None and len(emb_vecs) == len(mergeable):
            emb_map = {text: vec for text, vec in zip(mergeable, emb_vecs)}
        else:
            _record_embed_fail(stat)
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
    删除会导致同义句 \"D 盘=/data/code (Code Drive...)\" 与
    \"D 盘=Code Drive (path=/data/code...)\" 规范化后反而不同。
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


_UNIT_SPLIT_RE = re.compile(r"([。！？;；\n])")


def _iter_content_units(text: str):
    """Yield (body, delimiter) pairs while preserving original separators.

    ``re.split`` keeps the matched separators in the odd slots.  Callers can
    rebuild the input exactly with ``"".join(body + delim ...)``; this is what
    keeps newlines/semicolons lossless during merge.
    """
    parts = _UNIT_SPLIT_RE.split(text or "")
    for i in range(0, len(parts), 2):
        body = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if body or delim:
            yield body, delim


def _merge_two_entries(local: str, cold: str) -> str:
    """合并本地新条目与冷层已有条目。冲突取最新(本地 newer)。

    2026-09-19 round-3B: 旧实现用 ``rstrip("。！？;；\n") + "。"`` 重拼，
    会把代码/命令里的换行和分号改写成中文句号，也会制造 ``。。``。
    现在只追加“冷层独有句段”，并保留该句段自带的原始分隔符；新增句段
    之间用中性换行衔接。合并结果对中文是换行分段，对代码保持语法有效。
    """
    base = local if isinstance(local, str) else ""
    cold_text = cold if isinstance(cold, str) else ""
    if not base.strip():
        return cold_text
    if not cold_text.strip():
        return base

    # 句段级去重仍以规范化后完全相等为界，避免把“同模板 + 唯一标记”
    # 的旧句（如唯一订单号/实体名）误吞掉。
    local_norms = [_norm_sentence(body) for body, _ in _iter_content_units(base)]
    local_norms = [n for n in local_norms if n]

    additions = []
    for body, delim in _iter_content_units(cold_text):
        s = body.strip()
        if not s:
            continue
        sn = _norm_sentence(s)
        if not sn:
            continue
        if any(sn == ln for ln in local_norms):
            continue
        additions.append(body + delim)

    if not additions:
        return base

    out = base
    for add in additions:
        # Base already carries a structural separator (newline/semicolon) in
        # code-like text; otherwise use a neutral newline, never a period.
        sep = "" if out.endswith(("\n", ";", "；")) else "\n"
        out = out + sep + add
    return out


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
            stat["errors"] = stat.get("errors", 0) + 1
    except Exception:
        stat["errors"] = stat.get("errors", 0) + 1


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
                     now: Optional[datetime] = None,
                     entry: str = "") -> float:
    """惰性折现权重: w_eff = weight × 0.5 ^ ((now-last_active)/half_life)。

    排序时才计算, 不写 sidecar (与冷层 decay.py 同模式, 无后台任务)。
    缺失字段回退: weight=WEIGHT_INIT, last_active=written_at (零迁移成本)。
    R2 (FIX6): last_active 必须走 _ts_anchor 唯一入口; 未来时间戳夹到 now
    并计入 ts_anomaly, 不会被折现成"永久最新".
    """
    now = now or datetime.now(timezone.utc)
    weight = float(meta.get("weight") or WEIGHT_INIT)
    last = meta.get("last_active_at") or meta.get("written_at") or meta.get("updated_at")
    sha = hashlib.sha256((entry or "").encode()).hexdigest() if entry else ""
    dt = _ts_anchor(last, now, field="last_active_at",
                    sha=sha) if last else None
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
            # R2: 扫描下界锚点同样走 _ts_anchor; future last_scan_at 不得
            # 让扫描下界越过 now 而丢弃全部 fresh 查询.
            dt = _ts_anchor(ts, now, field="last_scan_at") if ts else None
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


# R5: _embed_batch_mnemosyne 已并入 _embed_texts 统一入口 (兼容别名见文件前部)。
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
            mvecs = _embed_texts(client, missing)
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


def _anchored_meta_time(meta: Dict[str, Any], entry: str, key: str,
                        now: datetime, stat: Optional[dict] = None
                        ) -> Optional[datetime]:
    """FIX7 I2: 活动扫描/降级路径读取旧时间戳的唯一入口。

    与 _meta_to_stamp_kwargs 的 _t 同语义: 空值 None, 其余走 _ts_anchor
    (未来夹 now + ts_anomaly; 解析失败 None + ts_anomaly)。所有调用方
    在换形/回写时读取 meta 字段, 不应再各自 _parse_iso。
    """
    val = meta.get(key)
    if not val:
        return None
    return _ts_anchor(val, now=now, field=key,
                      sha=_entry_sha(entry), sink=stat)


def apply_activity_hits(metastores: Dict[str, MetaStore],
                        entries_by_target: Dict[str, List[str]],
                        client, stat: dict) -> None:
    """Phase 4 活性信号: 语义两级判定 (实测校准, 2026-08-26)。

    fresh 查询 → 批量嵌入 → 与规则向量全对余弦 (缓存后 ≈ 免费)
      cos_max ≥ HIT_STRONG_COS → 强命中 +1.0/轮 (封顶, 刷新锚点)
      embedding 不可达 → 降级纯词法弱命中 +0.3 (HIT_WEAK_MODE=degraded)
    词法证据 (sb≥2) 正常模式仅作审计字段 (hits_lex), 不加分。
    v2 (2026-09-12, DESIGN §Q2): 强命中写 last_strong_hit_at,
    灰区/词法弱命中写 last_weak_hit_at; last_active_at 仍只在强命中刷新
    (弱命中不伪装成强活跃, 保留分级区分度)。
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
    # 查询嵌入 (每轮唯一真实成本, 1 批) — R5: 统一入口, 与 merge 同口径。
    qvecs = _embed_texts(client, queries)
    if qvecs is None:
        # 降级: 纯词法弱命中 (degraded 模式)
        if _cfg.HIT_WEAK_MODE == "degraded":
            _degraded_lexical_hits(metastores, rules, queries, stat)
        _record_embed_fail(stat)
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
                meta["last_strong_hit_at"] = now.isoformat()
                stat["hits_strong"] = stat.get("hits_strong", 0) + 1
            elif (_cfg.HIT_WEAK_MODE == "grey" and cos_max >= HIT_WEAK_COS
                    and any(_lex_evidence(q, e) for q in queries)):
                # grey 档 (用户可开): 灰区语义 + 词法佐证 → 弱命中 (不刷新锚点)
                _bump_weight(meta, HIT_WEAK_INCREMENT, refresh_anchor=False, now=now)
                meta["last_weak_hit_at"] = now.isoformat()
                stat["hits_weak"] = stat.get("hits_weak", 0) + 1
            else:
                if any(_lex_evidence(q, e) for q in queries):
                    stat["hits_lex"] = stat.get("hits_lex", 0) + 1
            meta["last_scan_at"] = now.isoformat()
            try:
                metastores[target].stamp(
                    e, meta.get("type", "rule"),
                    written_at=_anchored_meta_time(
                        meta, e, "written_at", now, stat),
                    updated_at=_anchored_meta_time(
                        meta, e, "updated_at", now, stat),
                    origin=meta.get("origin", "hermes"),
                    importance=float(meta.get("importance") or 0.8),
                    weight=float(meta.get("weight") or WEIGHT_INIT),
                    last_active_at=_anchored_meta_time(
                        meta, e, "last_active_at", now, stat),
                    last_scan_at=now,
                    cold_id=meta.get("cold_id"),
                    type_override=meta.get("type_override"),
                    type_source=meta.get("type_source"),
                    protect_override=meta.get("protect_override"),
                    last_strong_hit_at=_anchored_meta_time(
                        meta, e, "last_strong_hit_at", now, stat),
                    last_weak_hit_at=_anchored_meta_time(
                        meta, e, "last_weak_hit_at", now, stat),
                    last_recall_hit_at=_anchored_meta_time(
                        meta, e, "last_recall_hit_at", now, stat),
                    last_injected_at=_anchored_meta_time(
                        meta, e, "last_injected_at", now, stat),
                    last_evicted_at=_anchored_meta_time(
                        meta, e, "last_evicted_at", now, stat),
                    writeback_count=(int(meta["writeback_count"])
                                     if meta.get("writeback_count") is not None else None),
                    retire_count=(int(meta["retire_count"])
                                  if meta.get("retire_count") is not None else None),
                    handle=meta.get("handle"),
                    # FIX7 I4: 活动扫描不是出生重置; 显式继承旧标记,
                    # 否则带 reconcile 兜底的 legacy 条目会在扫描时被
                    # 当成"显式重置出生"而重获宽限.
                    reconcile_anchor_fallback=meta.get(
                        "reconcile_anchor_fallback"),
                )
            except Exception:
                pass  # 盖章失败不影响机制 (下次 reconcile 补)


def _degraded_lexical_hits(metastores: Dict[str, MetaStore],
                           rules: Dict[str, List[str]],
                           queries: List[str], stat: dict) -> None:
    """降级模式弱命中: 任一 fresh 查询共享 bigram ≥2 → +0.3/轮 (封顶, 不刷新锚点)。

    v2 (2026-09-12): 弱命中写 last_weak_hit_at (Q2 warm 级输入), 不刷新 last_active_at。
    """
    now = datetime.now(timezone.utc)
    for target, rs in rules.items():
        for e in rs:
            meta = metastores[target].get_entry(e)
            if not meta:
                continue
            if any(_lex_evidence(q, e) for q in queries):
                _bump_weight(meta, HIT_WEAK_INCREMENT, refresh_anchor=False, now=now)
                meta["last_weak_hit_at"] = now.isoformat()
                stat["hits_weak"] = stat.get("hits_weak", 0) + 1
            meta["last_scan_at"] = now.isoformat()
            try:
                metastores[target].stamp(
                    e, meta.get("type", "rule"),
                    written_at=_anchored_meta_time(
                        meta, e, "written_at", now, stat),
                    updated_at=_anchored_meta_time(
                        meta, e, "updated_at", now, stat),
                    weight=float(meta.get("weight") or WEIGHT_INIT),
                    last_active_at=_anchored_meta_time(
                        meta, e, "last_active_at", now, stat),
                    last_scan_at=now,
                    origin=meta.get("origin", "hermes"),
                    importance=float(meta.get("importance") or 0.8),
                    cold_id=meta.get("cold_id"),
                    type_override=meta.get("type_override"),
                    type_source=meta.get("type_source"),
                    protect_override=meta.get("protect_override"),
                    last_strong_hit_at=_anchored_meta_time(
                        meta, e, "last_strong_hit_at", now, stat),
                    last_weak_hit_at=_anchored_meta_time(
                        meta, e, "last_weak_hit_at", now, stat),
                    last_recall_hit_at=_anchored_meta_time(
                        meta, e, "last_recall_hit_at", now, stat),
                    last_injected_at=_anchored_meta_time(
                        meta, e, "last_injected_at", now, stat),
                    last_evicted_at=_anchored_meta_time(
                        meta, e, "last_evicted_at", now, stat),
                    writeback_count=(int(meta["writeback_count"])
                                     if meta.get("writeback_count") is not None else None),
                    retire_count=(int(meta["retire_count"])
                                  if meta.get("retire_count") is not None else None),
                    handle=meta.get("handle"),
                    # FIX7 I4: 降级扫描同样继承 reconcile 兜底标记.
                    reconcile_anchor_fallback=meta.get(
                        "reconcile_anchor_fallback"),
                )
            except Exception:
                pass


def _ambiguous_a1_eligible(meta: Dict[str, Any], now: datetime,
                           entry: str = "") -> bool:
    """A1-stub 资格: ambiguous 且 age≥21d 或 review_count≥2 (保留 21d 兜底)。"""
    if meta.get("judge_decision") != "ambiguous":
        return False
    if meta.get("judge_resolution") == "rule":
        return False  # R-grace 已解决, 走普通 rule 语义
    age = entry_age_days(meta, now=now, entry=entry)
    if age is not None and age >= JUDGE_AMBIGUOUS_LRU_DAYS:
        return True
    return int(meta.get("judge_review_count") or 0) >= JUDGE_AMBIGUOUS_MAX_REVIEWS


def _ambiguous_hold_valid(meta: Dict[str, Any],
                          now: Optional[datetime] = None,
                          entry: str = "") -> bool:
    """A0-hold 必须带有效 review_at 期限; 无期限/不可解析不当 A0 保护。"""
    if meta.get("judge_decision") != "ambiguous":
        return False
    if meta.get("judge_resolution") == "rule":
        return False
    now = now or datetime.now(timezone.utc)
    sha = hashlib.sha256((entry or "").encode()).hexdigest() if entry else ""
    return _ts_anchor(meta.get("judge_review_at"), now,
                      field="judge_review_at", sha=sha,
                      allow_future=True) is not None


def _handle_ambiguous_entry(store, client, target: str, entry: str,
                            meta: dict, metastore, stat: dict,
                            anchor_parts: List[str]) -> bool:
    """A0-hold / A1-stub 出口 (B-1/B-2): 永不全文冷迁, 只允许 stub-sink。"""
    now = datetime.now(timezone.utc)
    if _ambiguous_a1_eligible(meta, now, entry=entry):
        # 只在压力/预算下触发; 未超预算则继续 hold, 不空转
        rule_chars = 0
        for e in store.entries(target):
            m = metastore.get_entry(e) or {}
            if m.get("type") in ("rule", "stub"):
                rule_chars += len(e)
        if (store.usage_pct(target) >= SOFT_THRESHOLD * 100
                or rule_chars > RULE_BUDGET_CHARS):
            _handle_rule_stub_sink(store, client, target, entry,
                                   metastore, stat, anchor_parts)
            return True
        stat["kept"] += 1
        return True
    # A0: 留热层, 不 retype / 不跨层 dedup / 不整条冷迁
    stat["kept"] += 1
    stat["ambiguous_hold"] = stat.get("ambiguous_hold", 0) + 1
    return True


def _rule_activity_tier(meta: Dict[str, Any], entry: str,
                        queries_7d: List[str],
                        now: datetime) -> Tuple[str, int]:
    """Q2 (2026-09-12): 三级驻留分级 — 返回 (activity_tier, min_retire_age_days)。

      resolved_rule_grace: LLM 终审判 rule 后 14d 内不参与 LRU；
      active: 近 7 天有强语义命中 (last_strong_hit_at)            → 30 天
      warm:   近 7 天有弱语义命中 (last_weak_hit_at) 或现场词法命中
              (近 7 天查询 sb≥2, 零 LLM)                          → 14 天
      idle:   其余 (7 天无强命中、无词法命中)                       → 7 天
    旧字段缺失 (last_strong_hit_at=None) → strong=无, 弱=现场词法兜底,
    行为等价于旧数据全按 idle/warm 处理, 不会锁死 (DESIGN §4)。
    """
    if meta.get("judge_resolution") == "rule":
        _resolved = _ts_anchor(meta.get("judge_resolved_at"), now,
                               field="judge_resolved_at",
                               sha=hashlib.sha256(entry.encode()).hexdigest())
        if _resolved is not None and (now - _resolved) < timedelta(
                days=JUDGE_RESOLVED_RULE_GRACE_DAYS):
            return "resolved_rule_grace", JUDGE_RESOLVED_RULE_GRACE_DAYS
    if meta.get("judge_decision") == "ambiguous" \
            and meta.get("judge_resolution") != "rule":
        return "ambiguous", JUDGE_AMBIGUOUS_LRU_DAYS
    strong = _ts_anchor(meta.get("last_strong_hit_at"), now,
                        field="last_strong_hit_at",
                        sha=hashlib.sha256(entry.encode()).hexdigest())
    weak = _ts_anchor(meta.get("last_weak_hit_at"), now,
                      field="last_weak_hit_at",
                      sha=hashlib.sha256(entry.encode()).hexdigest())
    week = timedelta(days=7)
    if strong is not None and (now - strong) < week:
        return "active", RULE_MIN_RESIDENCY_ACTIVE_DAYS
    if ((weak is not None and (now - weak) < week)
            or (queries_7d and any(_lex_evidence(q, entry) for q in queries_7d))):
        return "warm", RULE_MIN_RESIDENCY_WARM_DAYS
    return "idle", RULE_MIN_RESIDENCY_IDLE_DAYS


def _rule_rank(entry: str, meta: Dict[str, Any], now: datetime,
               *, apply_grace: bool = True) -> float:
    """退役排序值: w_rank = w_eff × protected(×3) × kw_sink(×0.5) × GRACE_MULT。

    升序 = 先被换出。FIX8 B1 (2026-09-13): 新鲜窗口不再是候选档位/资格,
    只是第四个排序乘数 (与 protected 完全同构):
      - 新鲜窗口 = written_at 或 last_recall_hit_at (经 _ts_anchor 归一化)
        距今 ≤ RULE_MIN_RESIDENCY_DAYS;
      - 窗口内 ×GRACE_MULT; 窗口外 / 时间戳不可信 (未来/解析失败) 不乘;
      - 有界且非豁免: 乘数有限, 排序压力足够时仍会被选走; 窗口结束后
        w_eff 继续按 30d 半衰期自然衰减。
    protected 始终只是 ×WEIGHT_PROTECT_MULT 的排序乘数, 不再有前置过滤/
    年龄门; kw 可沉型 (should_keep_local=False) → ×WEIGHT_KWSINK_MULT。
    `apply_grace=False` 仅供 CACHE_POLICY_V2=0 的 legacy 候选选择器冻结旧
    排序主体 (回滚对照), 生产 v2 路径与审计一律默认 True。
    """
    eff = _rule_weight_eff(meta, now, entry=entry)
    mult = WEIGHT_PROTECT_MULT if _is_protected_rule(entry, meta) else 1.0
    if not should_keep_local_rule_view(entry):
        mult *= WEIGHT_KWSINK_MULT
    if apply_grace and _soft_residency_grace_ts(meta, now, entry=entry) \
            is not None:
        mult *= _effective_grace_mult()
    return eff * mult


# ---- FIX5/FIX8: 新鲜窗口 (B1 排序乘数输入, 2026-09-13) ----------------------

def _effective_soft_residency_days() -> float:
    """运行时软驻留窗口天数 (FIX5)。

    生产 ov/config 两处同值; 测试可能 monkeypatch 任意一处, 取较小值:
    任一处改为 <=0 都表示宽限整体关闭 (不加新开关/环境变量)。
    """
    vals = []
    for v in (globals().get("RULE_MIN_RESIDENCY_DAYS"),
              getattr(_cfg, "RULE_MIN_RESIDENCY_DAYS", None)):
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    return min(vals) if vals else 0.0


def _effective_grace_mult() -> float:
    """FIX8 B1: 新鲜窗口排序乘数 (测试可 patch overflow/config 任一处)。

    生产 ov/config 两处同值; 测试取较小值, 任一处 <=0 都表示关闭乘数
    (等价旧纯 _rule_rank)。默认值见 core/config.GRACE_MULT 的标定注释。
    """
    vals = []
    for v in (globals().get("GRACE_MULT"),
              getattr(_cfg, "GRACE_MULT", None)):
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            continue
    if not vals:
        return 1.0
    val = min(vals)
    return val if val > 0 else 1.0


def _entry_sha(entry: str) -> str:
    return hashlib.sha256((entry or "").encode("utf-8")).hexdigest()


def _soft_residency_anchor(meta: Dict[str, Any],
                           now: Optional[datetime] = None,
                           entry: str = "") -> Optional[datetime]:
    """FIX8 新鲜窗口锚点 = max(written_at, last_recall_hit_at) (最近写入/写回)。

    FIX6 R1/R2:
      - reconcile 兜底 (无内嵌日期, written_at 只是补章时间) 不享宽限;
      - 字段时间戳统一走 _ts_anchor: 解析失败 → None; 未来 >容差 → 夹 now
        + ts_anomaly + warning (sha 前 8 位)。两个字段任一无效时取另一个。
    """
    if meta.get("reconcile_anchor_fallback"):
        return None
    anchors: List[datetime] = []
    now = now or datetime.now(timezone.utc)
    sha = hashlib.sha256((entry or "").encode()).hexdigest() if entry else ""
    for field in ("written_at", "last_recall_hit_at"):
        raw = meta.get(field)
        if not raw:
            continue
        ts, anomalous = _ts_anchor_status(raw, now, field=field, sha=sha)
        # 未来/解析异常锚点不授予宽限 (F-3 硬口径); 其他判据仍可通过
        # _ts_anchor 拿到夹到 now 的值。另一字段有效时继续取另一字段。
        if ts is None or anomalous:
            continue
        anchors.append(ts)
    return max(anchors) if anchors else None


def _soft_residency_grace_ts(meta: Dict[str, Any],
                             now: datetime,
                             entry: str = "") -> Optional[datetime]:
    """FIX8 B1: 命中新鲜窗口 (rank 乘数输入) 则返回锚点时间戳, 否则 None。

    新鲜窗口 = written_at 或 last_recall_hit_at (经 _ts_anchor 归一化) 距
    now ≤ RULE_MIN_RESIDENCY_DAYS。窗口 <=0 → 整体关闭 (返回 None = 旧纯
    _rule_rank 行为)。本函数只表达"时间窗口内"; 它对 rank 的影响全部通过
    `_rule_rank` 的 ×GRACE_MULT 乘数实现, 不再有候选分档/资格/让位计数。
    `reconcile_anchor_fallback=True` = 明确不授予新鲜乘数 (非资格开关)。
    """
    days = _effective_soft_residency_days()
    if days <= 0:
        return None
    anchor = _soft_residency_anchor(meta, now=now, entry=entry)
    if anchor is None:
        return None
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if now - anchor <= timedelta(days=days):
        return anchor
    return None


def _in_soft_residency(meta: Dict[str, Any], now: datetime,
                       entry: str = "") -> bool:
    """FIX8: 条目当前是否处于新鲜窗口 (只读审计/测试辅助)."""
    return _soft_residency_grace_ts(meta, now, entry=entry) is not None


def _effective_evict_limit(max_evict: Optional[int]) -> int:
    """FIX7 I1: 本轮剩余可换出额度 (不把调用级上限做成每批上限)。

    None = 历史调用方, 默认 MAX_EVICT_PER_RUN; 显式值再夹到常量上限,
    防止调用方误传更大值破坏硬约束。
    """
    if max_evict is None:
        return MAX_EVICT_PER_RUN
    try:
        return max(0, min(int(max_evict), MAX_EVICT_PER_RUN))
    except (TypeError, ValueError):
        return 0


def _select_retirement_candidates_legacy(store, metastore, target: str,
                                          need_chars: int, stat: dict,
                                          *, max_evict: Optional[int] = None
                                          ) -> List[str]:
    """按 w_rank 升序选择退役候选 (Q2 三级年龄门槛, 2026-09-12, 回滚路径)。

    - Q4: protected 直接 continue (不进入候选池, 资格豁免; PROTECT_SKIP_LRU=0
      回滚时才参与 ×WEIGHT_PROTECT_MULT 排序)。
    - Q2 三级 min_age: idle 7 / warm 14 / active 30 (age 不足 → continue)。
      **词法活跃一票否决已删除** — 活跃只影响分级门槛与排序, 不影响资格。
    - type_override=state → S0 直迁路径处理, 不进 LRU 候选。
    - legacy 无元数据 → 不参与 (reconcile 先补章)。
    - tie-break: w_rank → last_active_at 早 → 字符长 → sha256 (幂等确定)。
    返回按挤出顺序排列的候选条目列表; 冷层失败由调用方 break。
    """
    now = datetime.now(timezone.utc)
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
        if m.get("type_override") == "state":
            continue  # 人工标注 state → S0 直迁, 不进 LRU 候选池
        # CACHE_POLICY_V2=0 为完整回滚: 无论 PROTECT_SKIP_LRU 默认是否已改 0,
        # 旧路径都恢复 protected 资格豁免 (本轮前预发布语义)。
        if (_cfg.PROTECT_SKIP_LRU or not _cache_policy_v2()) \
                and _is_protected_rule(e, m):
            continue  # Q4 legacy: protected 不参与自动沉 (资格豁免)
        if m.get("judge_decision") == "ambiguous":
            if m.get("judge_resolution") == "rule":
                # R-grace: judge_resolved_at 起 14d 内不进 LRU; 过期后按普通 rule.
                # FIX7 I2 明确例外: CACHE_POLICY_V2=0 回滚路径行为冻结,
                # 本行是生产代码中唯一保留的直接 _parse_iso; 禁止为了
                # "统一入口"改写 legacy 判定. v2 统一路径见
                # _select_retirement_candidates / _rule_activity_tier.
                # 对应测试: test_fix7_i2_legacy_direct_parse_documented_exception.
                _ra = (_parse_iso(str(m.get("judge_resolved_at")))
                       if m.get("judge_resolved_at") else None)
                if _ra is not None and (now - _ra) < timedelta(
                        days=JUDGE_RESOLVED_RULE_GRACE_DAYS):
                    continue
            else:
                # A0 不进入任何全文候选; A1 只可作为 stub 候选
                if not _ambiguous_a1_eligible(m, now, entry=e):
                    continue
                _age = entry_age_days(m, now=now, entry=e)
                wrank = _rule_rank(e, m, now, apply_grace=False)
                last = m.get("last_active_at") or m.get("written_at") or ""
                cands.append((wrank, last, -len(e),
                              hashlib.sha256(e.encode()).hexdigest(), e,
                              "ambiguous", JUDGE_AMBIGUOUS_LRU_DAYS))
                continue
        tier, min_age = _rule_activity_tier(m, e, _active_queries, now)
        age = entry_age_days(m, now=now, entry=e)
        if age is not None and age < min_age:
            continue  # 分级驻留期内不挤 (idle 7 / warm 14 / active 30)
        wrank = _rule_rank(e, m, now, apply_grace=False)
        last = m.get("last_active_at") or m.get("written_at") or ""
        cands.append((wrank, last, -len(e), hashlib.sha256(e.encode()).hexdigest(),
                      e, tier, min_age))
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    selected = []
    freed = 0
    _limit = _effective_evict_limit(max_evict)
    if _limit <= 0 or need_chars <= 0:
        return []
    for wrank, last, neglen, h, e, tier, min_age in cands:
        if freed >= need_chars:
            break
        if len(selected) >= _limit:
            break
        selected.append(e)
        freed += len(e)
    return selected


def _enforce_rule_budget_legacy(store, client, target: str, metastore,
                                stat: dict) -> None:
    """规则预算检查 + 挤权 (CACHE_POLICY_V2=0 回滚路径) (Phase 4 核心): rule+stub 字符超预算 → 退役最低权重规则。

    安全顺序: 冷层写成功才动本地 (stub-sink/retype 现有语义);
    冷层失败 → break (不丢数据, 下轮再试); 每轮 ≤ MAX_EVICT_PER_RUN。
    v2 (2026-09-12, DESIGN §Q4/Q6):
      - 预算 RULE_BUDGET_CHARS=2000 (与 TARGET_RATIO 40% 同源)。
      - 候选为完成态/kw-sink 时全文冷迁移 (释放全文), 普通 rule 才 stub。
      - 无候选且仍超预算 → stub GC (不再要求 ≥80% 水位) +
        budget_blocked_by_protected=true 告警上报, 不静默不空转。
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
        if (m and m.get("judge_decision") == "ambiguous"
                and m.get("judge_resolution") != "rule"
                and _ambiguous_a1_eligible(m, datetime.now(timezone.utc),
                                           entry=e)):
            # B-2 A1: ambiguous 只允许 stub-sink (全文先冷层 + 指针留热)
            try:
                _handle_rule_stub_sink(store, client, target, e, metastore,
                                       stat, anchor_parts)
            except Exception:
                stat["errors"] = stat.get("errors", 0) + 1
            released = len(e) - len(_make_stub(e))
        elif m and _rule_retype_eligible(e):
            # 完成态记录 → 全文冷迁移, 不留指针 (历史记录无需召回钩子)
            try:
                _handle_rule_retype(store, client, target, e, m,
                                    metastore, stat, anchor_parts)
            except Exception:
                stat["errors"] = stat.get("errors", 0) + 1
            released = len(e)
        elif not should_keep_local_rule_view(e):
            # v2 S1: kw 可沉型 → 全文冷迁移 (释放全文, 不留 stub)
            if target == "user" and classify_user_pref(
                    e, sentence_level=True) == "sink":
                anchor_parts.append(e)
            try:
                _handle_cold_migration(store, client, target, e, stat)
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
    # v2 (Q6): 仍超预算 → 无候选时 stub GC (冷层已有全文, 删指针无数据风险,
    # 不再要求 ≥80% 水位); 若剩余 rule 含 protected → 告警上报不静默。
    if rule_chars > RULE_BUDGET_CHARS:
        remaining = _select_retirement_candidates(
            store, metastore, target, rule_chars - RULE_BUDGET_CHARS, stat)
        if not remaining:
            # Q6/C-2: ambiguous_hold 平台期时 budget_blocked_by_protected 必须为
            # false (超预算的残余不是人工保护, 而是带期限的 A0)
            stat.setdefault("budget_blocked_by_protected", False)
            try:
                _stub_gc(store, metastore, target, stat, force=True)
            except Exception:
                pass
            if any(_is_protected_rule(e, metastore.get_entry(e) or {})
                   for e in store.entries(target)
                   if (metastore.get_entry(e) or {}).get("type") == "rule"):
                stat["budget_blocked_by_protected"] = True
                log.warning(
                    "RULE_BUDGET: %s 规则预算仍超 %d 且无未保护候选, "
                    "残余 protected 规则不自动沉 (人工处理或改标注)",
                    target, RULE_BUDGET_CHARS)
    # L7 (2026-08-26): flush 挤掉的 user 偏好进 [用户偏好摘要] 锚点 —
    # 否则 store_fact/direct_write 路径挤掉的偏好不进锚点 (仅 run_overflow 末尾 flush)
    if target == "user" and anchor_parts:
        try:
            _update_pref_anchor(client, store, anchor_parts, stat)
        except Exception:
            pass


def _select_retirement_candidates(store, metastore, target: str,
                                  need_chars: int, stat: dict,
                                  *, include_stubs: bool = True,
                                  max_evict: Optional[int] = None,
                                  exclude: Optional[Set[str]] = None
                                  ) -> List[str]:
    """统一退役候选池 (CACHE-POLICY-V2, FIX8 单一排序)。

    排序 = `_rule_rank` (w_eff × protected×3 × kw×0.5 × 新鲜×GRACE_MULT),
    升序 = 先出。无 protected skip / 年龄门 / ambiguous 资格 / type_override
    skip, 也没有宽限分档/fallback: 全部可换类型在同一个池里按同一个排序值
    参与 (FIX8 B2)。压力足够时任何乘数都不能阻止条目被选走。

    `exclude`: 只在本次选择中排除这些条目, 不改变其余条目相对顺序。P1 候选
    饥饿修复用 enforce 在冷失败后带 `exclude=已尝试集合` 重选下一顺位, 直到
    无候选/额度用尽/预算满足。
    `stat`: 保留历史调用方签名; FIX8 起本函数不再写 residency_deferred
    等分档计数.
    """
    if not _cache_policy_v2():
        return _select_retirement_candidates_legacy(
            store, metastore, target, need_chars, stat, max_evict=max_evict)
    _limit = _effective_evict_limit(max_evict)
    if _limit <= 0 or need_chars <= 0:
        return []
    now = datetime.now(timezone.utc)
    allowed = ({"rule", "state"} if not include_stubs
               else {"rule", "stub", "state"})
    excluded = set(exclude or ())
    cands: List[Tuple[Any, ...]] = []
    for e in store.entries(target):
        if e in excluded:
            continue
        m = metastore.get_entry(e)
        if not m:
            # legacy 尚未 reconcile (调用方应已先 reconcile); 仍按 WEIGHT_INIT
            # 初始先验参选, 不因缺章获得永久驻留。
            m = {"type": "rule", "weight": WEIGHT_INIT}
        typ = m.get("type")
        if typ not in ("rule", "state", "stub"):
            # P2 安全网: 历史/损坏 sidecar 的非法或 null type 不能让条目
            # 永久脱离候选池。stub 前缀仍按指针识别, 其余按 rule 内容处理。
            typ = "stub" if e.startswith(STUB_PREFIX) else "rule"
            m = dict(m)
            m["type"] = typ
        if typ not in allowed:
            continue
        wrank = _rule_rank(e, m, now)
        last = m.get("last_active_at") or m.get("written_at") or ""
        sha = _entry_sha(e)
        cands.append((wrank, last, -len(e), sha, e))
    cands.sort(key=lambda c: (c[0], c[1], c[2], c[3]))
    selected: List[str] = []
    freed = 0
    for _wrank, _last, _neglen, _sha, e in cands:
        if freed >= need_chars or len(selected) >= _limit:
            break
        selected.append(e)
        freed += len(e)
    return selected


def enforce_rule_budget(store, client, target: str, metastore,
                        stat: dict) -> None:
    """规则预算/硬容量统一换出 (CACHE-POLICY-V2 核心, F2 两阶段版)。

    - 计数拆分: content_chars = rule/state 全文; stub_chars = 指针文本。
      F2-③: 内容预算 `need` 只按 content_chars 计算; 指针总量只用于判断
      指针自身是否超 `_budget`, 绝不用"删指针"去补内容预算。
    - 阶段 1 (F2-①): 只在 rule/state 全文候选里按统一 `_rule_rank` 选,
      冷写成功才 replace 成 stub; 全文候选耗尽/冷层失败后才允许阶段 2。
    - F2/FIX8 P1 候选饥饿修复: 本 call 内维护 `attempted_stage1`，只要还有
      额度且预算未满足，就带 `exclude=attempted_stage1` 重选下一顺位；
      因此 need 小于单候选长度、头部候选永久冷失败时仍会尝试后续候选，直到
      无候选/额度用尽/预算满足。
    - 阶段 2: 仅当 stub_chars 自身超 _budget 时调用 _stub_gc(force=True);
      _stub_gc 内按 last_evicted_at/(retire_count==1) 保护本轮新建指针 (F2-②)。
    - FIX7 I1 / FIX8 P5: 批次循环共享调用级剩余额度, 单次调用实际全文换出
      (`lru_evicted`) ≤ MAX_EVICT_PER_RUN; `_stub_gc` 走独立 MAX_STUB_PER_RUN
      口径, 不计入该常量。
    - 冷层失败: errors++, 不删本地 (冷写成功才动本地铁律); 失败候选不阻塞
      仍有额度的后续候选。
    """
    if not _cfg.RULE_BUDGET_ENABLED or not _cfg.ACTIVITY_LOG_ENABLED:
        return
    if not _cache_policy_v2():
        return _enforce_rule_budget_legacy(store, client, target, metastore, stat)
    for _k in ("stubbed", "errors", "lru_evicted", "stub_gc",
               "protected_evicted", "pressure_batches", "cold_only"):
        stat.setdefault(_k, 0)
    _budget = _effective_rule_budget_chars()
    # F2-③: 指针自身预算独立按 config 常量核算; 测试常只 monkeypatch
    # 模块级 RULE_BUDGET_CHARS 压全文预算, 指针目录不应被同幅压缩
    # (验收: 19 条全换出时指针总量 749≤2000, 必须全部保留)。
    _stub_budget = int(getattr(_cfg, "RULE_BUDGET_CHARS", RULE_BUDGET_CHARS))
    anchor_parts: List[str] = []
    batches = 0

    def _snapshot() -> Tuple[List[str], List[str], int, int]:
        """返回 (content_entries, stub_entries, content_chars, stub_chars)。"""
        content_entries: List[str] = []
        stub_entries: List[str] = []
        content_chars = 0
        stub_chars = 0
        for _e in store.entries(target):
            _m = metastore.get_entry(_e) or {}
            _typ = _m.get("type")
            _is_stub = (_typ == "stub"
                        or (_typ is None and _e.startswith(STUB_PREFIX)))
            if _is_stub:
                stub_entries.append(_e)
                stub_chars += len(_e)
            else:
                content_entries.append(_e)
                content_chars += len(_e)
        return content_entries, stub_entries, content_chars, stub_chars

    call_evicted = 0  # FIX7 I1: 单次 enforce_rule_budget 调用级累计换出
    _call_cap = MAX_EVICT_PER_RUN
    for _batch in range(3):
        _content_entries, _stub_entries, content_chars, stub_chars = _snapshot()
        content_over = content_chars - _budget
        stub_over = stub_chars - _stub_budget
        hard = store.usage_pct(target) >= HARD_THRESHOLD * 100
        if content_over <= 0 and stub_over <= 0 and not hard:
            break
        if call_evicted >= _call_cap:
            # FIX7 I1: 调用上限已满, 不进入下一批, 也不再用剩余批次数
            # 重置额度; stub GC 同样不是全文换出, 但本调用到此为止。
            break
        batch_progress = False
        content_blocked = False

        # ---- 阶段 1: 统一候选池按 _rule_rank 选全文 ----
        if content_over > 0:
            attempted_stage1: Set[str] = set()
            stage1_progress = False
            while call_evicted < _call_cap and content_over > 0:
                _remaining = _call_cap - call_evicted
                candidates = _select_retirement_candidates(
                    store, metastore, target, max(content_over, 1), stat,
                    include_stubs=False, max_evict=_remaining,
                    exclude=attempted_stage1)
                if not candidates:
                    break
                tried_new = False
                for e in candidates:
                    if call_evicted >= _call_cap or content_over <= 0:
                        break
                    if e in attempted_stage1:
                        continue
                    attempted_stage1.add(e)
                    tried_new = True
                    if e not in store.entries(target):
                        continue
                    m = metastore.get_entry(e) or {}
                    if m.get("type") == "stub":
                        continue  # 双保险: 阶段 1 不得动指针
                    before_entries = store.entries(target)
                    protected = _is_protected_rule(e, m)
                    try:
                        _handle_rule_stub_sink(
                            store, client, target, e, metastore, stat,
                            anchor_parts,
                            force_cold_only=(_stub_budget <= 0))
                    except Exception:
                        stat["errors"] = stat.get("errors", 0) + 1
                    after_entries = store.entries(target)
                    if (after_entries != before_entries
                            and e not in after_entries):
                        # 冷层写成功才可能走到这里 (失败时本地原样保留)。
                        if protected:
                            stat["protected_evicted"] += 1
                        stat["lru_evicted"] += 1
                        call_evicted += 1
                        stage1_progress = True
                        batch_progress = True
                        # 重新核算 need, 再按新 need 选择下一顺位。
                        _, _, content_chars, stub_chars = _snapshot()
                        content_over = content_chars - _budget
                        stub_over = stub_chars - _stub_budget
                        break
                    # 冷层失败/本地未动: 继续本 call 内下一候选。
                if not tried_new:
                    break
            if content_over > 0:
                content_blocked = True
        else:
            content_blocked = True

        batches += 1
        if call_evicted >= _call_cap:
            # FIX7 I1: 达到单次调用上限立即结束外层批次循环; 不再在
            # 下一批重新获得 MAX_EVICT_PER_RUN 额度。
            break

        # ---- 阶段 2: 指针自身超预算才 GC; 内容仍超预算时禁止 ----
        # F2-①: 正常路径 content_over>0 时即使 stub_over>0 也不得先 GC 指针;
        # 只有全文候选耗尽或冷层失败 (content_blocked) 才放宽。
        _, _, content_chars, stub_chars = _snapshot()
        content_over = content_chars - _budget
        stub_over = stub_chars - _stub_budget
        if stub_over > 0 and (content_over <= 0 or content_blocked):
            before_gc = stat.get("stub_gc", 0)
            try:
                _stub_gc(store, metastore, target, stat, force=True)
            except Exception:
                pass
            if stat.get("stub_gc", 0) > before_gc:
                batch_progress = True
        if not batch_progress:
            break

    stat["pressure_batches"] = stat.get("pressure_batches", 0) + batches
    if _stub_budget <= 0:
        # FIX4 P1: 真 0 指针预算 = 显式 T3 cold-only 模式。允许本地不再保留
        # 指针 (evicted_no_ptr 会 >0); 冷层全文仍在, 但本模式不承诺本地
        # 句柄存在性。非 0 预算下此标志为 False, 存在性判据不放松。
        stat["t3_cold_only_mode"] = True
        stat.setdefault("budget_semantics",
                        "T3_cold_only_zero_pointer_budget")

    # 收尾审计: 内容仍超预算且候选池空 → protected 阻塞告警 (不静默)。
    _content_entries, _stub_entries, content_chars, _stub_chars = _snapshot()
    if content_chars > _budget:
        remaining = _select_retirement_candidates(
            store, metastore, target, content_chars - _budget, {},
            include_stubs=False)
        if not remaining:
            stat.setdefault("budget_blocked_by_protected", False)
            if any(_is_protected_rule(e, metastore.get_entry(e) or {})
                   for e in _content_entries):
                stat["budget_blocked_by_protected"] = True
                log.warning(
                    "RULE_BUDGET: %s 内容预算仍超 %d 且无全文候选, "
                    "残余含 protected 规则 (人工处理或改标注)",
                    target, _budget)
    if stat.get("protected_evicted", 0):
        log.warning(
            "RULE_BUDGET: %s 在预算/硬压力下换出 %d 条 protected 规则 "
            "(×%s 乘数仍非豁免; 全文已冷层可召回)",
            target, stat["protected_evicted"], WEIGHT_PROTECT_MULT)
    if target == "user" and anchor_parts:
        try:
            _update_pref_anchor(client, store, anchor_parts, stat)
        except Exception:
            pass


def restore_stubs_from_results(store, metastores: Dict[str, MetaStore],
                               results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """召回恢复 (Phase 4): recall 结果命中 stub 的 cold_id → 替换指针为全文。

    恢复全文写入 weight=WEIGHT_INIT+HIT_STRONG_INCREMENT=2.0, last_active_at=now,
    并累计 last_recall_hit_at/writeback_count; 恢复条目仍可被后续活性+预算换出
    (无任何硬驻留/7 天免疫)。返回未恢复的结果列表 (已恢复的从注入中剔除)。
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

                def _sdt(key: str):
                    # FIX7 I2: 写回恢复读 stub 旧时间戳也走统一锚点;
                    # 未来戳夹到 now + 全局 ts_anomaly 可见。
                    val = stub_meta.get(key)
                    if not val:
                        return None
                    return _ts_anchor(val, now=now, field=key,
                                      sha=_entry_sha(stub))

                _restore_kw: Dict[str, Any] = {}
                if stub_meta.get("judge_decision"):
                    # C-2: stub 恢复保留 judge_decision/review_count,
                    # 避免恢复后立即被同一判型重新丢回模糊带 (再 stub 由
                    # 权重/下一轮 LRU 兜底, 不在此清零审计字段)。
                    _restore_kw = {
                        "judge_decision": stub_meta.get("judge_decision"),
                        "judge_band": stub_meta.get("judge_band"),
                        "judge_confidence": stub_meta.get("judge_confidence"),
                        "judge_signals": stub_meta.get("judge_signals"),
                        "judge_reason": stub_meta.get("judge_reason"),
                        "judge_review_count": int(
                            stub_meta.get("judge_review_count") or 0),
                        "judge_policy": stub_meta.get("judge_policy", "v3"),
                        "judge_resolution": "stub_restored",
                    }
                metastores[target].stamp(
                    content, "rule", origin="stub_restore",
                    weight=WEIGHT_INIT + HIT_STRONG_INCREMENT,
                    last_active_at=now,
                    importance=orig_imp,
                    cold_id=stub_meta.get("cold_id"),
                    type_override=stub_meta.get("type_override"),
                    type_source="stub_restore",
                    protect_override=stub_meta.get("protect_override"),
                    protected=(stub_meta.get("protected")
                               if stub_meta.get("protected") is not None
                               else None),
                    last_strong_hit_at=_sdt("last_strong_hit_at"),
                    last_weak_hit_at=_sdt("last_weak_hit_at"),
                    last_recall_hit_at=now,
                    last_injected_at=_sdt("last_injected_at"),
                    last_evicted_at=_sdt("last_evicted_at"),
                    retire_count=int(stub_meta.get("retire_count") or 0),
                    writeback_count=int(stub_meta.get("writeback_count") or 0) + 1,
                    reconcile_anchor_fallback=False,
                    **_restore_kw,
                )
                restored_ids.add(rid)
        except Exception:
            continue
    if restored_ids:
        return [r for r in results if r.get("id") not in restored_ids]
    return results

# ---- E8: env 开关惰性委托 (2026-09-12) --------------------------------------
def __getattr__(name):
    if name in ("ACTIVITY_LOG_ENABLED", "RULE_BUDGET_ENABLED",
                "HIT_WEAK_MODE", "EMBED_BACKEND"):
        return getattr(_cfg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
