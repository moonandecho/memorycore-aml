#!/usr/bin/env python3
"""core/llm_judge.py — LLM judgements for cold-tier governance

Handles boundary entries that pure rules cannot confirm:
  - judge_dedup:  dedup fuzzy groups (similarity 0.40-0.75)
  - judge_stale:  stale confirmation for long entries (>80 chars) containing
                  "落地中/进行中"-style in-progress markers

Boundaries:
  - prompt hard-constrained: JSON output only, no free-form rewriting, no
    fact modification
  - any failure (no key / timeout / parse error) → conservative fallback,
    never blocks maintenance
  - batch <=10 entries per call, serial, 15s timeout per call
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from . import llm_config


def _call_llm(prompt: str) -> Optional[Dict[str, Any]]:
    """Call the LLM, return the parsed JSON dict. None on any failure.

    Guard (review C2): unconfigured / per-run cap / fail backoff -> None
    (conservative fallback, never blocks maintenance). Observable three
    states go through llm_config into the current session stat["llm"] + logs.
    """
    cfg = llm_config.acquire("冷层判定")
    if cfg is None:
        return None

    try:
        payload = {
            "model": cfg.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1000,
            "response_format": {"type": "json_object"},
        }
        data = llm_config.chat(cfg, payload)
        content = data["choices"][0]["message"]["content"].strip()
        # Strip markdown code fences
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        result = json.loads(content)
        llm_config.note_success()
        return result
    except llm_config.LLMError as e:
        llm_config.note_failure(e.category, e.detail)
        return None
    except Exception:
        llm_config.note_failure("bad_response")
        return None


def judge_dedup(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Judge whether a dedup candidate group is actually duplicate.

    Args:
        entries: 2-5 candidate entries [{id, content}, ...]

    Returns:
        {"decision": "duplicate"|"not_duplicate", "keep_id": "...",
         "discard_ids": [...], "reason": "..."} | None (failure)
    """
    if len(entries) < 2:
        return None

    items = []
    for i, e in enumerate(entries):
        cid = e.get("id", f"item_{i}")
        content = e.get("content", "")
        items.append(f"[{cid}] {content}")

    prompt = (
        "你是记忆去重助手。以下是 2-5 条冷层记忆条目, 语义相似度在 0.40-0.75 的模糊区间, "
        "无法用纯规则判定是否重复。请判断它们是否为同一事实的不同表述:\n\n"
        + "\n\n".join(items)
        + "\n\n判定规则:\n"
        "1. 如果它们描述的是同一事实/事件/配置 → duplicate, 保留信息最完整的条目\n"
        "2. 如果它们虽然同主题但描述不同的事实/不同时间/不同对象 → not_duplicate\n"
        "3. 如果拿不准 → not_duplicate (宁留不误删)\n"
        '4. 只输出 JSON: {"decision": "duplicate"|"not_duplicate", '
        '"keep_id": "最完整条目的id", "discard_ids": ["其他条目的id", ...], '
        '"reason": "一句话判据"}\n'
        "5. 不修改事实内容, 不添加推断, 不自由发挥"
    )

    result = _call_llm(prompt)
    if result is None:
        return None
    if "decision" not in result:
        return None
    return result


def judge_stale(entries: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Judge whether long entries (>80 chars) carrying stale/in-progress
    markers are actually stale.

    Covers both marker classes (in-progress: 落地中/进行中/规划中/待定/未完成;
    completed-state: 已修复/已解决/已退役/已停用/已废弃/不再使用/...).
    A stale word is often just historical background inside an otherwise
    valuable entry — such entries stay not_stale (prefer keeping).

    Args:
        entries: 1-5 candidate entries [{id, content}, ...]

    Returns:
        {"decision": "stale"|"not_stale", "stale_ids": [...],
         "reason": "..."} | None (failure)
    """
    if not entries:
        return None

    items = []
    for i, e in enumerate(entries):
        cid = e.get("id", f"item_{i}")
        content = e.get("content", "")
        items.append(f"[{cid}] {content}")

    prompt = (
        "你是记忆过时判定助手。以下是 1-5 条冷层记忆条目, 它们包含过时/进行时状态标记"
        "(如: 落地中/进行中/规划中/待定/未完成/已修复/已解决/已切换/已退役/已停用/"
        "已迁移/已删除/已完成/不再使用/已废弃), 但这些条目较长 (>80字), 过时词可能只是"
        "条目内描述的历史背景, 条目本身仍是有保留价值的方案/事实/决策记录。\n\n"
        + "\n\n".join(items)
        + "\n\n判定规则:\n"
        "1. 如果条目整体就是一个已完成的过时状态记录(如\"某某已停用, 不再使用\"), 保留无价值 → stale\n"
        "2. 如果过时词只是描述条目中的部分历史背景(如\"旧方案已退役, 新方案是...\"、"
        "\"延迟已修复\"、\"旧服务已停用但替代方案如下\"), 条目本身仍有保留价值 → not_stale\n"
        "3. 如果无法确定、或条目混合了有价值内容与过时描述 → not_stale (宁留不误删)\n"
        '4. 只输出 JSON: {"decision": "stale"|"not_stale", '
        '"stale_ids": ["已过时的条目id", ...], '
        '"reason": "一句话判据"}\n'
        "5. 不修改事实内容, 不添加推断, 不自由发挥"
    )

    result = _call_llm(prompt)
    if result is None:
        return None
    if "decision" not in result:
        return None
    return result


# ---- judge_reversal: LLM 语义反转兜底 (2026-08-06 实现) ----


def judge_reversal(entries):
    """判定两条条目是否语义反转 (不依赖显式否定词, 靠 LLM 语义理解)。

    仅当规则反转检测 (_is_reversal_pair) 因缺少否定词返回 False,
    但两条同主题 + 时间可判时才调用。LLM 不可用 -> 返回 None -> 保守保留。
    """
    if len(entries) != 2:
        return None

    older = entries[0]
    newer = entries[1]
    old_id = older.get("id", "?")
    new_id = newer.get("id", "?")
    old_content = older.get("content", "")
    new_content = newer.get("content", "")
    old_ts = older.get("timestamp", "?")
    new_ts = newer.get("timestamp", "?")

    prompt = (
        "你是记忆语义判定助手。以下是一对同主题的冷层记忆条目,"
        "新条目没有显式的否定词(如不/没/停止/取消),"
        "但可能表达了与旧条目相反/矛盾的偏好或事实。"
        "请判断新条目是否语义上否定了/替代了旧条目:\n\n"
        "[旧条目] 时间:" + old_ts + "\n"
        "ID:" + old_id + "\n"
        "内容:" + old_content + "\n\n"
        "[新条目] 时间:" + new_ts + "\n"
        "ID:" + new_id + "\n"
        "内容:" + new_content + "\n\n"
        "判定规则:\n"
        "1. 如果新条目表达的是与旧条目相反/矛盾的偏好/事实/结论"
        "(例如:旧说喜欢X新说改喜欢Y;旧说用方案A新说换成方案B;"
        "旧说启用新说已替换) -> reversal\n"
        "2. 如果新条目只是补充/细化/更新旧条目,"
        "不构成矛盾(例如:旧说喜欢X新说也喜欢Y;旧说配置A新说配置A改端口)"
        " -> not_reversal\n"
        "3. 如果两条描述不同的事实/对象/场景 -> not_reversal\n"
        "4. 如果拿不准 -> not_reversal(宁留不误删)\n"
        '5. 只输出 JSON: {"decision":"reversal"|"not_reversal",'
        '"older_id":"' + old_id + '","newer_id":"' + new_id + '",'
        '"reason":"一句话判据(中文)"}\n'
        "6. 不添加推断,不修改事实,不自由发挥"
    )

    result = _call_llm(prompt)
    if result is None:
        return None
    if "decision" not in result:
        return None
    result.setdefault("older_id", old_id)
    result.setdefault("newer_id", new_id)
    return result
