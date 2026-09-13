#!/usr/bin/env python3
"""core/classifier.py — 冷热判定规则 (纯规则, 不调 LLM)

规则 (2026-08-03 定稿, 与 v2 分流流程一致):
1. importance >= 0.8 或命中热关键词 → 热 (留本地)
2. 命中过时标记 → 过时 (E2: forget 不迁移)
3. 否则 → 冷 (下沉 Mnemosyne)
混合条目 (既含高频偏好又含低频细节) → 建议拆开, 高频留本地低频下沉。
"""
from typing import Any, Dict, List, Optional, Tuple

import re

from .config import HOT_KEYWORDS, STALE_MARKERS
from . import config as _cfg
from .judge import judge_entry


def judge_engine_enabled() -> bool:
    """SAFE-JUDGE v3 是否生效 (JUDGE_V3_ENABLED + CLASSIFIER_V2_ENABLED 双门)。"""
    return bool(_cfg.JUDGE_V3_ENABLED and _cfg.CLASSIFIER_V2_ENABLED)

# 判定结果类型
HOT = "hot"
COLD = "cold"
STALE = "stale"
MIXED = "mixed"


# ---- 判型 v2 (2026-09-12 设计定稿, DESIGN.md Q1) ------------------------------
# 堵点: v1 只认前缀式完成态词 (事故/交付/调研记录落 rule), 且 "准则/偏好"
# 两个名词被一票否决 (技术语境 "重叠准则相似度≥0.62" 误伤治理记录)。
# v2: 补"词 + 结果/决定"约束模式 (不单字裸加); 名词行为信号可被技术语境
# (±8 字窗口) 消歧; 元数据人工标注 type_hint 优先。回滚: CLASSIFIER_V2_ENABLED=0。

# 结果/决定模式 (与现有 done_markers 并列; 设计 §Q1(1)):
STATE_DONE_RESULT_PATTERNS = [
    r"已(部署|上线|交付|恢复|找回|回退|关闭|下线)",
    r"(部署|上线|交付|调研|恢复|救回)(成功|完成|完毕)",
    r"四轮闭环交付|交付完成|部署完成|上线完成|调研完成|调研结论",
    r"决定不(做|用|接|碰|继续)|放弃跟进|不再考虑",
    r"改主意恢复|复制恢复|启动成功",
    r"(定位|状态|方案|结论)再确认",
]

# 强行为词 (v2 判型一票判 rule; v1 回滚仍使用旧表 _STRONG_BEHAVIOR_V1):
STRONG_BEHAVIOR = ["用户要求我", "用户要求", "用户希望", "用户喜欢",
                   "用户偏好", "红线", "禁止", "习惯",
                   "行为准则", "用户纠正", "交互习惯", "写作风格",
                   "回答风格", "零容忍", "最高准则",
                   "必须", "不允许", "不能",
                   "规范", "原则", "规则", "先确认"]
# v1 回滚目标保持 2026-08-16 词表 (评审 B-1 的 v2 扩展不进旧语义):
_STRONG_BEHAVIOR_V1 = ["用户要求我", "用户偏好", "红线", "禁止", "习惯",
                       "行为准则", "用户纠正", "交互习惯", "写作风格",
                       "回答风格", "零容忍", "最高准则"]

# 强行为词里的 "用户偏好/习惯/禁止" 是易子串误伤词, 由
# _strong_behavior_hits 做逐次出现的上下文护栏 (B-2); 其余词保持原语义。

# 强指令词 (B-1): 只在该类上下文中算行为信号, 避免历史记录里的叙述性
# "视觉必须用 X / 不能装 X" 等技术事实被误判 rule。
_STRONG_IMPERATIVE_RE = re.compile(
    r"(?:每次|完成|部署|上线|交付|恢复|变更|修改|发布|执行|操作|前|后|时)"
    r"[^。！？;；\n]{0,10}"
    r"(必须|不允许|不能)"
)
# "规范/原则/规则" 作为标题标签时才算强行为词; 正文里的 "规则初筛/写记忆规范"
# 这类技术名词不算。
_STRONG_LABEL_RE = re.compile(
    r"(?:^|[\n。；;]|20\d\d-\d\d-\d\d\s*)(规范|原则|规则)\s*[:：]"
)
# "先确认" 本身是强交互准则 (B-1 建议词)。
_STRONG_DIRECT_WORDS = ("先确认",)

# 完成态命中后的尾随护栏 (B-1): 完成态词被用作时间/条件状语时不得计为
# 历史完成态 ("交付完成前必须先确认 / 部署完成后必须验证 / 已上线功能修改
# 必须先回退" 都是行为规则, 不是状态记录)。
_COMPLETION_CONDITIONAL_TAIL = re.compile(
    r"(?:必须|必须先|先确认|先验证|先回退|先通知|不允许|不能|才能|请|要求|通知)"
)
_COMPLETION_FUTURE_PREFIX = re.compile(r"^\s*(前|后)")

# 名词"准则/偏好"默认算行为信号, 但命中下列技术语境窗口时该次出现不计:
NOUN_TECH_CONTEXT = ["相似度", "命中", "召回", "锚点", "词表", "判型",
                     "分类", "阈值", "机制", "审计", "索引", "向量",
                     "embedding", "摘要", "溢流", "维护", "候选", "退役词"]

# 未完成态词 (B-1 补齐): "待上线/未交付/未完成/进行中" 等不得进入 state。
_PENDING_MARKERS = ["未定稿", "未拍板", "未定案", "待定稿", "待拍板",
                    "待定案", "需用户拍板",
                    "待上线", "待部署", "待交付", "待恢复",
                    "未交付", "未完成", "尚未完成", "进行中"]

_DONE_MARKERS = ["拍板", "已配置", "已停", "已切换", "已退役", "退役",
                 "已装", "已加", "停训", "停用", "已删", "已完成",
                 "半搬", "已重开", "已停用", "已清除",
                 "已启用", "已禁用", "已定稿", "已定案"]

_NOUNS = ("准则", "偏好")


def _completion_hit_is_conditional(content: str, m: "re.Match") -> bool:
    """完成态命中是否为时间/条件状语 (B-1 护栏), 而非历史完成态记录。

    True = 该次命中不算完成态证据。规则:
      - 完成态词后紧跟 "前/后" (交付完成前/部署完成后/恢复成功后) → 条件句;
      - 完成态词后 24 字内出现 必须/必须先/先确认/先验证/先回退/先通知/
        不允许/不能/才能/请/要求/通知 等指令或后续动作词
        ("已上线功能修改必须先回退" / "已部署环境变更前必须回退")。
    """
    tail = content[m.end():m.end() + 24]
    if not tail:
        return False
    if _COMPLETION_FUTURE_PREFIX.search(tail):
        return True
    return bool(_COMPLETION_CONDITIONAL_TAIL.search(tail))


def _strong_behavior_hits(content: str) -> List[str]:
    """v2 强行为词命中 (B-1 扩展 + B-2 技术对象护栏)。

    与旧 any(substring) 的区别:
      - 用户偏好设置 / 旧习惯 里的子串不计;
      - 调研结论里的技术性"禁止"(技术原因/技术限制)不计;
      - 必须/不能/不允许 只在指令上下文计;
      - 规范/原则/规则 只认标题标签 (句首或日期前缀 + 冒号);
      - 先确认 与用户要求/希望/喜欢等前缀保持一票命中。
    """
    hits: List[str] = []
    if not content:
        return hits
    for kw in STRONG_BEHAVIOR:
        if kw in _STRONG_DIRECT_WORDS:
            if kw in content:
                hits.append(kw)
            continue
        if kw in ("必须", "不允许", "不能"):
            if kw in content and _STRONG_IMPERATIVE_RE.search(content):
                hits.append(kw)
            continue
        if kw in ("规范", "原则", "规则"):
            for lm in _STRONG_LABEL_RE.finditer(content):
                if lm.group(1) == kw:
                    hits.append(kw)
                    break
            continue
        if kw == "用户偏好":
            active = False
            for m in re.finditer("用户偏好", content):
                tail = content[m.end():m.end() + 2]
                if tail == "设置":
                    continue  # "用户偏好设置" = 技术对象
                active = True
                break
            if active:
                hits.append(kw)
            continue
        if kw == "习惯":
            active = False
            for m in re.finditer("习惯", content):
                if m.start() > 0 and content[m.start() - 1] == "旧":
                    continue  # "旧习惯" = 技术对象
                active = True
                break
            if active:
                hits.append(kw)
            continue
        if kw == "禁止":
            active = False
            for m in re.finditer("禁止", content):
                pre = content[max(0, m.start() - 20):m.start()]
                post = content[m.end():m.end() + 24]
                if "调研结论" in pre and ("技术原因" in post or "技术限制" in post):
                    continue  # 调研结论中的技术限制, 不是行为准则
                active = True
                break
            if active:
                hits.append(kw)
            continue
        if kw in content:
            hits.append(kw)
    return list(dict.fromkeys(hits))


def _noun_tech_disambiguated(content: str, noun: str) -> bool:
    """名词 '准则/偏好' 的每次出现是否全部被技术语境消歧 (±8 字窗口)。

    True = 全部出现均落在技术语境窗口内 → 该名词不算行为信号;
    False = 至少一次出现无技术语境 → 行为信号成立 (或名词未出现, 无信号)。
    """
    idxs = [m.start() for m in re.finditer(re.escape(noun), content or "")]
    if not idxs:
        return False
    for i in idxs:
        window = content[max(0, i - 8): i + len(noun) + 8]
        # B-2: 技术对象里的名词也不算行为信号 (用户偏好设置 / 旧习惯)
        if noun == "偏好" and ("用户偏好设置" in window or "偏好设置" in window):
            continue
        if noun == "准则" and ("规则初筛" in window or "退役词" in window):
            continue
        if not any(tc in window for tc in NOUN_TECH_CONTEXT):
            return False
    return True


def _classify_entry_type_v1(content: str) -> str:
    """旧词法判型 (Phase 2, 2026-08-16) — CLASSIFIER_V2_ENABLED=0 的回滚目标。"""
    import re

    if not content or not re.search(r"20\d\d-\d\d-\d\d", content):
        return "rule"
    if any(m in content for m in _PENDING_MARKERS):
        return "rule"
    behavior_markers = list(_STRONG_BEHAVIOR_V1) + list(_NOUNS)
    if (any(m in content for m in _DONE_MARKERS)
            and not any(m in content for m in behavior_markers)):
        return "state"
    return "rule"


def _classify_entry_type_v2(content: str) -> Dict[str, Any]:
    """v2 判型主体: 返回 {"type": ..., "signals": {...}} (供 detail 复用)。"""
    import re

    signals: Dict[str, Any] = {
        "has_date": bool(content and re.search(r"20\d\d-\d\d-\d\d", content)),
        "pending_hits": [m for m in _PENDING_MARKERS if m in content],
        "done_hits": [],
        "result_pattern_hits": [],
        "strong_behavior_hits": [],
        "noun_active": [],
        "noun_tech_disambiguated": [],
    }
    if not content or not signals["has_date"]:
        signals["strong_behavior_hits"] = _strong_behavior_hits(content)
        return {"type": "rule", "signals": signals}
    # F4+B1: 否定/未完成态前缀排除优先于一切完成态判定
    if signals["pending_hits"]:
        signals["strong_behavior_hits"] = _strong_behavior_hits(content)
        return {"type": "rule", "signals": signals}

    # 完成态证据: 子串命中需通过"时间/条件状语"护栏 (B-1)。
    for marker in _DONE_MARKERS:
        for m in re.finditer(re.escape(marker), content):
            if _completion_hit_is_conditional(content, m):
                continue
            signals["done_hits"].append(marker)
            break
    for pat in STATE_DONE_RESULT_PATTERNS:
        for m in re.finditer(pat, content):
            if _completion_hit_is_conditional(content, m):
                continue
            signals["result_pattern_hits"].append(pat)
            break
    if not signals["done_hits"] and not signals["result_pattern_hits"]:
        signals["strong_behavior_hits"] = _strong_behavior_hits(content)
        return {"type": "rule", "signals": signals}

    signals["strong_behavior_hits"] = _strong_behavior_hits(content)
    if signals["strong_behavior_hits"]:
        return {"type": "rule", "signals": signals}
    for noun in _NOUNS:
        if noun in content:
            if _noun_tech_disambiguated(content, noun):
                signals["noun_tech_disambiguated"].append(noun)
            else:
                signals["noun_active"].append(noun)
    if signals["noun_active"]:
        return {"type": "rule", "signals": signals}
    return {"type": "state", "signals": signals}


# E-1: v2 主体冻结为 legacy 回滚目标 (JUDGE_V3_ENABLED=0 时调用)。
_classify_entry_type_v2_legacy = _classify_entry_type_v2


def classify_entry_type(content: str, type_hint: Optional[str] = None) -> str:
    """条目类型判定 (旧公开签名兼容): "state" | "rule"。

    v3 (SAFE-JUDGE, 2026-09-13): 调 core.judge.judge_entry;
      - decision=ambiguous 时公开类型映射为 rule (旧调用方安全, 防止误沉);
      - JUDGE_V3_ENABLED=0 → 回 v2; 此时 CLASSIFIER_V2_ENABLED=0 → v1。
    type_hint 人工标注永远优先 (state/rule 直接返回, 不解析)。
    """
    if type_hint in ("state", "rule"):
        return type_hint
    if judge_engine_enabled():
        return judge_entry(content).public_type
    if not _cfg.CLASSIFIER_V2_ENABLED:
        return _classify_entry_type_v1(content)
    return _classify_entry_type_v2_legacy(content)["type"]


def classify_entry_type_detail(content: str,
                               type_hint: Optional[str] = None) -> Dict[str, Any]:
    """判型 + 信号明细 (审计/测试; 判定口径与 classify_entry_type 一致)。

    v3 返回: type/public_type (state|rule, ambiguous→rule), decision
    (state|rule|ambiguous), band, confidence, signals, reason。
    """
    if type_hint in ("state", "rule"):
        return {"type": type_hint, "public_type": type_hint,
                "decision": type_hint, "band": "strong", "confidence": 1.0,
                "reason": f"type_hint={type_hint}",
                "signals": {"type_hint": type_hint}}
    if judge_engine_enabled():
        from .judge import judge_entry_detail
        return judge_entry_detail(content)
    if not _cfg.CLASSIFIER_V2_ENABLED:
        return {"type": _classify_entry_type_v1(content),
                "public_type": _classify_entry_type_v1(content),
                "decision": _classify_entry_type_v1(content),
                "band": "weak", "confidence": 0.5,
                "reason": "classifier_v1_rollback",
                "signals": {"classifier_v2_enabled": False}}
    out = _classify_entry_type_v2_legacy(content)
    out["signals"]["classifier_v2_enabled"] = True
    out["public_type"] = out["type"]
    out["decision"] = out["type"]
    out["band"] = "strong"
    out["confidence"] = 0.8
    out["reason"] = "classifier_v2_legacy"
    return out


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
    """溢流/审计热保留判定 (SAFE-JUDGE v3 适配器)。

    E-1: 先调 judge_entry — state→False (可下沉), ambiguous→True (留热层),
    rule→继续走既有 keep/sink 词法。旧回滚路径 (JUDGE_V3_ENABLED=0) 保持
    v2/v1 的 classify_entry_type state 否决。注意: rule 侧判据提取为
    should_keep_local_rule_view, 供 overflow 在"已有 sidecar rule 章"的
    语义下独立使用 (typed rule 的完成态改判走 S2 retype, 不绕过统计)。
    """
    try:
        if judge_engine_enabled():
            jr = judge_entry(content)
            if jr.decision == "state":
                return False
            if jr.decision == "ambiguous":
                return True
        elif _cfg.CLASSIFIER_V2_ENABLED:
            if classify_entry_type(content) == "state":
                return False
    except Exception:
        pass
    return should_keep_local_rule_view(content)


def should_keep_local_rule_view(content: str) -> bool:
    """
    溢流场景 rule 侧热保留判定 (比 classify 更严格, 只保留真正每轮要用的)。

    v2 分流判断: 行为准则 / 交互偏好 / 用户纠正 → 留本地;
    状态记录 / 历史决策 / 低频配置细节 / 服务器环境事实 → 可下沉。
    判定顺序: 用户偏好前缀 → 强 keep 信号 → sink 组合 → 弱 keep → 默认。

    v3 (2026-08-09): 修复热层超载 — 原版 keep_markers 含"必须/唯一/要求"
    等通用词, 技术/环境记录 (GPU 方案/VS Code 栈/服务器内存/Mac 硬件等)
    常含这些词被误留热层。新版分强/弱 keep 两级:
    - 强 keep (行为准则/红线/偏好/决策词): 命中即留, 无可覆盖
    - 弱 keep (通用指令词): 被强 sink 组合覆盖
    - 强 sink ≥2, 或 1 强 sink + 2 弱 sink, 或 ≥3 弱 sink → 下沉
    """
    # ---- 1. 用户偏好陈述启发式 (绝对留) ----
    user_pref_prefixes = [
        "用户喜欢", "用户偏好", "用户希望", "用户要求", "用户不喜欢",
        "用户习惯", "用户希望我", "用户要求我", "用户纠正", "用户明确",
        "用户对",  # 2026-08-03 补漏: "用户对自托管项目兴趣..."
    ]
    for p in user_pref_prefixes:
        if content.startswith(p) or p in content[:25]:
            if p == "用户偏好" and "用户偏好设置" in content[:25]:
                continue  # B-2: 用户偏好设置是技术对象, 不是用户偏好陈述
            return True

    head30 = content[:30]
    if "用户对" in head30 and ("兴趣" in head30 or "偏好" in head30):
        return True


    # ---- 3a. sink 词表定义 (步骤 2 的技术语境判定需要) ----
    # 强 sink: 明确的技术/环境/项目信号 (组合 ≥2 才沉, 防单个词误伤)
    sink_strong = [
        # 硬件/环境
        "GPU", "VS Code", "VSIX", "vscode", "SSD", "smartmontools",
        "LPDDR3", "BGA", "压测", "内核 6.12", "defconfig", "zram",
        "swap", "AP0512Z", "M4", "10核", "16GB 内存", "512GB",
        "磨损 0%", "备用块 100%", "写入 2.98TB", "通电 113h",
        # 服务/推理
        "Ollama", "bge-m3", "llama-rerank", "moss", "1737MiB",
        "q8_0", "F16", "keepalive",
        # 脚本/命令
        "backup-mnemosyne.sh", "sqlite3", "backup API", "WAL", "rsync",
        "sync-obsidian", "obsidian-rag", "systemd", "sudoers",
        "monitor_off", "monitor-guard", "adjust-brightness",
        "site-packages", "update-mnemosyne", "overflow.py", "classifier",
        "maintenance", "QueryCache", "vscode-fallback", "editor-fallback",
        "RemoteCommand", "tmux", "cron 任务", "update-mnemosyne.sh",
        "build-memtest", "双编译", "available 谷值", "mem0", "Mem0",
        # 版本/commit 标识
        "d4d295c", "51fbd34", "758c458", "c85dab1e", "forgotten=", "merged=",
        # 召回/向量/测试技术语境 (2026-08-09 补: 技术记录误留热层)
        "SQLite", "持久化语义缓存", "语义缓存", "normalized query",
        "embedding", "向量", "召回", "rerank", "top-1", "单测", "e2e",
        "dense_score", "sentence_level", "importance", "maintenance.py",
    ]
    # 弱 sink: 单独命中不沉, 参与计数
    sink_weak = [
        "MemoryCore", "Mnemosyne", "冷层", "热层", "锚点", "溢流",
        "服务器", "开源", "commit", "落地", "已完成", "已退役", "已停用",
        "端口", "显存", "RSS", "E盘", "SMB", "Tailscale", "ssh",
        "iCloud", "维护", "pip", "README",
    ]

    # ---- 2. 强 keep 信号: 行为准则/红线/偏好/决策 (命中即留) ----
    # 特判 (2026-08-09): 当条目命中 ≥2 个强 sink (技术语境主导, 如
    # MemoryCore 机制记录/commit 记录), "绝不"等被引用的强 keep 词
    # 可能只是修复记录里的关键词 (例: classifier 误伤修复记录引用
    # "绝不/拒绝/宁可"), 此时不以强 keep 保留。真决策词 (偏好/准则/
    # 拍板/红线/原则/习惯/纠正) 不受影响 — 它们是用户行为信号。
    # 用户偏好前缀已在步骤 1 绝对保留, 不受此影响。
    strong_keep_markers = [
        "偏好", "准则", "红线", "零容忍", "绝不", "禁止", "原则",
        "习惯", "纠正", "行为准则", "交互习惯", "写作风格", "回答风格",
        "明确要求", "强制", "规范", "最高准则", "拍板", "决策",
    ]
    # v2 同步 (2026-09-12): 裸名词"准则/偏好"特判与 classify_entry_type v2
    # 同源 — 技术语境 (±8 字窗口, NOUN_TECH_CONTEXT) 消歧该名词全部出现
    # 时才不算行为信号; 原 _pref_tech_context 只认"偏好查询/召回/摘要/锚点"
    # 四个短语且与 v2 口径冲突, 已删除 (单一真相源: _noun_tech_disambiguated)。
    _s_strong_hits_for_keep = [kw for kw in sink_strong if kw in content]
    _tech_dominant = len(_s_strong_hits_for_keep) >= 2
    for kw in strong_keep_markers:
        if kw in content:
            if kw in _NOUNS and _noun_tech_disambiguated(content, kw):
                continue  # v2: 名词出现全部在技术语境 → 不视为用户偏好
            if kw == "绝不" and _tech_dominant:
                continue  # 技术语境主导, "绝不"可能只是被引用
            return True

    # ---- 3. sink 组合判定 (词表在 3a 已定义) ----
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
            # 全多字词 (2026-08-06 移除单字 '宁'/'先'/'再':
            # '优先走' 类配置句曾被 '先' 误判 core; 短句交互准则
            # 仍由 '用户'/'必须'/'不要' 与步骤 1 的 '宁可'/'先确认' 覆盖)
            short_protect = ["用户", "必须", "不要", "宁可"]
            if any(kw in content for kw in short_protect):
                return "core"

    # ---- 步骤 1: 核心信号 (必留) ----
    # 行为指令词 (全多字词; 2026-08-06 移除单字 '绝'/'宁' 消除子串误伤,
    # 语义由 '绝不'/'拒绝'/'宁可'/'宁愿' 覆盖)
    cmd_words = ["必须", "禁止", "宁可", "宁愿", "不要", "忌", "红线",
                 "零容忍", "不允许", "拒绝", "不希望", "期望", "要求"]
    # 句式词 (全多字词; 移除单字 '先'/'再'/'才'/'只', 仅保留偏好语义明确的;
    # '优先' 也不收 — '优先级' 含 '优先', 会重现同类误伤; 交互准则由
    # interact_words 的 '先确认' 兜底)
    sent_words = ["宁可", "一律", "绝不"]
    # 交互准则词
    interact_words = ["大白话", "分层类比", "先确认", "汇报", "沟通",
                      "验证", "准确", "严谨", "覆盖"]
    # 身份信任词
    trust_words = ["抑郁", "信任", "相处", "尊重", "记忆外置"]

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
