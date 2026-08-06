#!/usr/bin/env python3
"""core/classifier.py — 冷热判定规则 (纯规则, 不调 LLM)

规则 (2026-08-03 定稿, 与 v2 分流流程一致):
1. importance >= 0.8 或命中热关键词 → 热 (留本地)
2. 命中过时标记 → 过时 (E2: forget 不迁移)
3. 否则 → 冷 (下沉 cold tier)
混合条目 (既含高频偏好又含低频细节) → 建议拆开, 高频留本地低频下沉。
"""
from typing import Dict, List, Tuple

from .config import HOT_KEYWORDS, STALE_MARKERS

# 判定结果类型
HOT = "hot"
COLD = "cold"
STALE = "stale"
MIXED = "mixed"


def classify(content: str, importance: float = 0.8, scope: str = "global") -> Dict[str, str]:
    """判定一条内容的冷热。

    Returns:
        {"decision": "hot"|"cold"|"stale", "reason": "..."}
    """
    if not content or not content.strip():
        return {"decision": COLD, "reason": "empty content"}

    # E2: 过时状态记录 → 不迁移 (直接 forget/删除)
    # 长度门禁: 只有短条目 (≤80 字) 含状态标记才算过时;
    # 长条目含状态词通常是混合记录 (如配置项里引用已停用服务),
    # 宁留本地不误删。
    if len(content) <= 80:
        for marker in STALE_MARKERS:
            if marker in content:
                return {"decision": STALE, "reason": f"stale marker '{marker}' (short entry)"}

    # 热: importance 高 或 命中热关键词
    if importance >= 0.8:
        return {"decision": HOT, "reason": f"importance={importance} >= 0.8"}

    for kw in HOT_KEYWORDS:
        if kw in content:
            return {"decision": HOT, "reason": f"hot keyword '{kw}'"}

    # 默认冷
    return {"decision": COLD, "reason": "low importance, no hot signals"}


def should_keep_local(content: str) -> bool:
    """溢流场景的热保留判定 (比 classify 更严格, 只保留真正每轮要用的)。

    v2 分流判断: 行为准则 / 交互偏好 / 用户纠正 / 环境常量 → 留本地;
    状态记录 / 历史决策 / 低频配置细节 → 可下沉。
    用户偏好表达多样 (不一定带关键词), 所以这里同时检查:
    - 明确的准则/偏好/纠正/常量关键词
    - 内容以"用户""我"开头的偏好陈述 (启发式)
    """
    # 准则/偏好/纠正/常量关键词 (比 HOT_KEYWORDS 更聚焦)
    keep_markers = [
        "偏好", "准则", "原则", "禁止", "必须", "习惯", "要求",
        "纠正", "拍板", "明确要求", "零容忍", "不允许",
        "行为准则", "交互习惯", "写作风格", "回答风格", "环境常量",
        # 用户红线/强规范词 (2026-08-03 补漏)
        "强制", "绝不", "不能", "红线", "硬约束", "唯一", "必须用",
    ]
    for kw in keep_markers:
        if kw in content:
            return True
    # 用户偏好陈述启发式: "用户喜欢/偏好/希望/要求/不喜欢" 开头
    user_pref_prefixes = [
        "用户喜欢", "用户偏好", "用户希望", "用户要求", "用户不喜欢",
        "用户习惯", "用户希望我", "用户要求我", "用户纠正", "用户明确",
        "用户对",  # 2026-08-03 补漏: "用户对自托管项目兴趣..."
    ]
    for p in user_pref_prefixes:
        if content.startswith(p) or p in content[:25]:
            return True
    # 用户偏好/兴趣陈述启发式: 内容首 30 字含 "用户对...兴趣" 组合
    head30 = content[:30]
    if "用户对" in head30 and ("兴趣" in head30 or "偏好" in head30):
        return True
    return False


def classify_user_pref(content: str, importance: float = 0.5,
                       *, sentence_level: bool = False) -> str:
    """USER.md 内容分类: 'core' (留本地) | 'sink' (进冷层) | 'stale' (过时)。

    sentence_level=False (条目级, A 写入分流用): 保守, 含 importance/STALE 判定
    sentence_level=True  (句子级, B 溢流拆分用): 更敏感, 宁留勿沉, 不做 stale

    判定顺序 (两粒度共用):
    1. 核心信号 (必留): 行为指令/句式/交互准则/身份信任词 + importance>=0.8(仅条目级)
    2. 用户前缀启发 (必留): 开头 25 字内含用户偏好陈述
    3. STALE (仅条目级+仅短条目): <=80字 含状态词 → stale
    4. 默认 sink: 以上都不中 → 长尾可沉
    """
    if not content or not content.strip():
        return "sink"

    content = content.strip()

    # ---- 句子级额外保护 (在步骤 1 前, 句子级专属) ----
    if sentence_level:
        if len(content) < 15:
            # 短句保护用多字词, 单字 宁/先/再 会子串误伤
            # (如 优先走 类配置句被判 core); 交互准则由
            # 用户/必须/不要 与核心信号的 宁可/先确认 覆盖
            short_protect = ["用户", "必须", "不要", "宁可"]
            if any(kw in content for kw in short_protect):
                return "core"

    # ---- 步骤 1: 核心信号 (必留) ----
    # 行为指令词 (全多字词; 单字 绝/宁 会子串误伤, 语义由
    # 绝不/拒绝/宁可/宁愿 覆盖)
    cmd_words = ["必须", "禁止", "宁可", "宁愿", "不要", "忌", "红线",
                 "零容忍", "不允许", "拒绝", "不希望", "期望", "要求"]
    # 句式词 (全多字词; 单字 先/再/才/只 会子串误伤,
    # 优先 也不收 — 优先级 含 优先 会重现同类误伤;
    # 交互准则由 interact_words 的 先确认 兜底)
    sent_words = ["宁可", "一律", "绝不"]
    # 交互准则词
    interact_words = ["大白话", "分层类比", "先确认", "汇报", "沟通",
                      "验证", "准确", "严谨", "覆盖"]
    # 身份信任词
    trust_words = ["信任", "尊重"]

    all_core = cmd_words + sent_words + interact_words + trust_words
    # 去重 ("宁"/"先"/"再" 可能出现在多个列表)
    seen = set()
    all_core_dedup = []
    for w in all_core:
        if w not in seen:
            seen.add(w)
            all_core_dedup.append(w)

    for kw in all_core_dedup:
        if kw in content:
            return "core"

    # importance >= 0.8 (仅条目级; 句子级不看 importance)
    if not sentence_level and importance >= 0.8:
        return "core"

    # ---- 步骤 2: 用户前缀启发 (必留) ----
    user_prefixes = [
        "用户喜欢", "用户偏好", "用户希望", "用户要求", "用户不喜欢",
        "用户习惯", "用户纠正", "用户明确",
    ]
    head25 = content[:25]
    for p in user_prefixes:
        if p in head25:
            return "core"
    # "用户对...兴趣" 组合
    if "用户对" in head25 and ("兴趣" in head25 or "偏好" in head25):
        return "core"

    # ---- 步骤 3: STALE (仅条目级 + 仅短条目 <=80 字) ----
    # 句子级不判 stale; 长条目含状态词不判 stale (混合记录保护)
    if not sentence_level and len(content) <= 80:
        stale_markers = ["已修复", "已解决", "已切换", "已完成",
                         "已退役", "已停用", "不再使用"]
        for marker in stale_markers:
            if marker in content:
                return "stale"

    # ---- 步骤 4: 默认 sink (长尾可沉) ----
    return "sink"


def split_mixed(content: str) -> Tuple[List[str], List[str]]:
    """混合条目拆分: 返回 (hot_parts, cold_parts)。

    按句子切分 (。！？;换行), 逐句判定。纯启发式, 不做语义理解。
    """
    import re

    sentences = [s.strip() for s in re.split(r"[。！？;；\n]", content) if s.strip()]
    hot_parts, cold_parts = [], []
    for s in sentences:
        d = classify(s, importance=0.5)  # 句子级用默认低 importance
        if d["decision"] == HOT:
            hot_parts.append(s)
        else:
            cold_parts.append(s)
    return hot_parts, cold_parts
