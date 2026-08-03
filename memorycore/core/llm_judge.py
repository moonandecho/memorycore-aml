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

from .config import LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_TIMEOUT


def _call_llm(prompt: str) -> Optional[Dict[str, Any]]:
    """Call the LLM, return the parsed JSON dict. None on any failure."""
    if not LLM_API_KEY:
        return None

    try:
        import urllib.request

        payload = {
            "model": LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 1000,
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
        # Strip markdown code fences
        content = re.sub(r"^```(json)?|```$", "", content, flags=re.M).strip()
        return json.loads(content)
    except Exception:
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
    """Judge whether long entries (>80 chars) with "落地中/进行中" markers
    are actually stale (the in-progress matter has completed).

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
        "你是记忆过时判定助手。以下是 1-5 条冷层记忆条目, 包含\"落地中/进行中\"等 "
        "进行时状态标记, 但这些条目较长 (>80字), 可能只是部分内容过时。"
        "请判断这些条目中\"进行时\"部分描述的事项是否已实际完成/过时:\n\n"
        + "\n\n".join(items)
        + "\n\n判定规则:\n"
        "1. 如果\"落地中/进行中\"描述的事项显然已经完成 (例如提到的时间已过、"
        "引用的版本已升级、关联的项目已完成) → stale\n"
        "2. 如果无法确定是否完成、或只是背景信息中偶尔出现进行时词 → not_stale\n"
        "3. 如果拿不准 → not_stale (宁留不误删)\n"
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
