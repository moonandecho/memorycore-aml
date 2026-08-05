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
import re
from typing import Any, Dict, List, Optional, Tuple

from .classifier import classify, classify_user_pref, should_keep_local, HOT, COLD, STALE

# ---- 相似度阈值 ------------------------------------------------------------

_SAME_FACT_RATIO = 0.85       # 内容几乎相同 → 同一事实
_SIMILAR_TOPIC_RATIO = 0.50   # 同主题但细节不同 → 需 merge
_RECALL_SCORE_SAME = 0.80     # recall dense_score >= 此值视为高度匹配
_RECALL_SCORE_SIMILAR = 0.48  # recall dense_score >= 此值视为同主题
_RECALL_TOP_K = 3             # 查重时的召回数
_MIN_TEXT_RATIO = 0.15        # 最低字面相似度门禁 (防向量误匹配)
# ---- 用户偏好摘要锚点 (P4) --------------------------------------------------

_ANCHOR_PREFIX = "[用户偏好摘要]"   # 内容前缀: 识别/查重/召回锚点
_ANCHOR_IMPORTANCE = 0.8        # 高 importance → prefetch 分层注入高档
_ANCHOR_MAX_CHARS = 800         # 锚点长度上限 (超限截断保留头部)
_ANCHOR_QUERY = "[用户偏好摘要]"  # recall 查已有锚点 (前缀强关键词)



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
        "kept": 0,
        "errors": 0,
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
                        r = client.remember(s, importance=0.5, scope="global")
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

    # ---- Step 2+3+5: 逐条处理 --------------------------------------------
    for entry in entries:
        if should_keep_local(entry):
            stat["kept"] += 1
            continue

        decision = classify(entry, importance=0.6)
        d = decision["decision"]

        if d == STALE:
            _handle_stale(store, client, target, entry, stat)
        elif d == COLD:
            if target == "user" and classify_user_pref(
                    entry, sentence_level=True) == "sink":
                anchor_parts.append(entry)  # P4: 整条下沉的用户侧偏好进锚点
            _handle_cold_migration(store, client, target, entry, stat)
        else:
            stat["kept"] += 1

    # ---- Step 6: 验证 ----------------------------------------------------
    if target == "user" and anchor_parts:
        _update_pref_anchor(client, store, anchor_parts, stat)
    stat["chars_after"] = store.char_count(target)
    stat["usage_after"] = f"{store.usage_pct(target)}%"
    stat["target"] = target
    return stat




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
                # 冷层有相似 → update 合并
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
    """检测本地同主题碎片并合并。返回 (merged, merge_count)。"""
    if len(entries) <= 1:
        return list(entries), 0

    n = len(entries)
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

    for i in range(n):
        for j in range(i + 1, n):
            ratio = _smart_ratio(entries[i], entries[j])
            if ratio > _SIMILAR_TOPIC_RATIO:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        root = find(i)
        groups.setdefault(root, []).append(i)

    merged = []
    merge_count = 0
    for indices in groups.values():
        if len(indices) == 1:
            merged.append(entries[indices[0]])
        else:
            group_entries = [entries[i] for i in indices]
            merged.append(_merge_group(group_entries))
            merge_count += len(indices) - 1

    return merged, merge_count


def _norm_sentence(s: str) -> str:
    """规范化句子用于去重: 去空白/标点, 保留括号内容, 小写。

    括号内容 (路径/注释/别名如 Code Drive、SMB 共享) 是语义核心,
    删除会导致同义句 "D 盘=/home/user/D (Code Drive...)" 与
    "D 盘=Code Drive (path=/home/user/D...)" 规范化后反而不同。
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

def _recall_safe(client, entry: str) -> List[Dict[str, Any]]:
    """安全调用 recall_results, 截前 200 字作查询。"""
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
