#!/usr/bin/env python3
"""core/recall_fusion.py — P2 预注册召回融合内核（治理层 + prefetch 插件共用）

**为什么单独成模块（2026-09-18）**：prefetch 插件为避免 import `server` 的副作用，
过去自己拼了一条召回链（直连冷层 → 自己的非有限收口 → `_apply_decay`），
于是治理层的排序改进（RRF 融合）永远到不了「每轮注入」这条真实流量路径 ——
这正是「副本会漂移」的教科书案例（历史已漂移两次：嵌入通道写死 ollama、
非有限分数收口要单独补丁）。本模块只依赖 `core.decay`，无 server 副作用，
两条路径共用同一份实现。

**预注册超参（DESIGN-P2.md §1.3）：不得因 dev/holdout 结果调参。**

开关语义（2026-09-18 起默认开）：未设置 / 空串 / 纯空白 → 开；
`0/false/no/off`（任意大小写、首尾空白）→ 关；其余值（含拼写错误、随机串）→ 开。
"""
from __future__ import annotations

import os
import re

from .decay import _apply_decay

# ---- 开关与预注册超参 ------------------------------------------------------
FUSION_ENV = "MEMORYCORE_RECALL_FUSION"
CANDIDATE_K_ENV = "MEMORYCORE_RECALL_FUSION_CANDIDATE_K"
# 显式关闭白名单（默认开之后：只有这些值关闭融合）
OFF_VALUES = ("0", "false", "no", "off")
CANDIDATE_K_DEFAULT = 30
CANDIDATE_K_MAX = 50
RRF_K = 5
W_ENGINE = 1.0
W_DECAY = 1.5
W_LEX = 0.25
LEX_PRODUCT_LIMIT = 200_000

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_LATIN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_.:/#\-]*")
_DATE_RE = re.compile(r"\d{4}[-/年]\d{1,2}(?:[-/月]\d{1,2}日?)?")
_DIGIT2_RE = re.compile(r"\d{2,}")


def fusion_enabled() -> bool:
    """融合默认开；仅显式白名单假值关闭（2026-09-18 闸门判定后改默认）。

    判定依据（服务器 PREREGISTRATION-v7-rrf.md / RRF-V7-VALIDATION-REPORT.md）：
    未参与选参的 v7 闸门块（153 正样本 / 79 负控）hit@5 **+10.91pp**，
    配对 McNemar 精确 p=0.0227（24 组不一致），MRR +0.086，负控误返率不变，
    每次召回 p50 +4.3ms，无新模型/服务/依赖；30% 封存块同向（+9.30pp，无回退）。
    关闭 = 回到判定前的原召回路径（逐字节旧行为，由 golden 回归测试钉住）。

    取值口径（默认开之后）：未设置 / 空串 / 纯空白 → 开；
    0/false/no/off（任意大小写与首尾空白）→ 关；
    其余值（含拼写错误与随机串）→ 开（默认即开；见发布说明）。
    """
    raw = os.environ.get(FUSION_ENV)
    if raw is None:
        return True
    value = raw.strip().lower()
    if not value:
        return True
    return value not in OFF_VALUES


def candidate_k() -> int:
    """预注册 candidate_k 默认 30；允许 env 覆盖，硬上限 50。"""
    raw = os.environ.get(CANDIDATE_K_ENV)
    try:
        value = (int(raw) if raw is not None else CANDIDATE_K_DEFAULT)
    except (TypeError, ValueError):
        value = CANDIDATE_K_DEFAULT
    if value < 1:
        value = 1
    return min(value, CANDIDATE_K_MAX)


def _longest_common_cjk(a, b) -> int:
    """LCS 项的公共连续中文字串长度（只统计 CJK，与预注册公式一致）。"""
    aa = "".join(_CJK_RE.findall(a or ""))
    bb = "".join(_CJK_RE.findall(b or ""))
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


def _normalize_date(token: str) -> str:
    return re.sub(r"[年月/]", "-", token).rstrip("日")


def lex_score(query, content) -> float:
    """DESIGN-P2 §1.2 预注册词法/实体弱特征（只读，不调用冷层）。

    非字符串 query/content 一律按无词法证据处理（score=0），不让单条
    坏数据把整条融合召回变成报错/空结果。
    """
    q = query if isinstance(query, str) else ""
    c = content if isinstance(content, str) else ""
    cl = c.lower()
    score = 0.0
    for token in _LATIN_RE.findall(q):
        if len(token) < 2:
            continue
        tl = token.lower()
        if token.isalpha():
            if re.search(r"(?<![a-z0-9_])" + re.escape(tl) +
                         r"(?![a-z0-9_])", cl):
                score += 0.4
        elif tl in cl:
            score += 1.0
    if set(_DIGIT2_RE.findall(q)) & set(_DIGIT2_RE.findall(c)):
        score += 0.7
    q_dates = {_normalize_date(x) for x in _DATE_RE.findall(q)}
    c_dates = {_normalize_date(x) for x in _DATE_RE.findall(c)}
    if q_dates & c_dates:
        score += 1.0
    if len(q) * len(c) <= LEX_PRODUCT_LIMIT:
        common = _longest_common_cjk(q, c)
        if common >= 3:
            score += min(0.5 * (common - 2), 1.5)
    return round(score, 6)


def fuse_candidates(results, query, top_k):
    """对候选池做三路 RRF 融合，返回截断到 top_k 的原候选 dict 列表。

    三路：R_eng=冷层返回序；R_dec=`_apply_decay` 后 final_score 序；
    R_lex=本地预注册词法证据序（仅正证据）。只读，不调冷层/不落盘/不扩候选。

    名次 key 是候选在 `pool` 中的位置（行身份），不是 `id`。因此重复 id
    各自保留独立名次，缺失 id 的行同样按独立候选参与，不会被 dict 折叠。
    """
    pool = list(results or [])
    if not pool:
        return []
    n = len(pool)
    # R_eng: 冷层返回序 (1-based, 位置即身份).
    engine_rank = list(range(1, n + 1))

    # R_lex: 仅正词法证据参与; 同分以 engine rank 稳定决胜。
    lex_scores = [lex_score(query, row.get("content")) for row in pool]
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
        score = (W_ENGINE / (RRF_K + engine_rank[pos])
                 + W_DECAY / (RRF_K + decay_rank.get(pos, n + 1)))
        if pos in lex_rank:
            score += W_LEX / (RRF_K + lex_rank[pos])
        scored.append((score, engine_rank[pos], pos))
    scored.sort(key=lambda item: (-item[0], item[1]))
    limit = max(0, int(top_k))
    return [pool[pos] for _, _, pos in scored[:limit]]
