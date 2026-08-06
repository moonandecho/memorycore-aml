#!/usr/bin/env python3
"""core/maintenance.py — 冷层全量治理 v3 (Round 2: 向量预筛 + 回收站)

对冷层 (cold tier) 执行定期巡检:
  1. 全量枚举 (多路 recall + 邻居扩展, 覆盖率 ≥95%)
  2. 向量预筛去重 (sqlite-vec 邻居 → 文本判定, O(n×recall))
  3. 过时事实清理 (STALE_MARKERS + 长短分级 + LLM + 回收站)
  4. 冲突事实取舍 (同主题新旧冲突 → 保留最新)
  5. 向量完整性校验 (total == embeddings)
"""
from __future__ import annotations

import difflib
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from .classifier import STALE, classify
from .overflow import _norm_sentence, _bigram_coverage

# ---- 阈值 ----------------------------------------------------------------

_SIM_THRESHOLD = 0.75       # 字面相似度 ≥ 此值视为重复, 直接合并
_FUZZY_LOW = 0.40           # 模糊组下限: 低于此值不算候选
_CONFLICT_THRESHOLD = 0.55  # 同主题但内容差异大 → 可能冲突
_RECALL_BREADTH = 10        # 多路召回时的 top_k
_RECALL_NEIGHBOR = 5        # 邻居扩展 top_k
_COVERAGE_TARGET = 0.95     # 枚举覆盖率目标
_VEC_NEIGHBOR_K = 10        # 向量预筛: 每条查 top_k 邻居
_VEC_SCORE_THRESHOLD = 0.5  # 向量预筛: dense_score 下限 (低于此值不配对)

# ---- 反转检测 (2026-08-05 用户拍板: 反转冲突以最新为准) --------------------

_REVERSAL_NEG_WORDS = ["不", "没", "无", "非", "否", "别", "不再",
                       "停止", "取消", "不要", "拒绝", "禁", "讨厌",
                       "反对", "不喜", "不爱", "不感兴趣"]
_REVERSAL_WEAK_NEG = ["不太", "不大", "不怎么", "未必", "难免", "不常", "不同", "不仅", "不过", "不可", "不必", "不止", "不再"]
_REVERSAL_OVERLAP = 0.55   # 字面重叠阈值 (bigram 覆盖率)
_REVERSAL_MIN_RATIO = 0.40 # 最小字面相似度 (同主题门槛)

_RECALL_QUERIES = [         # 多路召回种子查询 (覆盖常见主题空间)
    "偏好 习惯 准则",
    "服务器 网络 IP 配置",
    "项目 技术栈 框架",
    "bug 错误 问题",
    "API 接口 端点",
    "用户 行为 风格",
    "部署 环境 版本",
    "规则 禁止 要求",
    "已完成 已修复 已废弃",
    "数据 存储 数据库",
]


# ---- 反转判定 --------------------------------------------------------------

def _is_reversal_pair(older, newer):
    """判定两条是否构成反转: 新条否定旧条且同主题。

    三条件 AND:
      1. 新条含否定词 (_REVERSAL_NEG_WORDS 白名单)
      2. 排除弱化否定 (不太/不大/不怎么/未必/难免)
      3. 同主题: 字面重叠 (bigram 覆盖率 >= 0.55 或 ratio >= 0.40)
      4. 时间先后: newer.timestamp > older.timestamp (缺失时视为不可判 -> False)
    保守: 任一条件不满足 -> False (宁留不误删)。
    """
    n_ts = newer.get("timestamp") or ""
    o_ts = older.get("timestamp") or ""
    if not n_ts or not o_ts or n_ts <= o_ts:
        return False
    c_new = newer.get("content") or ""
    c_old = older.get("content") or ""
    if not c_new or not c_old:
        return False

    # 检查否定词: 任一侧 (newer 或 older) 命中白名单否定词即可 (P1-3 双向化)
    # 场景: 旧"不喜欢A" → 新"喜欢A" (新条无否定词, 旧条有) 同样是反转
    has_neg = False
    for w in _REVERSAL_NEG_WORDS:
        # 检查新条
        if w in c_new:
            is_weak = False
            for wn in _REVERSAL_WEAK_NEG:
                if wn in c_new and w in wn:
                    is_weak = True
                    break
            if not is_weak:
                has_neg = True
                break
        # 检查旧条 (P1-3: 旧条有否定词 + 新条同主题 → 正向替代也是反转)
        if w in c_old:
            is_weak = False
            for wn in _REVERSAL_WEAK_NEG:
                if wn in c_old and w in wn:
                    is_weak = True
                    break
            if not is_weak:
                has_neg = True
                break
    if not has_neg:
        return False

    # 同主题判定: 复用 bigram 覆盖率 + SequenceMatcher (与去重一致)
    nn = _norm_sentence(c_new)
    no = _norm_sentence(c_old)
    ratio = difflib.SequenceMatcher(None, nn, no).ratio()
    if ratio >= _REVERSAL_MIN_RATIO:
        return True
    bg_new = set()
    for i in range(len(nn) - 1):
        bg_new.add(nn[i:i + 2])
    if _bigram_coverage(no, bg_new) >= _REVERSAL_OVERLAP:
        return True
    return False




def _topic_overlap_with_ts(entry_a, entry_b) -> bool:
    """判定两条是否同主题 + 时间可判 (用于收集 LLM 模糊反转候选)。
    
    不检查否定词 — 仅检查话题重叠 + 时间序。
    """
    ts_a = entry_a.get("timestamp") or ""
    ts_b = entry_b.get("timestamp") or ""
    if not ts_a or not ts_b:
        return False
    c_a = entry_a.get("content") or ""
    c_b = entry_b.get("content") or ""
    if not c_a or not c_b:
        return False
    na = _norm_sentence(c_a)
    nb = _norm_sentence(c_b)
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    if ratio >= _REVERSAL_MIN_RATIO:
        return True
    bg_b = {nb[i:i + 2] for i in range(len(nb) - 1)}
    if _bigram_coverage(na, bg_b) >= _REVERSAL_OVERLAP:
        return True
    return False




# ---- 主入口 ----------------------------------------------------------------

def run_maintenance(client) -> dict:
    """冷层全量治理 v3 (向量预筛 + 回收站)。

    Returns:
        dict: {scanned, merged, cleaned, conflicts_resolved,
               reversals_resolved, vector_ok, total, embeddings, errors,
               pending_stale, trash_cleared, trash_revived}
    """
    stat: Dict[str, Any] = {
        "scanned": 0,
        "merged": 0,
        "cleaned": 0,
        "conflicts_resolved": 0,
        "reversals_resolved": 0,  # 反转冲突消解数 (去重 + 冲突 + LLM)
        "candidates_processed": 0,  # Fix2: LLM 反转候选对处理数 (reversal 命中 + fallback 合并)
        "vector_ok": None,
        "total": 0,
        "embeddings": 0,
        "errors": 0,
        "pending_stale": 0,    # 回收站当前条目
        "trash_cleared": 0,    # 本次到期清空
        "trash_revived": 0,    # 本次恢复 (被召回命中)
        "forgotten": 0,        # 冷层降权遗忘数
    }

    # ---- Step 0: 回收站巡检 (召回恢复 + 到期清空) ----------------------
    trash_revived, trash_cleared = _trash_cycle(client)
    stat["trash_revived"] = trash_revived
    stat["trash_cleared"] = trash_cleared

    from ..trash_store import TrashStore
    trash = TrashStore()
    stat["pending_stale"] = trash.count()

    # ---- Step 1: 全量枚举 (B1: 邻居扩展升级) ----------------------------
    try:
        stat["total"] = client.stats().get("total", 0)
    except Exception:
        stat["total"] = 0

    all_entries = _enumerate_all(client, stat["total"])
    stat["scanned"] = len(all_entries)

    if not all_entries:
        try:
            s = client.stats()
            stat["total"] = s.get("total", 0)
            stat["embeddings"] = s.get("embeddings", 0)
            stat["vector_ok"] = (stat["total"] == stat["embeddings"])
        except Exception:
            pass
        stat["note"] = "no entries enumerated (cold layer may be empty or list_all unavailable)"
        return stat

    # ---- Step 2: 向量预筛去重 (B2 重写 + 反转消解) ---------------
    fuzzy_reversal_candidates: list = []
    stat["merged"], fuzzy_groups, rev_count, fuzzy_dedup, step2_forgotten = _merge_duplicates(client, all_entries)
    fuzzy_reversal_candidates.extend(fuzzy_dedup)
    stat["reversals_resolved"] += rev_count

    # ---- Step 2b: 模糊组 LLM 确认 (B4) ----------------------------------
    if fuzzy_groups:
        llm_merged = _llm_dedup_confirm(client, fuzzy_groups)
        stat["merged"] += llm_merged

    # ---- Step 3: 过时事实清理 (B3 + 回收站入站) ------------------------
    stat["cleaned"] = _clean_stale(client, all_entries, trash)
    stat["pending_stale"] = trash.count()  # 更新回收站计数

    # ---- Step 4: 冲突事实取舍 --------------------------------------------
    stat["conflicts_resolved"], conf_rev, step4_forgotten = _resolve_conflicts(client, all_entries, fuzzy_reversal_candidates)
    stat["reversals_resolved"] += conf_rev

    # ---- Step 4b: LLM 语义反转兜底 (模糊候选) ------------------------
    # R4: 全局去重 (Step 2 + Step 4 可能对同一对重复追加)
    seen_pairs = set()
    deduped_candidates = []
    for pair in fuzzy_reversal_candidates:
        if len(pair) != 2:
            continue
        key = (pair[0].get("id", ""), pair[1].get("id", ""))
        if key not in seen_pairs:
            seen_pairs.add(key)
            deduped_candidates.append(pair)
    fuzzy_reversal_candidates = deduped_candidates

    # Fix1: 汇总已处理 id (Step 2 + Step 4 的 forget/merge victim), Step 4b 跳过
    processed_ids = set(step2_forgotten)
    processed_ids.update(step4_forgotten)

    if fuzzy_reversal_candidates:
        from .llm_judge import judge_reversal
        from ..trash_store import TrashStore

        batch_size = 5
        llm_reversal_hits = 0       # Fix2: 真正 LLM 确认反转数
        llm_candidates_processed = 0  # Fix2: 处理的候选对总数
        for i in range(0, len(fuzzy_reversal_candidates), batch_size):
            batch = fuzzy_reversal_candidates[i:i + batch_size]
            for pair in batch:
                if len(pair) != 2:
                    continue
                older, newer = pair[0], pair[1]
                # Fix1: 跳过已被 Step 2/3/4 处理过的 pair (幽灵记录防护)
                if older.get("id") in processed_ids or newer.get("id") in processed_ids:
                    continue
                result = judge_reversal([older, newer])
                if result is None:
                    # R2: LLM 不可用/失败 → 退回直接合并 (排除反转后就是同主题重复)
                    _merged = _merge_for_dedup(newer.get("content", ""), older.get("content", ""))
                    try:
                        client.update(newer["id"], _merged)
                        TrashStore().add(
                            older["id"], older.get("content", ""),
                            reason="merge_obsolete",
                            source_decision="llm_fallback_merge")
                        client.forget(older["id"])
                        llm_candidates_processed += 1  # Fix2: fallback 合并也算处理
                    except Exception:
                        pass
                    continue
                if result.get("decision") != "reversal":
                    # R2: LLM 判非反转 → 退回直接合并
                    _merged = _merge_for_dedup(newer.get("content", ""), older.get("content", ""))
                    try:
                        client.update(newer["id"], _merged)
                        TrashStore().add(
                            older["id"], older.get("content", ""),
                            reason="merge_obsolete",
                            source_decision="llm_not_reversal_fallback")
                        client.forget(older["id"])
                        llm_candidates_processed += 1  # Fix2: 非反转合并也算处理
                    except Exception:
                        pass
                    continue

                # LLM 确认反转 → forget 旧条 + 回收站
                older_id = result.get("older_id") or older.get("id", "")
                if not older_id:
                    continue
                try:
                    client.forget(older_id)
                    TrashStore().add(
                        older_id, older.get("content", ""),
                        reason="reversal_llm",
                        source_decision="llm_reversal")
                    llm_reversal_hits += 1
                    llm_candidates_processed += 1  # Fix2
                except Exception:
                    continue

        stat["candidates_processed"] = llm_candidates_processed
        stat["reversals_resolved"] += llm_reversal_hits

    # ---- Step 4c: 冷层降权遗忘 (decay → trash) ---------------------------
    stat["forgotten"] = _forget_decayed(client, all_entries, trash)
    stat["pending_stale"] = trash.count()

    # ---- Step 5: 向量完整性校验 ------------------------------------------
    try:
        s = client.stats()
        stat["total"] = s.get("total", 0)
        stat["embeddings"] = s.get("embeddings", 0)
        stat["vector_ok"] = (stat["total"] == stat["embeddings"])
    except Exception as e:
        stat["errors"] += 1
        stat["vector_ok"] = None
        stat["vector_error"] = str(e)

    return stat


# ---- Step 0: 回收站巡检 ----------------------------------------------------

def _trash_cycle(client) -> Tuple[int, int]:
    """回收站巡检: 召回恢复 + 到期清空。

    恢复机制 (P1-7 明确):
    - 仅适用于"未 forget 的观察类"条目 (reason=stale_candidate):
      此类条目仍在冷层, recall 命中 → trash.remove 恢复。
    - 已 forget 的 victim 类 (reason 含 reversal/conflict/decayed/merge):
      冷层已删除, recall 不可能命中; 30 天到期 → trash-empty 直接清空。
      设计意图: 已 forget 条目恢复需重新 remember (含重新 embedding),
      复杂度高于收益; 30 天窗口已足够长, 真重要的会被重新写入。

    返回 (revived, cleared)。
    """
    from ..trash_store import TrashStore

    trash = TrashStore()
    revived = 0
    cleared = 0

    # ---- 召回即恢复: 对 trash 条目逐一 recall, 命中 top_k=10 则恢复 ----
    for entry in trash.get_all():
        mid = entry.get("memory_id", "")
        content = entry.get("content", "")
        if not mid:
            continue
        try:
            results = client.recall_results(content[:200], top_k=_VEC_NEIGHBOR_K)
            for r in results:
                if r.get("id") == mid:
                    trash.remove(mid)
                    revived += 1
                    break
        except Exception:
            continue

    # ---- 到期清空: >30 天 → forget + 移除 -------------------------------
    for entry in trash.get_expired():
        mid = entry.get("memory_id", "")
        if not mid:
            continue
        try:
            client.forget(mid)
        except Exception:
            pass  # 可能已被遗忘, 不影响回收站清理
        trash.remove(mid)
        cleared += 1

    return revived, cleared


# ---- Step 1: 枚举 (B1: 邻居扩展升级) ---------------------------------------

def _enumerate_all(client, total_hint: int = 0) -> List[Dict[str, Any]]:
    """尽量枚举冷层全部条目 (B1 升级: 多路召回 + 邻居扩展, 覆盖率 ≥95%)。

    策略:
      1. 尝试 client.list_all() (若服务器支持)
      2. 降级: 10 路种子召回, 每路 top_k=10
      3. 邻居扩展: 对已收集条目内容各 recall top_k=5
      4. 按 id 去重, 最多 2 轮
    """
    # 尝试 list_all
    try:
        results = client.list_all()
        if results:
            # P2-4: 过滤已 superseded 条目 (服务器 sleep 时标记, 治理层不再重复处理)
            active = [r for r in results if not r.get("superseded_by")]
            return active
    except (AttributeError, Exception):
        pass

    # 降级: 多路 recall + 邻居扩展
    seen: Set[str] = set()
    all_entries: List[Dict[str, Any]] = []

    # Round 1: 10 路种子召回, top_k=10
    for query in _RECALL_QUERIES:
        try:
            results = client.recall_results(query, top_k=_RECALL_BREADTH)
            for r in results:
                rid = r.get("id", "")
                if rid and rid not in seen:
                    seen.add(rid)
                    all_entries.append(r)
        except Exception:
            continue

    # Round 2: 邻居扩展 — 对已收集条目内容各 recall top_k=5
    for entry in all_entries[:]:
        content = entry.get("content", "")
        if not content:
            continue
        try:
            results = client.recall_results(content[:200], top_k=_RECALL_NEIGHBOR)
            for r in results:
                rid = r.get("id", "")
                if rid and rid not in seen:
                    seen.add(rid)
                    all_entries.append(r)
        except Exception:
            continue

    return all_entries


# ---- Step 2: 向量预筛去重 (B2 重写) -----------------------------------------

def _merge_duplicates(client, entries: List[Dict[str, Any]]) -> Tuple[int, list, int, list, set]:
    """B2 向量预筛去重: sqlite-vec 邻居替代 bigram 倒排。

    1. 对每条条目 recall top_k=_VEC_NEIGHBOR_K 找向量邻居
    2. 邻居中 dense_score > _VEC_SCORE_THRESHOLD → 候选对
    3. 哈希兜底: _norm_sentence 相同 → 直接判重
    4. 候选对做精确文本判定: SequenceMatcher + bigram 覆盖率
    5. ≥_SIM_THRESHOLD: 先查反转 → 删旧留新; 否则合并
    6. _FUZZY_LOW-_SIM_THRESHOLD → LLM

    返回 (merged_count, fuzzy_groups, reversal_count, fuzzy_reversal_candidates)。
    """
    n = len(entries)
    if n <= 1:
        return 0, [], 0, [], set()

    # ---- 0. 建立索引 ----
    id_to_entry: Dict[str, Dict[str, Any]] = {}
    norms: Dict[str, str] = {}
    for entry in entries:
        eid = entry.get("id", "")
        if not eid:
            continue
        id_to_entry[eid] = entry
        norms[eid] = _norm_sentence(entry.get("content", ""))

    # ---- 1. 哈希兜底: 规范化后完全相同 → 直接合并 ----
    # 按 norm 分组收集 id 列表
    norm_to_ids: Dict[str, List[str]] = {}
    for eid, norm in norms.items():
        if not norm:
            continue
        norm_to_ids.setdefault(norm, []).append(eid)

    merged = 0
    reversal_count = 0
    fuzzy_reversal_candidates: List[List[Dict]] = []
    fuzzy_groups: List[List[Dict]] = []
    to_forget: Set[str] = set()
    processed_pairs: Set[Tuple[str, str]] = set()

    for norm, eids in norm_to_ids.items():
        if len(eids) < 2:
            continue
        # 多个 id 共享同一个规范化文本 → 字面重复, 直接合并
        keeper_id = eids[0]
        for victim_id in eids[1:]:
            if victim_id in to_forget or keeper_id in to_forget:
                continue
            keeper = id_to_entry[keeper_id]
            victim = id_to_entry[victim_id]
            merged_content = _merge_for_dedup(
                keeper.get("content", ""), victim.get("content", "")
            )
            try:
                client.update(keeper_id, merged_content)
                # P1-5: victim 进回收站再 forget
                TrashStore().add(
                    victim_id, victim.get("content", ""),
                    reason="merge_obsolete",
                    source_decision="rule_merge")
                client.forget(victim_id)
                to_forget.add(victim_id)
                merged += 1
            except Exception:
                continue

    # ---- 2. 向量预筛: 对每条条目逐条 recall 找邻居 ----
    # 只处理未被哈希兜底合并的条目
    active_ids = [eid for eid in id_to_entry if eid not in to_forget]

    for eid_a in active_ids:
        if eid_a in to_forget:
            continue
        entry_a = id_to_entry[eid_a]
        content_a = entry_a.get("content", "")
        if not content_a:
            continue

        # 向量召回邻居
        try:
            neighbors = client.recall_results(
                content_a[:200], top_k=_VEC_NEIGHBOR_K
            )
        except Exception:
            continue

        for nb in neighbors:
            eid_b = nb.get("id", "")
            if not eid_b or eid_b == eid_a:
                continue
            if eid_b in to_forget or eid_b not in id_to_entry:
                continue
            if nb.get("dense_score", 0) < _VEC_SCORE_THRESHOLD:
                continue

            # 去重对
            pair_key = tuple(sorted([eid_a, eid_b]))
            if pair_key in processed_pairs:
                continue
            processed_pairs.add(pair_key)

            entry_b = id_to_entry[eid_b]
            c1 = content_a
            c2 = entry_b.get("content", "")

            # ---- 文本精确判定 ----
            ratio = difflib.SequenceMatcher(None, c1, c2).ratio()
            norm_a = norms.get(eid_a, "")
            norm_b = norms.get(eid_b, "")
            bg_a = {norm_a[i:i + 2] for i in range(len(norm_a) - 1)}
            bg_b = {norm_b[i:i + 2] for i in range(len(norm_b) - 1)}
            bg_cov_a = _bigram_coverage(norm_a, bg_b)
            bg_cov_b = _bigram_coverage(norm_b, bg_a)
            combined = max(ratio, bg_cov_a, bg_cov_b)

            if combined >= _SIM_THRESHOLD:
                # ---- 反转检测: 新条否定旧条 -> 删旧留新, 不焊句子 (2026-08-05) ----
                if _is_reversal_pair(entry_a, entry_b):
                    older, newer = (
                        (entry_a, entry_b)
                        if (entry_a.get("timestamp") or "") <= (entry_b.get("timestamp") or "")
                        else (entry_b, entry_a)
                    )
                    if older["id"] not in to_forget:
                        try:
                            client.forget(older["id"])
                            from ..trash_store import TrashStore
                            TrashStore().add(
                                older["id"], older.get("content", ""),
                                reason="reversal_obsolete",
                                source_decision="rule_reversal")
                            to_forget.add(older["id"])
                            reversal_count += 1
                        except Exception:
                            pass
                    continue

                # 规则反转未命中 + 同主题 + 有时间 → LLM 模糊候选
                # P1-4: 进入 LLM 候选的 pair 跳过直接合并，等 LLM 判定后再决定
                if _topic_overlap_with_ts(entry_a, entry_b):
                    # 保证时间序: older first, newer second
                    ts_a = entry_a.get("timestamp") or ""
                    ts_b = entry_b.get("timestamp") or ""
                    if ts_a <= ts_b:
                        fuzzy_reversal_candidates.append([entry_a, entry_b])
                    else:
                        fuzzy_reversal_candidates.append([entry_b, entry_a])
                    continue  # P1-4: 跳过直接合并, 等 LLM 判定

                # 直接合并 (保留较长)
                if len(c2) > len(c1):
                    keeper, victim = entry_b, entry_a
                else:
                    keeper, victim = entry_a, entry_b

                if victim["id"] in to_forget or keeper["id"] in to_forget:
                    continue

                merged_content = _merge_for_dedup(
                    keeper.get("content", ""), victim.get("content", "")
                )
                try:
                    client.update(keeper["id"], merged_content)
                    # P1-5: victim 进回收站再 forget
                    TrashStore().add(
                        victim["id"], victim.get("content", ""),
                        reason="merge_obsolete",
                        source_decision="rule_merge")
                    client.forget(victim["id"])
                    to_forget.add(victim["id"])
                    merged += 1
                except Exception:
                    continue
            elif combined >= _FUZZY_LOW:
                # 模糊组 → 收集待 LLM
                fuzzy_groups.append([entry_a, entry_b])

    return merged, fuzzy_groups, reversal_count, fuzzy_reversal_candidates, to_forget


def _llm_dedup_confirm(client, fuzzy_groups: List[List[Dict]]) -> int:
    """模糊去重组交 LLM 确认。返回 LLM 确认合并数。"""
    from .llm_judge import judge_dedup

    merged = 0
    for group in fuzzy_groups:
        if len(group) < 2:
            continue
        result = judge_dedup(group)
        if result is None or result.get("decision") != "duplicate":
            continue

        keep_id = result.get("keep_id", "")
        discard_ids = result.get("discard_ids", [])

        keeper = next((e for e in group if e["id"] == keep_id), None)
        if keeper is None:
            keeper = group[0]

        for victim_id in discard_ids:
            victim = next((e for e in group if e["id"] == victim_id), None)
            if victim is None:
                continue
            try:
                merged_content = _merge_for_dedup(
                    keeper.get("content", ""), victim.get("content", "")
                )
                client.update(keeper["id"], merged_content)
                # Fix3: victim 进回收站 (P1-5 遗漏)
                from ..trash_store import TrashStore
                TrashStore().add(
                    victim_id, victim.get("content", ""),
                    reason="merge_obsolete",
                    source_decision="llm_dedup")
                client.forget(victim_id)
                merged += 1
            except Exception:
                continue

    return merged


# ---- Step 3: 过时清理 (B3 + 回收站入站) ------------------------------------

_LONG_STALE_MARKERS = ["落地中", "进行中", "规划中", "待定", "未完成"]  # P2-2: 进行时词在此判定


def _clean_stale(client, entries: List[Dict[str, Any]], trash=None) -> int:
    """B3 过时清理: 短条目直接 forget, 长条目含进行时标记 → LLM → 回收站。

    - 短条目 (≤80字) 含任何 STALE_MARKERS → forget
    - 长条目 (>80字) 含常规过时标记 (已修复等) → forget
    - 长条目含 落地中/进行中 → LLM:
        stale → forget (现有)
        not_stale → 入回收站 (新增, 不 forget)
        LLM 失败 → 入回收站 (保守)

    Args:
        trash: TrashStore 实例, 用于入站 not_stale 条目
    """
    from .config import STALE_MARKERS
    from .llm_judge import judge_stale
    from ..trash_store import TrashStore

    if trash is None:
        trash = TrashStore()

    cleaned = 0
    stale_candidates = []  # 长条目 + 落地中/进行中, 待 LLM

    for entry in entries:
        content = entry.get("content", "")
        if not content:
            continue

        is_short = len(content) <= 80
        matched_markers = [m for m in STALE_MARKERS if m in content]
        if not matched_markers:
            continue

        if is_short:
            # 短条目: 先进回收站再 forget (P2-2: 与长条目对称, 统一进回收站)
            try:
                trash.add(
                    entry["id"],
                    entry.get("content", ""),
                    reason="stale_short",
                    source_decision="rule_stale")
                client.forget(entry["id"])
                cleaned += 1
            except Exception:
                pass
        else:
            # 长条目
            has_progress = any(m in content for m in _LONG_STALE_MARKERS)
            has_regular_stale = any(
                m in content for m in STALE_MARKERS
                if m not in _LONG_STALE_MARKERS
            )

            if has_regular_stale:
                # 含常规过时标记 (已修复等) → 直接 forget
                try:
                    client.forget(entry["id"])
                    cleaned += 1
                except Exception:
                    pass
            elif has_progress:
                # 只有进行时标记 → 交 LLM
                stale_candidates.append(entry)

    # ---- LLM 确认 (分批 ≤5) ----
    if stale_candidates:
        batch_size = 5
        for i in range(0, len(stale_candidates), batch_size):
            batch = stale_candidates[i:i + batch_size]
            result = judge_stale(batch)

            if result is None:
                # LLM 失败 → 保守入回收站
                for entry in batch:
                    trash.add(
                        entry["id"],
                        entry.get("content", ""),
                        reason="stale_candidate",
                        source_decision="llm_failed",
                    )
                continue

            if result.get("decision") == "stale":
                stale_ids = set(result.get("stale_ids", []))
                for entry in batch:
                    if entry["id"] in stale_ids:
                        try:
                            client.forget(entry["id"])
                            cleaned += 1
                        except Exception:
                            pass
                    else:
                        # 同批未标记为 stale → 入回收站
                        trash.add(
                            entry["id"],
                            entry.get("content", ""),
                            reason="stale_candidate",
                            source_decision="llm_not_stale",
                        )
            else:
                # LLM 判 not_stale → 入回收站
                for entry in batch:
                    trash.add(
                        entry["id"],
                        entry.get("content", ""),
                        reason="stale_candidate",
                        source_decision="llm_not_stale",
                    )

    return cleaned


# ---- Step 3b: 冷层降权遗忘判定 --------------------------------------------

def _forget_decayed(client, entries, trash=None):
    """冷层降权遗忘: final_score = importance × 0.5^(days/90) < 0.05 且 importance < 0.8 → 移入回收站。

    语义: 存储层留存决策, 回答"该条目是否值得保留"。
    与 _apply_decay 分工: _apply_decay 管 recency-aware 排序 (base=dense_score),
    此处管存储留存 (base=importance, 无 query 语义时更稳定)。

    遗忘后从冷层删除该条目 (移入回收站即从 active 移除)。
    高价值 (importance >= 0.8) 永不进入遗忘路径。

    Returns:
        int: 本次遗忘数
    """
    from datetime import datetime, timezone as _tz
    from ..trash_store import TrashStore

    if trash is None:
        trash = TrashStore()

    forgotten = 0
    now = datetime.now(_tz.utc)

    for entry in entries:
        eid = entry.get("id", "")
        if not eid:
            continue
        importance = entry.get("importance", 0.5)

        # 高价值永不进遗忘路径
        if importance >= 0.8:
            continue

        # 解析 last_recalled (P0-2: 缺失时降级用 timestamp)
        last_str = entry.get("last_recalled")
        days = 365  # 最终兜底
        if last_str:
            try:
                normalized = last_str.replace("Z", "+00:00")
                last_dt = datetime.fromisoformat(normalized)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_tz.utc)
                delta = now - last_dt
                days = max(delta.days, 0)
            except (ValueError, TypeError):
                days = 365
        else:
            # last_recalled 缺失 → 降级用 timestamp（写入时间）
            ts_str = entry.get("timestamp")
            if ts_str:
                try:
                    normalized = ts_str.replace("Z", "+00:00")
                    ts_dt = datetime.fromisoformat(normalized)
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=_tz.utc)
                    delta = now - ts_dt
                    days = max(delta.days, 0)
                except (ValueError, TypeError):
                    days = 365

        factor = 0.5 ** (days / 90)
        final_score = importance * factor

        if final_score < 0.05:
            # 安全顺序: 先入回收站, 成功后再从冷层删除。
            # trash.add 失败时跳过该条 (不 forget), 下轮巡检再试。
            try:
                trash.add(
                    eid,
                    entry.get("content", ""),
                    reason="decayed",
                    source_decision="decay_forget",
                )
            except Exception:
                continue  # 回收站写入失败 → 保守, 不删冷层数据
            try:
                client.forget(eid)
                forgotten += 1
            except Exception:
                # forget 失败但回收站已写入 → 仍计为成功
                # (冷层可能已不可达, 回收站有备份, 下次巡检会处理)
                forgotten += 1

    return forgotten


# ---- 合并辅助 --------------------------------------------------------------

def _merge_for_dedup(base: str, other: str) -> str:
    """合并两条同主题条目: base 为主体, 追加 other 的独有句子。"""
    base_sentences = set(re.split(r"[。！？;；\n]", base))
    extra = []
    for s in re.split(r"[。！？;；\n]", other):
        s = s.strip()
        if not s:
            continue
        is_new = True
        for bs in base_sentences:
            if s in bs or bs in s or difflib.SequenceMatcher(None, s, bs).ratio() > 0.8:
                is_new = False
                break
        if is_new:
            extra.append(s)
    if extra:
        base = base.rstrip("。！？;；\n") + "。" + "。".join(extra) + "。"
    return base


# ---- Step 4: 冲突取舍 -----------------------------------------------------

def _resolve_conflicts(client, entries: List[Dict[str, Any]], fuzzy_reversal_candidates: list = None) -> Tuple[int, int, set]:
    """解决同主题冲突: 反转冲突按时间取舍, 普通冲突保留最新, victim 进回收站。

    冲突判定: 两条内容同主题 (相似度 > CONFLICT_THRESHOLD)
    但差异不够大到视为完全重复 (相似度 < SIM_THRESHOLD)。

    返回 (resolved_count, reversal_count)。
    """
    if fuzzy_reversal_candidates is None:
        fuzzy_reversal_candidates = []
    if len(entries) <= 1:
        return 0, 0, set()

    from ..trash_store import TrashStore

    n = len(entries)
    resolved = 0
    reversal_count = 0
    to_forget: Set[str] = set()

    for i in range(n):
        if entries[i]["id"] in to_forget:
            continue
        for j in range(i + 1, n):
            if entries[j]["id"] in to_forget:
                continue
            c1 = entries[i].get("content", "")
            c2 = entries[j].get("content", "")
            ratio = difflib.SequenceMatcher(None, c1, c2).ratio()

            if _CONFLICT_THRESHOLD <= ratio < _SIM_THRESHOLD:
                # ---- 反转检测: 新条否定旧条 -> 按时间取舍 (2026-08-05) ----
                if _is_reversal_pair(entries[i], entries[j]):
                    older, newer = (
                        (entries[i], entries[j])
                        if (entries[i].get("timestamp") or "") <= (entries[j].get("timestamp") or "")
                        else (entries[j], entries[i])
                    )
                    victim_id = older.get("id", "")
                    if victim_id and victim_id not in to_forget:
                        try:
                            client.forget(victim_id)
                            TrashStore().add(
                                victim_id, older.get("content", ""),
                                reason="reversal_obsolete",
                                source_decision="rule_reversal")
                            to_forget.add(victim_id)
                            reversal_count += 1
                            resolved += 1
                        except Exception:
                            continue
                    continue

                # 规则反转未命中 + 同主题 + 有时间 → LLM 模糊候选
                if _topic_overlap_with_ts(entries[i], entries[j]):
                    ts_i = entries[i].get("timestamp") or ""
                    ts_j = entries[j].get("timestamp") or ""
                    if ts_i <= ts_j:
                        fuzzy_reversal_candidates.append([entries[i], entries[j]])
                    else:
                        fuzzy_reversal_candidates.append([entries[j], entries[i]])
                    continue  # R3: 跳过普通冲突取舍, 等 Step 4b 统一处理

                # 非反转冲突: 按时间取舍 (有 timestamp 用时间, 否则按长度)
                ts_i = entries[i].get("timestamp") or ""
                ts_j = entries[j].get("timestamp") or ""
                if ts_i and ts_j:
                    if ts_j >= ts_i:
                        keeper, victim = entries[j], entries[i]
                    else:
                        keeper, victim = entries[i], entries[j]
                elif len(c2) >= len(c1):
                    keeper, victim = entries[j], entries[i]
                else:
                    keeper, victim = entries[i], entries[j]

                victim_id = victim.get("id", "")
                if victim_id and victim_id not in to_forget:
                    try:
                        client.forget(victim_id)
                        TrashStore().add(
                            victim_id, victim.get("content", ""),
                            reason="conflict_obsolete",
                            source_decision="rule_conflict")
                        to_forget.add(victim_id)
                        resolved += 1
                    except Exception:
                        continue

    return resolved, reversal_count, to_forget
