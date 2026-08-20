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
import math
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

from .classifier import classify, classify_user_pref, should_keep_local, HOT, COLD, STALE
from .config import (
    STATE_TTL_DAYS, RULE_COMPRESS_DAYS,
    SOFT_THRESHOLD, HARD_THRESHOLD, TARGET_RATIO,
    RULE_RETYPE_DAYS, RULE_RETYPE_MIN_DONE_MARKERS,
    RULE_RETYPE_DONE_MARKERS, RULE_RETYPE_BEHAVIOR_MARKERS,
    RULE_STUB_IDLE_DAYS, IMPORTANCE_PROTECT, MAX_STUB_PER_RUN,
    STUB_MAX_CHARS, STUB_PREFIX, STUB_GC_MIN_AGE_DAYS,
    ACTIVITY_LOG_ENABLED, CROSS_DEDUP_MIN_IDLE_DAYS,
    CLUSTER_EMBED_THRESHOLD, MEMORYCORE_EMBED_URL, MEMORYCORE_EMBED_MODEL,
)
from .metadata import (MetaStore, entry_age_days, parse_embedded_date,
                       _parse_iso, load_recent_queries)

# ---- 相似度阈值 ------------------------------------------------------------

_SAME_FACT_RATIO = 0.85       # 内容几乎相同 → 同一事实
_SIMILAR_TOPIC_RATIO = 0.50   # 同主题但细节不同 → 需 merge
_RECALL_SCORE_SAME = 0.80     # recall dense_score >= 此值视为高度匹配
_RECALL_SCORE_SIMILAR = 0.48  # recall dense_score >= 此值视为同主题
_RECALL_TOP_K = 3             # 查重时的召回数
_MIN_TEXT_RATIO = 0.15        # 最低字面相似度门禁 (防向量误匹配)

# ---- 反转检测 (2026-08-05: 写入侧反转覆盖) -------------------------------

_REVERSAL_NEG_WORDS = ["不", "没", "无", "非", "否", "别", "不再",
                       "停止", "取消", "不要", "拒绝", "禁", "讨厌",
                       "反对", "不喜", "不爱", "不感兴趣"]
_REVERSAL_WEAK_NEG = ["不太", "不大", "不怎么", "未必", "难免", "不常", "不同", "不仅", "不过", "不可", "不必", "不止", "不再"]


def _topic_overlap(a: str, b: str) -> bool:
    """判定两条是否同主题 (与 maintenance _is_reversal_pair 判定一致)。"""
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
# ---- 用户偏好摘要锚点 (P4) --------------------------------------------------

_ANCHOR_PREFIX = "[用户偏好摘要]"   # 内容前缀: 识别/查重/召回锚点
_ANCHOR_IMPORTANCE = 0.8        # 高 importance → prefetch 分层注入高档
_ANCHOR_MAX_CHARS = 800         # 锚点长度上限 (超限截断保留头部)
_ANCHOR_QUERY = _ANCHOR_PREFIX  # 从 _ANCHOR_PREFIX 派生 (原硬编码 "[用户偏好摘要]")



# ---- Phase 3: rule invalidation signals (2026-08-20) ----------------------
# Tiered protection: B-class rules may retire by layered evidence
# (merge / compress / sink / dedup); A-class meta-rules, red-line rules and
# high-importance entries are never touched (only merge/compress).

# S6 protection line: A-class meta-rules (apply every turn; topic-activity
# signals are meaningless for them) — same source as classifier
# strong_keep_markers / interact_words (word list is tunable).
_RULE_META_MARKERS = [
    "行为准则", "交互习惯", "写作风格", "回答风格", "措辞", "汇报", "沟通",
    "大白话", "结论先行", "验证", "准确", "严谨", "覆盖", "抑郁", "信任",
    "尊重", "纠正", "红线", "零容忍",
]
# S6 red-line class (hard-veto words, A or B class alike) — absolute protection
_RULE_REDLINE_MARKERS = ["红线", "零容忍", "绝不", "禁止", "纠正"]

# S4: per-run cap on candidates evaluated for dormancy (LLM call guardrail)
_STUB_EVAL_CAP = 10


def _is_protected_rule(entry: str, meta: dict) -> bool:
    """S6: A-class / red-line / high importance -> absolute protection
    (never stub / never retype / never cross-tier dedup; merge+compress only)."""
    if meta.get("importance", 0.8) >= IMPORTANCE_PROTECT:
        return True
    if any(kw in entry for kw in _RULE_META_MARKERS):
        return True
    if any(kw in entry for kw in _RULE_REDLINE_MARKERS):
        return True
    return False


def _rule_retype_eligible(entry: str) -> bool:
    """S2: completion re-check eligibility — embedded date >= 60d + >= 2
    completion markers + zero behavior-directive words."""
    d = parse_embedded_date(entry)
    if d is None:
        return False
    days = (datetime.now(timezone.utc) - d).days
    if days < RULE_RETYPE_DAYS:
        return False
    hits = [m for m in RULE_RETYPE_DONE_MARKERS if m in entry]
    # nested dedup ("退役" ⊂ "已退役" counts once)
    distinct = [m for m in hits
                if not any(o != m and m in o for o in hits)]
    if len(distinct) < RULE_RETYPE_MIN_DONE_MARKERS:
        return False
    if any(m in entry for m in RULE_RETYPE_BEHAVIOR_MARKERS):
        return False
    return True


def _try_cross_layer_dedup(store, client, target: str, entry: str,
                           stat: dict) -> bool:
    """S5 (L1): the cold tier already holds an equivalent copy (same-level
    match) -> drop the hot copy (zero information loss)."""
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
    """S4: stub pointer (<= STUB_MAX_CHARS, lexical, zero LLM dependency).

    Format: [规则指针]{topic<=10 chars}→recall("{topic}") — pointer + recall hook.
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
    """S4: LLM dormancy judge (<= 5 entries per batch, config LLM channel).

    Returns {entry: dormant}; failures / unparseable output omit the entry
    (caller treats it as active). Prompt hard constraints: uncertain = active;
    judge topic recurrence only, never value.
    """
    from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT

    if not LLM_API_KEY or not entries:
        return {}
    qs = "\n".join(f"- {q[:120]}" for q in queries[-60:])[:1500]
    out: Dict[str, bool] = {}
    import json
    import urllib.request
    for i in range(0, len(entries), 5):
        chunk = entries[i:i + 5]
        items = "\n".join(
            f'<entry index="{j}">{e[:300]}</entry>'
            for j, e in enumerate(chunk))
        prompt = (
            "你是记忆治理助手。判断下列记忆准则涉及的主题, 近期是否被用户讨论过。\n"
            f"<queries>recent user messages (last 30 days):\n{qs}\n</queries>\n"
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
                "model": LLM_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 800,
                "response_format": {"type": "json_object"},
            }
            req = urllib.request.Request(
                LLM_BASE_URL.rstrip("/") + "/chat/completions",
                data=json.dumps(payload).encode(),
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {LLM_API_KEY}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
                data = json.loads(resp.read().decode())
            content = data["choices"][0]["message"]["content"].strip()
            content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
            results = json.loads(content).get("results", [])
            for r in results:
                idx = r.get("entry_index")
                d = r.get("dormant")
                if (isinstance(idx, int) and 0 <= idx < len(chunk)
                        and isinstance(d, bool)):
                    out[chunk[idx]] = d
        except Exception:
            continue  # this batch counts as active
    return out


def _rule_topic_dormant(entry: str, queries: List[str]) -> bool:
    """S4 dormancy evidence chain: lexically active -> False; lexically
    inactive -> LLM confirm; any failure -> False (active).

    Dormancy is a negative claim: insufficient evidence always means active
    — better to keep than to mis-sink.
    """
    if not queries:
        return False
    for q in queries:
        if _topic_overlap(q, entry) or _topic_overlap(entry, q):
            return False  # lexically active (zero-cost fast screen)
    res = _llm_judge_dormant([entry], queries)
    return bool(res.get(entry, False))


def _plan_stub_candidates(metastore, client, entries: List[str]) -> List[str]:
    """L2 pre-planning: B-class + idle >= RULE_STUB_IDLE_DAYS + dormancy
    confirmed -> stub candidates (<= MAX_STUB_PER_RUN).

    Ordering: longest idle first; ties broken by length (chars saved per stub).
    """
    if not ACTIVITY_LOG_ENABLED:
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
    """S2 (L1): completion re-check — restamp as state
    (origin=retype_overflow) then retire via the normal TTL path.

    Safe order: stamp -> cold migration (local removed only after cold
    confirmed). On cold failure the original rule stamp (timestamps /
    importance / origin) is restored and the entry stays.
    """
    d = parse_embedded_date(entry)
    try:
        metastore.stamp(entry, "state", written_at=d, origin="retype_overflow")
    except Exception:
        pass  # F1 semantics: stamp failure never blocks (reconcile backstop)
    before = len(store.entries(target))
    if target == "user" and classify_user_pref(
            entry, sentence_level=True) == "sink":
        anchor_parts.append(entry)
    _handle_cold_migration(store, client, target, entry, stat)
    if len(store.entries(target)) < before:
        stat["aged_sunk"] += 1
        stat["retyped"] += 1
        return
    # rollback: restore the original rule stamp
    try:
        metastore.stamp(
            entry, "rule",
            written_at=(_parse_iso(str(meta["written_at"]))
                        if meta.get("written_at") else None),
            updated_at=(_parse_iso(str(meta["updated_at"]))
                        if meta.get("updated_at") else None),
            importance=meta.get("importance", 0.8),
            origin=meta.get("origin", "legacy"),
        )
    except Exception:
        pass


def _handle_rule_stub_sink(store, client, target: str, entry: str,
                           metastore, stat: dict,
                           anchor_parts: List[str]) -> None:
    """S4 (L2): dormant B-class rule -> full text written to the cold tier
    first; only after cold confirms stored, the local entry is replaced by a
    stub pointer. Any failure keeps the original entry untouched (errors+1).
    """
    if entry not in store.entries(target):
        return  # already handled by another action this run (S2/S5)
    try:
        r = client.remember(entry, importance=0.6, scope="global")
        if r.get("status") != "stored":
            stat["errors"] += 1
            return
    except Exception:
        stat["errors"] += 1
        return
    stub = _make_stub(entry)
    if store.replace(target, entry, stub).get("success"):
        try:
            metastore.stamp(stub, "stub", origin="stub_sink")
        except Exception:
            pass  # stamp failure re-covered by reconcile (STUB_PREFIX)
        stat["stubbed"] += 1
        if target == "user":
            anchor_parts.append(entry)  # full pref text joins the anchor
    else:
        stat["errors"] += 1


def _stub_gc(store, metastore, target: str, stat: dict) -> None:
    """Stub lifecycle GC — oldest-first pointer removal, <=
    MAX_STUB_PER_RUN per overflow run.

    Pointers only: zero cold-tier calls (full texts stay in cold, forget is
    never called). Pointers younger than STUB_GC_MIN_AGE_DAYS are kept
    (prevents create-then-collect thrash).
    """
    entries = store.entries(target)
    stubs = []
    for e in entries:
        m = metastore.get_entry(e)
        if m and m.get("type") == "stub":
            age = entry_age_days(m)
            stubs.append((age if age is not None else 9999, e))
    stubs.sort(key=lambda t: -t[0])  # oldest first
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


# S3 embedding channel (optional enhancement; unavailable -> lexical-only,
# overflow never blocks on it)
def _embed_batch(texts: List[str]) -> Optional[Dict[str, List[float]]]:
    """Batch embeddings via the OpenAI-compatible endpoint configured in
    config (MEMORYCORE_EMBED_URL, default ollama /v1); any failure -> None
    (degrade to lexical-only)."""
    if not texts:
        return None
    try:
        import json
        import urllib.request
        req = urllib.request.Request(
            MEMORYCORE_EMBED_URL.rstrip("/") + "/embeddings",
            data=json.dumps({"model": MEMORYCORE_EMBED_MODEL,
                             "input": texts}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        items = data.get("data") or []
        if len(items) != len(texts):
            return None
        return {t: list(it.get("embedding") or [])
                for t, it in zip(texts, items)}
    except Exception:
        return None


def _cosine(a: List[float], b: List[float]) -> float:
    """Cosine similarity between two vectors."""
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
        "aged_sunk": 0,          # Phase 2: state entries retired by age
        "metadata_stamped": 0,   # Phase 2: legacy entries stamped by reconcile
        "stubbed": 0,            # Phase 3 S4: dormant B rules -> stub pointers
        "stub_gc": 0,            # Phase 3: stub pointers collected (oldest first)
        "retyped": 0,            # Phase 3 S2: rule -> state completion re-checks
    }

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
    merged_entries, merge_count = _merge_local_fragments(entries)
    if merge_count > 0:
        _rebuild_file(store, target, merged_entries)
        stat["merged"] = merge_count
        entries = merged_entries

    # ---- Step 0.5 (Phase 2): metadata reconcile — legacy stamping / GC ----
    # Runs after split/merge so their products are covered too. Idempotent;
    # writes only the sidecar, never the .md files.
    metastore = MetaStore(target, memory_path=store.memory_path,
                          user_path=store.user_path)
    try:
        meta_stat = metastore.reconcile(entries)
        stat["metadata_stamped"] = meta_stat.get("stamped", 0)
    except Exception:
        # F1 (final review): a sidecar failure (disk full / lock error) must
        # never block the overflow — degrade to the legacy keyword path.
        stat["errors"] += 1
        stat["metadata_stamped"] = 0

    # ---- L2 pre-planning (Phase 3): dormant B-class rules under hard
    # pressure -> stub candidates (evidence chain + LLM dormancy judge) ----
    stub_candidates: Set[str] = set()
    if store.usage_pct(target) >= HARD_THRESHOLD * 100:
        try:
            stub_candidates = set(_plan_stub_candidates(
                metastore, client, entries))
        except Exception:
            stub_candidates = set()  # planning failure -> no stubbing (degrade)

    # ---- Step 2+3+5: 逐条处理 --------------------------------------------
    for entry in entries:
        # Phase 2 (2026-08-16): metadata-first — typed entries retire by
        # age+type (keywords play no part); untyped entries fall back to the
        # legacy keyword path.
        meta = metastore.get_entry(entry)
        if meta and _handle_typed_entry(store, client, target, entry, meta,
                                        metastore, stat, anchor_parts,
                                        stub_candidates):
            continue

        if should_keep_local(entry):
            # B: 长条目压缩优先 (2026-08-07) — keep 但 >200 字:
            # 先尝试 LLM 压缩成精简版留本地, 原始细节沉冷层; 失败保留原样。
            if len(entry) > _COMPRESS_MIN_CHARS:
                compressed = _llm_compress(client, entry)
                if compressed is not None and compressed != entry:
                    # 校验 2: 压缩确实更短 (省字目标)
                    if len(compressed) < len(entry) * 0.8:
                        # 先沉原始细节到冷层 (原子性: 失败则保留本地原样)
                        try:
                            r = client.remember(entry, importance=0.6, scope="global")
                            if r.get("status") == "stored":
                                # F3 (final review): only count after a
                                # successful local replace
                                if store.replace(target, entry, compressed).get("success"):
                                    stat["compressed"] += 1
                                    anchor_parts.append(entry)
                                    continue
                                stat["errors"] += 1
                                stat["kept"] += 1
                                continue
                        except Exception:
                            pass
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

    # ---- Step 5.5 (Phase 3): stub GC — no stubs created this run and still
    # under hard pressure -> collect the oldest pointers (opportunistic) ----
    if (stat.get("stubbed", 0) == 0
            and store.usage_pct(target) >= HARD_THRESHOLD * 100):
        try:
            _stub_gc(store, metastore, target, stat)
        except Exception:
            pass  # GC is opportunistic; never blocks overflow

    # ---- Step 6: 验证 ----------------------------------------------------
    if target == "user" and anchor_parts:
        _update_pref_anchor(client, store, anchor_parts, stat)
    stat["chars_after"] = store.char_count(target)
    stat["usage_after"] = f"{store.usage_pct(target)}%"
    stat["target"] = target
    return stat




# ---- Phase 2: typed-entry retirement decision (2026-08-16) -----------------

def _handle_typed_entry(store, client, target: str, entry: str,
                        meta: dict, metastore, stat: dict,
                        anchor_parts: List[str],
                        stub_candidates: Optional[Set[str]] = None) -> bool:
    """Retirement decision for entries that have sidecar metadata.

    - stub: pointer stays forever (GC via Step 5.5; never enters
      retirement paths).
    - state: age >= STATE_TTL_DAYS -> cold migration via the safe path
      (cold write confirmed before local removal); not expired -> keep.
    - rule (Phase 3 tiered protection, 2026-08-20):
        * S6: A-class / red-line / importance >= 0.9 -> long-compression
          only; never stub / retype / cross-tier dedup
        * S2 (L1 >=60%): completion re-check -> retype state -> TTL sink
        * S5 (L1 >=60%, idle >=30d): cold tier holds an equivalent copy ->
          drop the hot copy
        * S4 (L2 >=80%): dormancy confirmed -> full text to cold + stub
          pointer left locally
        * existing: >= RULE_COMPRESS_DAYS without update and long -> LLM
          compression (any failure keeps the original)
    - unknown type -> return False, caller falls back to legacy keywords.

    Returns: True = handled (caller continues); False = fall back to legacy.
    """
    etype = meta.get("type")
    age = entry_age_days(meta)

    if etype == "stub":
        stat["kept"] += 1  # pointer stays; GC via Step 5.5 under hard pressure
        return True

    if etype == "state":
        if age is not None and age >= STATE_TTL_DAYS:
            before = len(store.entries(target))
            if target == "user" and classify_user_pref(
                    entry, sentence_level=True) == "sink":
                anchor_parts.append(entry)
            _handle_cold_migration(store, client, target, entry, stat)
            if len(store.entries(target)) < before:
                stat["aged_sunk"] += 1  # count only when it really left the hot tier
            return True
        stat["kept"] += 1  # not expired yet
        return True

    if etype == "rule":
        protected = _is_protected_rule(entry, meta)
        usage = store.usage_pct(target)

        # S2 (L1): completion re-check -> retype -> TTL sink (cold failure
        # restores the original rule stamp)
        if (not protected and usage >= SOFT_THRESHOLD * 100
                and _rule_retype_eligible(entry)):
            _handle_rule_retype(store, client, target, entry, meta,
                                metastore, stat, anchor_parts)
            return True

        # S5 (L1): cross-tier redundancy — only probed after
        # CROSS_DEDUP_MIN_IDLE_DAYS idle (historical-redundancy oriented,
        # keeps recall overhead low; cold copy confirmed -> drop hot, zero loss)
        if (not protected and usage >= SOFT_THRESHOLD * 100
                and age is not None and age >= CROSS_DEDUP_MIN_IDLE_DAYS
                and _try_cross_layer_dedup(store, client, target, entry, stat)):
            return True

        # S4 (L2): dormant stub-sink (candidates pre-planned in run_overflow)
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
                try:
                    r = client.remember(entry, importance=0.6, scope="global")
                    if r.get("status") == "stored":
                        # F3 (final review): verify replace before counting
                        if store.replace(target, entry, compressed).get("success"):
                            try:
                                metastore.stamp(compressed, "rule",
                                                origin="overflow")
                            except Exception:
                                pass  # F1: stamp failure is non-fatal (reconcile re-stamps)
                            stat["compressed"] += 1
                            anchor_parts.append(entry)
                            return True
                        stat["errors"] += 1
                        stat["kept"] += 1
                        return True
                except Exception:
                    pass
                stat["errors"] += 1
                stat["kept"] += 1
                return True
        stat["kept"] += 1
        return True

    return False  # unknown type -> legacy fallback


# ---- P4: 用户偏好摘要锚点 --------------------------------------------------

def _update_pref_anchor(client, store, new_parts: List[str], stat: dict) -> None:
    """维护冷层用户偏好摘要锚点。

    用户偏好内容下沉时, 不散落丢失, 合并成一条高 importance 锚点,
    作为 prefetch 召回的用户偏好画像总索引。

    规则拼接 + 增量更新 (自包含零依赖, 关键词覆盖优先):
      - 首次建立: 并入 USER.md 当前 core 偏好 → 初始即完整画像
      - 后续溢流: 合入本次下沉的偏好内容 (句级去重)
      - 已有锚点用 recall 前缀查询定位 (避开词法盲区)
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
        for m in client.recall_results(_ANCHOR_QUERY, top_k=5):
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
        except Exception:
            pass
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
                # 反转检测 (双向化): 本地条或冷层旧条任一侧含否定词 + 同主题 → 覆盖
                # 场景: 旧"不喜欢A" → 新"喜欢A" (新条无否定词, 旧条有) 同样是反转
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
                    # 检查冷层旧条: 旧条否定+新条正向 → 正向替代
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
                        # 写入侧反转覆盖前, 旧条进回收站
                        from ..trash_store import TrashStore
                        TrashStore().add(
                            matched["id"], cold_content,
                            reason="reversal_obsolete",
                            source_decision="write_side_reversal")
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
                    return  # update 失败 → 本地保留
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
    """过时条目: 尝试 forget 冷层匹配, 然后删本地。"""
    try:
        existing = _recall_safe(client, entry)
        if existing:
            for ex in existing:
                ratio = difflib.SequenceMatcher(
                    None, entry, ex.get("content", "")
                ).ratio()
                if ratio > _SAME_FACT_RATIO:
                    try:
                        client.forget(ex["id"])
                    except Exception:
                        pass  # forget 失败不阻塞
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


def _merge_local_fragments(entries: List[str]) -> Tuple[List[str], int]:
    """检测本地同主题碎片并合并。返回 (merged, merge_count)。

    Phase 3 (2026-08-20): stub pointers ([规则指针] prefix) never merge —
    they share a fixed format prefix, so lexical similarity is naturally
    >= 0.5 and merging would silently drop pointers. They are opaque
    retrieval hooks, not prose: merging has zero benefit.
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

    # Phase 3 S3: optional embedding channel (complements lexical; when the
    # embedding API is down this degrades to lexical-only)
    mergeable = [e for i, e in enumerate(entries) if i not in skip]
    emb_map = _embed_batch(mergeable) if len(mergeable) >= 2 else None
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
    删除会导致同义句 "D 盘=/srv/data (Code Drive...)" 与
    "D 盘=Code Drive (path=/srv/data...)" 规范化后反而不同。
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
    from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT

    if not LLM_API_KEY:
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
        import urllib.request

        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 2000,
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            LLM_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        text = json.loads(content).get("merged", "").strip()
    except Exception:
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

    边界 (与 _llm_merge 同款):
    - 只做\"压缩\", 不自由发挥 (prompt 硬约束: 保留路径/数字/日期/专有名词,
      不添加/不推断/不修改事实);
    - 失败路径 (无 key / 网络错误 / 超时 / 解析失败 / 信息保留校验不过 /
      压缩不省字) 一律返回 None, 由调用方保留原条目, 溢流永不阻塞。
    """
    from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT

    if not LLM_API_KEY:
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
        import urllib.request

        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.2,
            "max_tokens": 3000,
            # 压缩是纯提取任务, 禁用推理链 (v4-flash 推理会吃光 token 导致空输出)
            "thinking": {"type": "disabled"},
            "response_format": {"type": "json_object"},
        }
        req = urllib.request.Request(
            LLM_BASE_URL.rstrip("/") + "/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {LLM_API_KEY}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            data = json.loads(resp.read().decode())
        content = data["choices"][0]["message"]["content"].strip()
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        text = json.loads(content).get("compressed", "").strip()
    except Exception:
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
    # 长条目 (>250字) 用首尾拼接 (前150+后100), 保留首部语义 + 尾部关键信息
    if len(entry) > 250:
        query = entry[:150] + " " + entry[-100:]
    else:
        query = entry[:200]
    return client.recall_results(query, top_k=_RECALL_TOP_K)


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
    base_sentences = set(re.split(r"[。！？;；\n]", base))

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
