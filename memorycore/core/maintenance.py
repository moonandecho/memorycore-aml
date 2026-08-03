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


# ---- 主入口 ----------------------------------------------------------------

def run_maintenance(client) -> dict:
    """冷层全量治理 v3 (向量预筛 + 回收站)。

    Returns:
        dict: {scanned, merged, cleaned, conflicts_resolved,
               vector_ok, total, embeddings, errors,
               pending_stale, trash_cleared, trash_revived}
    """
    stat: Dict[str, Any] = {
        "scanned": 0,
        "merged": 0,
        "cleaned": 0,
        "conflicts_resolved": 0,
        "vector_ok": None,
        "total": 0,
        "embeddings": 0,
        "errors": 0,
        "pending_stale": 0,    # 回收站当前条目
        "trash_cleared": 0,    # 本次到期清空
        "trash_revived": 0,    # 本次恢复 (被召回命中)
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

    # ---- Step 2: 向量预筛去重 (B2 重写) -------------------------------
    stat["merged"], fuzzy_groups = _merge_duplicates(client, all_entries)

    # ---- Step 2b: 模糊组 LLM 确认 (B4) ----------------------------------
    if fuzzy_groups:
        llm_merged = _llm_dedup_confirm(client, fuzzy_groups)
        stat["merged"] += llm_merged

    # ---- Step 3: 过时事实清理 (B3 + 回收站入站) ------------------------
    stat["cleaned"] = _clean_stale(client, all_entries, trash)
    stat["pending_stale"] = trash.count()  # 更新回收站计数

    # ---- Step 4: 冲突事实取舍 --------------------------------------------
    stat["conflicts_resolved"] = _resolve_conflicts(client, all_entries)

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
            trash.remove(mid)
            cleared += 1
        except Exception:
            continue

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
            return results
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

def _merge_duplicates(client, entries: List[Dict[str, Any]]) -> Tuple[int, list]:
    """B2 向量预筛去重: sqlite-vec 邻居替代 bigram 倒排。

    1. 对每条条目 recall top_k=_VEC_NEIGHBOR_K 找向量邻居
    2. 邻居中 dense_score > _VEC_SCORE_THRESHOLD → 候选对
    3. 哈希兜底: _norm_sentence 相同 → 直接判重
    4. 候选对做精确文本判定: SequenceMatcher + bigram 覆盖率
    5. ≥_SIM_THRESHOLD 合并; _FUZZY_LOW-_SIM_THRESHOLD → LLM

    返回 (merged_count, fuzzy_groups)。
    """
    n = len(entries)
    if n <= 1:
        return 0, []

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
                    client.forget(victim["id"])
                    to_forget.add(victim["id"])
                    merged += 1
                except Exception:
                    continue
            elif combined >= _FUZZY_LOW:
                # 模糊组 → 收集待 LLM
                fuzzy_groups.append([entry_a, entry_b])

    return merged, fuzzy_groups


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
                client.forget(victim_id)
                merged += 1
            except Exception:
                continue

    return merged


# ---- Step 3: 过时清理 (B3 + 回收站入站) ------------------------------------

_LONG_STALE_MARKERS = ["落地中", "进行中"]


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
            # 短条目: 直接 forget (含落地中等进行时标记也直接清理)
            try:
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

def _resolve_conflicts(client, entries: List[Dict[str, Any]]) -> int:
    """解决同主题冲突: 保留最新的条目, 旧版 forget。

    冲突判定: 两条内容同主题 (相似度 > CONFLICT_THRESHOLD)
    但差异不够大到视为完全重复 (相似度 < SIM_THRESHOLD)。
    """
    if len(entries) <= 1:
        return 0

    n = len(entries)
    resolved = 0
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
                if len(c2) >= len(c1):
                    keeper, victim = entries[j], entries[i]
                else:
                    keeper, victim = entries[i], entries[j]

                try:
                    client.forget(victim["id"])
                    to_forget.add(victim["id"])
                    resolved += 1
                except Exception:
                    continue

    return resolved
