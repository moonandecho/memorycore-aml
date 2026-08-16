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


def classify_entry_type(content: str) -> str:
    """Entry type: "state" (historical decision / status record) | "rule" (precept).

    Phase 2 (2026-08-16): extracted from the completion-state detection as a
    single source of truth, shared by three call sites: store_fact write
    routing / on_memory_write direct-write governance / overflow reconcile.

    Conservative rule (all conditions must hold for "state"):
      0. pending/negation exclusion: 未定稿/待拍板 etc. are in-progress
         records, not completed ones -> rule
      1. contains a date (20xx-xx-xx)
      2. contains a completion marker (拍板/已配置/已停/已定稿 ...)
      3. contains no behavior/instruction marker (偏好/准则/禁止 ...)

    Both mis-type directions are data-safe:
      - state misjudged as rule -> entry stays, only compressed after 30d
      - rule misjudged as state -> sinks to cold tier after 7d, recallable
    So no LLM typing is needed; pure rules suffice.
    """
    import re

    if not content or not re.search(r"20\d\d-\d\d-\d\d", content):
        return "rule"
    # Pending/negation exclusion: "未定稿/待拍板" describe decisions still
    # open, not completed ones (probe-verified: 未定稿/未拍板/待拍板 all used
    # to be misjudged as state before this guard).
    pending_markers = ["未定稿", "未拍板", "未定案", "待定稿", "待拍板",
                       "待定案", "需用户拍板"]
    if any(m in content for m in pending_markers):
        return "rule"
    done_markers = ["拍板", "已配置", "已停", "已切换", "已退役", "退役",
                    "已装", "已加", "停用", "已删", "已完成",
                    "已重开", "已停用", "已清除",
                    # Phase 2 synonyms; bare "定稿" collides with 未定稿/
                    # 定稿前, so only the 已-prefixed form is kept
                    "已启用", "已禁用", "已定稿", "已定案"]
    behavior_markers = ["用户要求我", "用户偏好", "红线", "禁止", "习惯",
                        "行为准则", "用户纠正", "交互习惯", "写作风格",
                        "回答风格", "零容忍", "PM准则",
                        # preference/precept language always means rule
                        "准则", "偏好"]
    if (any(m in content for m in done_markers)
            and not any(m in content for m in behavior_markers)):
        return "state"
    return "rule"


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

    v3 (2026-08-09): 分级 keep — 强 keep (偏好/准则/红线/环境常量) 命中即留;
    弱 keep (必须/要求等通用指令词) 可被技术 sink 信号覆盖。
    判定顺序: 用户偏好前缀 → 强 keep 信号 → sink 组合 → 弱 keep → 默认。

    修复热层超载: 原版 keep_markers 含"必须/唯一/要求"等通用词,
    技术/环境记录 (GPU 方案/VS Code 栈/服务器内存等) 常含这些词被误留热层。
    新版分强/弱 keep 两级:
    - 强 keep (行为准则/红线/偏好/决策词): 命中即留, 无可覆盖
    - 弱 keep (通用指令词): 被强 sink 组合覆盖
    - 强 sink ≥2, 或 1 强 sink + 2 弱 sink, 或 ≥3 弱 sink → 下沉
    """
    # ---- 1. 用户偏好陈述启发式 (绝对留) ----
    user_pref_prefixes = [
        "用户喜欢", "用户偏好", "用户希望", "用户要求", "用户不喜欢",
        "用户习惯", "用户希望我", "用户要求我", "用户纠正", "用户明确",
        "用户对",
    ]
    for p in user_pref_prefixes:
        if content.startswith(p) or p in content[:25]:
            return True

    head30 = content[:30]
    if "用户对" in head30 and ("兴趣" in head30 or "偏好" in head30):
        return True

    # ---- 1.5 completion-state detection (2026-08-16): historical
    # decision/status records -> sink. Logic lives in classify_entry_type
    # (single source of truth, see above).
    if classify_entry_type(content) == "state":
        return False

    # ---- 3a. sink 词表定义 (步骤 2 的技术语境判定需要) ----
    # 强 sink: 明确的技术/环境/项目信号 (组合 ≥2 才沉, 防单个词误伤)
    # 已泛化: 移除生产版用户特化词 (具体硬件参数/脚本名/commit hash/个人服务),
    # 保留通用技术词
    sink_strong = [
        # 硬件/环境
        "GPU", "VS Code", "VSIX", "vscode", "SSD", "smartmontools",
        "LPDDR3", "BGA", "压测", "内核", "defconfig", "zram",
        "swap", "systemd", "sudoers",
        # 服务/推理
        "Ollama", "bge-m3", "llama-rerank", "sqlite3", "WAL", "rsync",
        # 端口/硬件资源
        "端口", "显存", "RSS",
        # 召回/向量/测试技术语境
        "SQLite", "持久化语义缓存", "语义缓存", "normalized query",
        "embedding", "向量", "召回", "rerank", "top-1", "单测", "e2e",
        "dense_score", "sentence_level", "importance",
        # MemoryCore 内部
        "overflow.py", "classifier", "maintenance", "QueryCache",
        "maintenance.py", "mem0", "Mem0",
    ]
    # 弱 sink: 单独命中不沉, 参与计数 (已泛化: MemoryCore→memorycore, Mnemosyne→cold tier,
    # 移除个人服务/用户特定路径等生产特化词)
    sink_weak = [
        "memorycore", "cold tier", "冷层", "热层", "锚点", "溢流",
        "服务器", "开源", "commit", "落地", "已完成", "已退役", "已停用",
        "SMB", "Tailscale", "ssh", "维护", "pip", "README",
        "cron 任务", "site-packages",
    ]

    # ---- 2. 强 keep 信号: 行为准则/红线/偏好/决策 (命中即留) ----
    # 特判 a: "偏好"若紧跟技术语境词 (偏好查询/偏好召回/偏好摘要/偏好锚点)
    #   → 不是用户偏好, 不视为强 keep
    # 特判 b: "绝不"若条目命中 ≥2 个强 sink (技术语境主导)
    #   → 可能只是被引用, 不视为强 keep
    strong_keep_markers = [
        "偏好", "准则", "红线", "零容忍", "绝不", "禁止", "原则",
        "习惯", "纠正", "行为准则", "交互习惯", "写作风格", "回答风格",
        "明确要求", "强制", "规范", "PM准则", "拍板", "决策",
        "环境常量",  # 开源版通用定位: 服务器环境常量需留本地
    ]
    _pref_tech_context = ("偏好查询" in content or "偏好召回" in content
                          or "偏好摘要" in content or "偏好锚点" in content)
    _s_strong_hits_for_keep = [kw for kw in sink_strong if kw in content]
    _tech_dominant = len(_s_strong_hits_for_keep) >= 2
    for kw in strong_keep_markers:
        if kw in content:
            if kw == "偏好" and _pref_tech_context:
                continue
            if kw == "绝不" and _tech_dominant:
                continue
            return True

    # ---- 3. sink 组合判定 ----
    s_strong_hits = [kw for kw in sink_strong if kw in content]
    s_weak_hits = [kw for kw in sink_weak if kw in content]
    if (len(s_strong_hits) >= 2
            or (len(s_strong_hits) >= 1 and len(s_weak_hits) >= 2)
            or len(s_weak_hits) >= 3):
        return False

    # ---- 4. 弱 keep 信号: 通用指令词 (无 sink 覆盖时留) ----
    weak_keep_markers = ["必须", "要求", "不能", "唯一", "不允许", "必须用"]
    for kw in weak_keep_markers:
        if kw in content:
            return True

    # ---- 5. 默认: 有 sink 信号 → 沉, 否则留 ----
    if s_strong_hits or s_weak_hits:
        return False
    return True


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
    # 通用信任信号词 (已泛化: 移除生产版用户私密词)
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
