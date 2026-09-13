#!/usr/bin/env python3
"""core/judge.py — SAFE-JUDGE v3: 机制级判型 (纯规则, 零 LLM, 零网络, O(n))

设计口径: JUDGE-DESIGN.md §Q-A ~ §Q-D / §Q-E.

核心公式:
    SAFE-JUDGE v3 = 子句/嵌入面解析 + 言语行为标签 + 体貌谓词
                    + 作用域消解 + 模糊带安全路由

开放域事件动词 (部署/恢复/调研/迁移/通知/...) 只填"谓词槽", 不参与判定;
判定只依赖有限闭类算子/标签:
  - 体貌: 已/已经/曾/刚, VP 了/过, 完成/成功/完毕/结束, 拍板/决定/再确认...
  - 未完成: 待X/未X/尚未/正在/进行中/计划/打算
  - 情态: 必须/应当/不得/严禁/禁止/不要/绝不 vs 不能/无法/不允许/失败/异常
  - 言语行为标签: NORM 闭类 / REPORT 闭类
  - 作用域: 顶层 vs 逗号子句, 引号/括号/箭头派生, 条件触发 X前/后/时

对外:
    JudgeResult(decision, public_type, confidence, band, signals, reason)
    judge_entry(content, type_hint=None) -> JudgeResult
    judge_entry_detail(content, type_hint=None) -> dict
    classify_entry_type(...) 在 classifier.py 适配; ambiguous -> rule
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config as _cfg


@dataclass
class JudgeResult:
    """判型结果 (三值 decision + 二值 public_type 兼容)。"""
    decision: str          # "state" | "rule" | "ambiguous"
    public_type: str       # "state" | "rule"; ambiguous -> "rule"
    confidence: float      # 0..1
    band: str              # "strong" | "weak" | "ambiguous"
    signals: Dict[str, Any] = field(default_factory=dict)
    reason: str = ""


# ===========================================================================
# 0. 闭类算子 / 标签 (注释: 闭类, 不是领域词表; 新增领域动词无需改本文件)
# ===========================================================================

_DATE_RE = re.compile(r"20\d\d[-/.]\d{1,2}[-/.]\d{1,2}")

_TOP_SEPS = set("。！？!?；;.\n")
_SUB_SEPS = set("，,、")
_QUOTE_OPEN = {'"': '"', "'": "'", "“": "”", "‘": "’",
               "「": "」", "『": "』", "`": "`"}
_PAREN_OPEN = set("（(【[｛{")
_PAREN_CLOSE = set("）)】]｝}")
_PAREN_PAIRS = {"（": "）", "(": ")", "【": "】", "[": "]", "｛": "｝",
                "{": "}"}

# 体貌算子: 完成/过去
_PERFECT_ALREADY_RE = re.compile(r"(?<!而)已(?=[^\s，。；;！？!?、:：])")
_PERFECT_ZENG_RE = re.compile(r"曾(?=[^\s，。；;！？!?、])")
# 体貌算子: 完成/过去.
# A-5: 刚后不维护动作动词白名单 — 任何"刚+谓词"都按完成体填充槽;
# 如需排除技术名词，按结构而不是领域动词追加闭类规则.
_PERFECT_GANG_RE = re.compile(r"刚(?=[\u4e00-\u9fa5A-Za-z0-9])")
# D-1 VP了: 排除封闭语法词 为了/除了/经过/超过/不得已/难得; 但"通过了/
# 审核通过了"中的"通"是谓词填充, 不能因为 lookbehind 命中 通 就整类漏判.
_PERFECT_LE_RE = re.compile(
    r"(?<![为除经超不难得])[\u4e00-\u9fa5A-Za-z0-9]了(?=[，,。；;！!？?\s]|$)")
_PERFECT_GUO_RE = re.compile(
    r"(?<![经通超不难得])[\u4e00-\u9fa5A-Za-z0-9]过(?=[，,。；;！?\s]|$)")
_RESULT_COMPLEMENT_RE = re.compile(r"(?:完成|成功|完毕|结束)")
_RESULT_NEG_PRE = ("未", "尚未", "没", "还没", "没有", "无", "不能", "无法",
                   "是否", "未完全", "没能", "难以")
# 结果承诺谓词 (A-3 STATE_EVENT)
_COMMITMENT_RE = re.compile(
    r"(?<![不非])"
    r"(?:拍板|决定不(?:做|用|接|碰|继续)|放弃跟进|改主意|再确认|拒绝|放弃)")
# 兼容 v2 结果模式里的"决定做/决定采用"等
_COMMITMENT2_RE = re.compile(r"决定(?:不|要|做|用|接|碰|继续|采用|选择|放弃)")

# A-5: 未来/计划闭类算子 (结构槽, 不是领域动作词表). "拟/将" 加邻接护栏,
# 避免把 模拟/虚拟/将军/将近/麻将 这类名词当未来标记.
_FUTURE_PLAN_RE = re.compile(
    r"(?:计划|打算|预计|即将|将要|(?<![模虚])拟|"
    r"(?<![麻])将(?![军来近就领]))")
# 计划消解跨度: 计划/预计/即将 + ≤12 字无标点内容 + 结果谓词整体标掉,
# 使其不单独作为历史完成证据 (P01–P06). 例: 计划完成部署 / 预计下周完成年检.
_FUTURE_COMPLETION_SPAN_RE = re.compile(
    r"(?:计划|打算|预计|即将|将要|(?<![模虚])拟|(?<![麻])将(?![军来近就领]))"
    r"(?:于|在|从|自)?[^，。；;！？!?\n]{0,12}?"
    r"(?:完成|成功|完毕|结束|通过|签约|部署|上线|交付|迁移|恢复|审批|验收)")

# A-5/D-3: 条件句触发 = 设计闭类 (完成|成功|完毕|结束) + 时/前/后,
# 不维护动作动词白名单; 但也不把任意 "X后" 都升格为未来条件 (历史叙述
# "卸载旧驱动后重装" 必须继续按完成态流水处理).
_COND_TRIGGER_RE = re.compile(
    r"(?:完成|成功|完毕|结束)(?:之前|之后|前后|前|后|时)")
# D-2 观察框架 (symptom 专用): 结构式 [动作短语 2..12 字] + 时/前/后.
# 这里允许任何动作动词, 因为只用于"能力否定+观察时点"的症状判定;
# 时间副词 以后/之后/目前/同时/及时 由后缀 lookbehind 排除.
_OBSERVED_FRAME_RE = re.compile(
    r"[\u4e00-\u9fa5A-Za-z0-9]{2,12}"
    r"(?:之前|之后|前后|(?<![以之今随然最背])后|(?<![目之当从以眼])前|"
    r"(?<![同及暂顿小随平有])时)(?!候|间|刻)")
# 未完成/待办算子 (闭类体貌算子 + 泛化计划/将来标记).
_PENDING_RE = re.compile(
    r"(?:待(?:上线|部署|交付|恢复|定稿|拍板|定案|确认|办|处理|迁移|发布|"
    r"开始|执行|验证|审批|回复|接|做|用|装|更新|升级|测试|调研|完成)|"
    r"未(?:完成|交付|定稿|拍板|定案|确认|迁移|上线|部署|解决|修复)|"
    r"尚未|正在|进行中|计划|打算|预计|即将|将要|(?<![模虚])拟|"
    r"(?<![麻])将(?![军来近就领]))")
_NEGATED_PENDING_RE = re.compile(
    r"(?:无|没有|不存在)\s*待(?:上线|部署|交付|恢复|定稿|拍板|定案|确认|办|"
    r"处理|迁移|发布|开始|执行|验证|审批|回复|接|做|用|装|更新|升级|测试|"
    r"调研|完成)|"
    r"未完成项(?:[^，。；;！？!?\n]{0,10})(?:清单|列表|项)?\s*(?:为\s*空|为零|不存在)")
# 道义情态闭类 (严格情态; 不能/不允许/无法属能力否定)
_DEONTIC_RE = re.compile(
    r"(?:必须|务必|应当|应该|不得|不准|严禁|禁止|不要|绝不|切勿|切忌|"
    r"(?<![特分个])别(?![的])|应(?=当|该))")
# 能力/结果否定闭类
_ABILITY_RE = re.compile(r"(?:不能|无法|不允许|失败|报错|异常|不可用|打不开|进不去)")
# 全称/频率算子
_QUANTIFIER_RE = re.compile(r"(?:每次|一律|任何|始终|总是|永远|从不|凡是)")
# 言语行为/态度谓词闭类 (A-3 RULE_SPEECH)
_SPEECH_RE = re.compile(
    r"(?:用户|PM)?(?:要求|希望|偏好|喜欢|习惯|纠正|期望|零容忍)")
# 技术对象: 其中的谓词是命名对象, 不是言语行为 (B-2)
_TECH_SPEECH_RE = re.compile(r"(?:用户偏好设置|偏好设置|旧习惯|期望值|高于期望)")


# ===========================================================================
# 1. 解析器: 子句 / 嵌入面 / 标签 / 作用域
# ===========================================================================

@dataclass
class Clause:
    text: str
    index: int
    top: bool = True          # 顶层句段 vs 逗号子句
    derived: bool = False     # 箭头 / 因此 / 所以 后的派生子句
    sep_before: str = ""
    quoted_head: bool = False


def _strip_leading_date(text: str) -> str:
    t = (text or "").lstrip()
    m = _DATE_RE.match(t)
    return t[m.end():].lstrip() if m else t


def _mask_spans(text: str, *, quotes: bool = True, parens: bool = True) -> str:
    """把引号/括号跨度替换为等长空格 (保持长度, 根级判定只看裸露文本)。"""
    if not text:
        return ""
    out = list(text)
    i, n = 0, len(text)
    quote_close = None
    depth = 0
    while i < n:
        ch = text[i]
        if quote_close is not None:
            out[i] = " "
            if ch == quote_close:
                quote_close = None
            i += 1
            continue
        if quotes and ch in _QUOTE_OPEN:
            out[i] = " "
            quote_close = _QUOTE_OPEN[ch]
            i += 1
            continue
        if parens:
            if ch in _PAREN_OPEN:
                depth += 1
                out[i] = " "
                i += 1
                continue
            if depth > 0:
                if ch in _PAREN_CLOSE:
                    depth -= 1
                out[i] = " "
                i += 1
                continue
        i += 1
    return "".join(out)


def _split_units(content: str) -> List[Clause]:
    """扫描全文 → 顶层句段 + 逗号子句 + 派生边界。引号/括号内不切分。"""
    segs: List[Clause] = []
    buf: List[str] = []
    sep_before = ""
    quote_close = None
    depth = 0
    top = True

    def flush() -> None:
        nonlocal buf, sep_before, top
        raw = "".join(buf).strip()
        if raw:
            segs.append(Clause(text=raw, index=len(segs),
                               top=top, sep_before=sep_before))
        buf = []

    for ch in content or "":
        if quote_close is not None:
            buf.append(ch)
            if ch == quote_close:
                quote_close = None
            continue
        if ch in _QUOTE_OPEN:
            quote_close = _QUOTE_OPEN[ch]
            buf.append(ch)
            continue
        if ch in _PAREN_OPEN:
            depth += 1
            buf.append(ch)
            continue
        if ch in _PAREN_CLOSE:
            depth = max(0, depth - 1)
            buf.append(ch)
            continue
        if depth == 0 and ch in _TOP_SEPS:
            flush()
            sep_before = ch
            top = True
            continue
        if depth == 0 and ch in _SUB_SEPS:
            flush()
            sep_before = ch
            top = False
            continue
        buf.append(ch)
    flush()

    # 派生边界: → / 因此 / 所以 (只把右半边标 derived)
    out: List[Clause] = []
    for c in segs:
        parts = re.split(r"(?=因此|所以)|→", c.text)
        if len(parts) <= 1:
            out.append(c)
            continue
        for j, p in enumerate(parts):
            p = (p or "").strip()
            if not p:
                continue
            cc = Clause(text=p, index=len(out), top=c.top,
                        derived=(j > 0) or c.derived,
                        sep_before=(c.sep_before if j == 0 else "→"))
            out.append(cc)
    for i, c in enumerate(out):
        c.index = i
    return out


_NORM_LABELS = [
    "零容忍", "最高指令", "最高准则", "行为准则", "交互习惯", "写作风格",
    "回答风格", "管理准则", "铁律", "红线", "指令", "约定", "准则",
    "规范", "原则", "规则", "要求", "希望", "偏好", "喜欢", "习惯",
    "纠正",
]
_REPORT_LABELS = [
    "结论", "记录", "状态", "结果", "调研",
    "定位", "自述", "完成", "交付", "部署", "上线", "恢复", "迁移",
    "明确", "决定", "拍板",
]
# D-4: label 只能是闭类标签本身, 或明确的"主题+标签"复合模式。
# NORM 侧承担安全方向 (误判 rule 只多留热层), 因此允许主题前缀 + NORM 闭类词尾。
_NORM_COMPOUND_RE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9_\- ]{1,18}"
    r"(?:要求|希望|偏好|喜欢|习惯|纠正|期望|零容忍|准则|规范|原则|规则|"
    r"铁律|红线|指令|约定)$")
# REPORT 侧误判会直接冷迁, 复合模式只放行设计明列的"调研结论"以及
# "主题+调研" 这类报告类主题 (如 示例项目自动化调研); 迁移记录/部署状态
# 这类主题词不是报告标签, 必须落回正文继续判 root 证据。
_REPORT_COMPOUND_RE = re.compile(
    r"^[\u4e00-\u9fa5A-Za-z0-9_\- ]{1,16}(?:调研|结论)$")
_LABEL_HEAD_RE = re.compile(r"^(?P<label>[^:：\n]{1,26})[:：]\s*(?P<body>.*)$", re.S)


def _label_kind(label_part: str) -> Optional[str]:
    """标签头闭类判别; 先 NORM 后 REPORT (NORM 压过任何完成态)。

    B01/B02/B04/B06/B09/L03–L06/C06/C07 的根因是 "任意冒号前子串" 都算
    label。本函数只认: 闭类标签本身, 或显式白名单化的主题+标签复合模式。
    """
    lp = (label_part or "").strip()
    if not lp or len(lp) > 18:
        return None
    # 去掉标签内的括号注释 (日期/自述等), 不参与标签词面
    lp_clean = re.sub(r"[（(][^）)]*[）)]", "", lp).strip()
    if not lp_clean:
        lp_clean = lp
    if lp_clean in _NORM_LABELS or _NORM_COMPOUND_RE.match(lp_clean):
        return "norm"
    if lp_clean in _REPORT_LABELS or _REPORT_COMPOUND_RE.match(lp_clean):
        return "report"
    return None


def _parse_label(text: str) -> Tuple[Optional[str], str, str]:
    """返回 (label_kind, body, label_phrase)。仅识别子句头标签。

    B-1.3/F1: 先对 quote/paren 做跨度掩码再找冒号, 避免
    `2026-09-01 "结论: ..."` / `(状态: ...)` 的引号/括号内标签被当根级。
    """
    t = _strip_leading_date(text or "")
    masked = _mask_spans(t, quotes=True, parens=True)
    m = _LABEL_HEAD_RE.match(masked)
    if not m:
        return None, t, ""
    kind = _label_kind(m.group("label"))
    if kind is None:
        return None, t, ""
    label_phrase = re.sub(r"\s+", " ", m.group("label")).strip()
    return kind, (m.group("body") or "").strip(), label_phrase


# ===========================================================================
# 2. 闭类证据抽取
# ===========================================================================

def _strip_future_plan_spans(text: str) -> str:
    """把 "计划/打算/拟/预计/即将/将要/将 + … + 结果谓词" 整体标成空格。

    A-5: 未来/计划是结构算子, 不是历史完成体; P01–P06 的
    "计划完成/预计完成/即将完成" 不得单独作为 STATE_EVENT 证据。
    """
    if not text:
        return text
    return _FUTURE_COMPLETION_SPAN_RE.sub(
        lambda m: " " * len(m.group(0)), text)


def _has_perfect(text: str) -> bool:
    """完成/过去体证据 (已/曾/刚, VP了/过, 完成/成功/完毕/结束, 结果承诺词)。

    条件触发短语 (完成前/后/时) 中的完成词应在调用前从文本删除/掩掉;
    未来/计划短语中的完成词同样先消解, 避免 P01–P06 误判历史完成态。
    """
    if not text:
        return False
    t = _strip_future_plan_spans(text)
    if not t.strip():
        return False
    if _PERFECT_ALREADY_RE.search(t):
        return True
    if _PERFECT_ZENG_RE.search(t) or _PERFECT_GANG_RE.search(t):
        return True
    if _PERFECT_LE_RE.search(t) or _PERFECT_GUO_RE.search(t):
        return True
    for m in _RESULT_COMPLEMENT_RE.finditer(t):
        pre = t[max(0, m.start() - 3):m.start()]
        if any(pre.endswith(neg) for neg in _RESULT_NEG_PRE):
            continue
        return True
    if _COMMITMENT_RE.search(t) or _COMMITMENT2_RE.search(t):
        return True
    return False


def _has_pending(text: str) -> List[str]:
    return [m.group(0) for m in _PENDING_RE.finditer(text or "")]


def _embedded_contents(text: str) -> List[str]:
    """返回引号/括号跨度内部文本 (B-1.3 判定嵌入证据用)。"""
    out: List[str] = []
    i, n = 0, len(text or "")
    while i < n:
        ch = text[i]
        if ch in _QUOTE_OPEN:
            close = _QUOTE_OPEN[ch]
            j = i + 1
            while j < n and text[j] != close:
                j += 1
            out.append(text[i + 1:j])
            i = j + 1
            continue
        if ch in _PAREN_PAIRS:
            close = _PAREN_PAIRS[ch]
            depth = 1
            j = i + 1
            while j < n and depth:
                if text[j] == ch:
                    depth += 1
                elif text[j] == close:
                    depth -= 1
                j += 1
            out.append(text[i + 1:j - 1])
            i = j
            continue
        i += 1
    return out


def _has_evidence_tokens(text: str) -> bool:
    """B-1.3: 嵌入/派生位是否真的携带闭类证据, 而非普通括号注释。"""
    if not text:
        return False
    return bool(_has_perfect(text) or _has_pending(text) or _has_deontic(text)
                or _has_speech(text) or _has_ability(text))


def _span_has_evidence(text: str) -> bool:
    if _has_evidence_tokens(text):
        return True
    kind, _body, _phrase = _parse_label(text)
    return kind is not None


def _has_negated_pending(text: str) -> bool:
    return bool(_NEGATED_PENDING_RE.search(text or ""))


def _has_deontic(text: str) -> bool:
    return bool(_DEONTIC_RE.search(text or ""))


def _has_ability(text: str) -> bool:
    return bool(_ABILITY_RE.search(text or ""))


def _has_speech(text: str) -> bool:
    if not text or _TECH_SPEECH_RE.search(text):
        return False
    for m in _SPEECH_RE.finditer(text):
        kw = m.group(0)
        # 技术对象消歧: 用户偏好设置/旧习惯等已由 _TECH_SPEECH_RE 拦;
        # 再排除 "期望值/符合期望"
        pre = text[max(0, m.start() - 6):m.start()]
        if kw.endswith("期望") and ("值" in text[m.end():m.end() + 2]
                                    or "合" in pre[-1:]):
            continue
        # "已按要求/依照要求" 是过去状语, 不是言语行为谓词
        if kw.endswith("要求") and pre[-1:] in ("按", "依", "遵", "据", "照"):
            continue
        return True
    return False


def _has_quantifier(text: str) -> bool:
    return bool(_QUANTIFIER_RE.search(text or ""))


def _first_clause_is_pending(clauses: Sequence[Clause]) -> bool:
    for c in clauses:
        if c.derived:
            continue
        # F1: 引号/括号内是嵌入内容, 不作为首句待办主题证据.
        masked = _mask_spans(c.text, quotes=True, parens=True)
        t = _strip_leading_date(masked).strip()
        if not t:
            continue
        if _NEGATED_PENDING_RE.search(t):
            return False
        return bool(_PENDING_RE.search(t))
    return False


def _symptom_observed_frame(text: str, prior_state: bool) -> bool:
    """观察框架: 同一子句含结构式 "...动作时/前/后", 或前文已有完成事件。"""
    if prior_state:
        return True
    return bool(_OBSERVED_FRAME_RE.search(text or ""))


def _agent_subject(text: str) -> bool:
    return bool(re.search(r"(?:用户|我|你|我们|大家|PM)", text or ""))


def _has_uncertain_followup(text: str) -> bool:
    return bool(re.search(
        r"(?:可能|也许|或许|大概|估计|不确定|待定|疑似|存疑|需要确认|待验证|"
        r"但可能|或者需要|可能要)", text or ""))


# ===========================================================================
# 3. 子句角色决策表 (A-3)
# ===========================================================================

RULE_LABEL = "RULE_LABEL"
RULE_DEONTIC = "RULE_DEONTIC"
RULE_SPEECH = "RULE_SPEECH"
RULE_CONDITIONAL = "RULE_CONDITIONAL"
RULE_GENERIC = "RULE_GENERIC"
STATE_EVENT = "STATE_EVENT"
STATE_REPORT = "STATE_REPORT"
STATE_SYMPTOM = "STATE_SYMPTOM"
PENDING_FIRST = "PENDING_FIRST"
PENDING = "PENDING"
WEAK_ABILITY = "WEAK_ABILITY"
DEFAULT = "DEFAULT"

_NORM_ROLES = {RULE_LABEL, RULE_DEONTIC, RULE_SPEECH, RULE_CONDITIONAL,
               RULE_GENERIC}
_STATE_ROLES = {STATE_EVENT, STATE_REPORT, STATE_SYMPTOM}


@dataclass
class _ClauseRole:
    clause: Clause
    role: str
    evidence: str = ""
    weak: bool = False      # 嵌入/派生位置 → 只算弱证据
    label_kind: Optional[str] = None


def _conditional_evidence(root_text: str) -> Tuple[str, str]:
    """剥离条件触发短语及紧邻体标记 → (cleaned, main_after_last_trigger)。

    "已部署前/交付完成后" 类条件句中的 已/完成 必须随触发短语一起剥离,
    否则会被误读为矩阵完成体。
    """
    cleaned = root_text
    last_main = ""
    for m in _COND_TRIGGER_RE.finditer(root_text):
        start = m.start()
        # 向前吃掉紧邻的 已/已经/曾/刚 + 完成体词 (如 "已部署前")
        k = start
        while k > 0 and root_text[k - 1] in " \t":
            k -= 1
        if k >= 2 and root_text[k - 2:k] in ("已经",):
            k -= 2
        elif k >= 1 and root_text[k - 1] in "已曾刚":
            k -= 1
        cleaned = cleaned[:k] + " " * (m.end() - k) + cleaned[m.end():]
        last_main = root_text[m.end():].strip()
    return cleaned, last_main


def _classify_clause(c: Clause, prior_state: bool, norm_seen: bool) -> _ClauseRole:
    raw = c.text.strip()
    label_kind, body, label_phrase = _parse_label(raw)
    weak = bool(c.derived)
    embedded = raw.startswith(("(", "（", "\"", "“", "'", "‘", "「"))
    if embedded:
        weak = True

    if label_kind == "norm" and not weak:
        return _ClauseRole(c, RULE_LABEL, evidence=f"norm:{label_phrase}",
                           label_kind=label_kind)
    if label_kind == "report" and not weak:
        # REPORT 标签: 正文里的情态/能力词是"被记录内容", 不产生根级 rule
        return _ClauseRole(c, STATE_REPORT, evidence=f"report:{label_phrase}",
                           label_kind=label_kind)

    # 根级文本: 掩掉引号/括号; 弱子句仍可提供弱证据
    root_text = _mask_spans(raw, quotes=True, parens=True)

    # --- 条件句角色: 先剥条件触发短语, 再看主句 ---
    cleaned, main = _conditional_evidence(root_text)
    had_cond = cleaned != root_text
    if had_cond:
        main_plain = _mask_spans(main, quotes=True, parens=True)
        # D-2: 道义/言语行为/全称/施事任一在作用域内 → 不判 symptom.
        # 不能因为出现 "...时/后" 就无条件落症状 (O05–O07).
        norm_scope = (_has_deontic(main_plain) or _has_speech(main_plain)
                      or _has_quantifier(main_plain)
                      or _agent_subject(main_plain)
                      or _has_deontic(root_text) or _has_speech(root_text)
                      or _has_quantifier(root_text)
                      or _agent_subject(root_text))
        if main_plain and _has_deontic(main_plain):
            return _ClauseRole(c, RULE_CONDITIONAL,
                               evidence="conditional_deontic", weak=weak)
        if main_plain and _has_ability(main_plain):
            if not norm_scope and _symptom_observed_frame(root_text,
                                                          prior_state):
                # "重启后不能进入桌面" / "验收时不允许导出数据" = 症状
                return _ClauseRole(c, STATE_SYMPTOM,
                                   evidence="conditional_ability",
                                   weak=weak)
            # norm_scope 命中: 不判 symptom, 继续走下面 root/ability 决策表
        elif (main_plain and not _has_perfect(main_plain)
              and not _NEGATED_PENDING_RE.search(main_plain)):
            # 条件从句 + 主句无过去体 = 裸祈使/后续动作 → rule
            return _ClauseRole(c, RULE_CONDITIONAL,
                               evidence="conditional_action", weak=weak)
        # 主句带过去体/能力否定被 norm_scope 拦住 → 继续按完成事件/规则判

    # --- 根级规范/言语行为/道义 ---
    if not weak:
        if _has_speech(root_text):
            return _ClauseRole(c, RULE_SPEECH,
                               evidence="speech_act", weak=False)
        if _has_deontic(root_text):
            return _ClauseRole(c, RULE_DEONTIC,
                               evidence="deontic", weak=False)
        if _has_quantifier(root_text) and not _has_perfect(cleaned):
            return _ClauseRole(c, RULE_GENERIC,
                               evidence="quantifier", weak=False)

    # --- 能力否定: 症状 / 规则 / 模糊 ---
    if _has_ability(root_text):
        if _has_deontic(root_text) or _has_quantifier(root_text) \
                or _has_speech(root_text) or _agent_subject(root_text):
            return _ClauseRole(c, RULE_DEONTIC,
                               evidence="ability_with_norm_scope", weak=weak)
        if _symptom_observed_frame(root_text, prior_state):
            return _ClauseRole(c, STATE_SYMPTOM, evidence="symptom", weak=weak)
        return _ClauseRole(c, WEAK_ABILITY, evidence="ability_no_frame",
                           weak=True)

    # --- 完成/过去体 ---
    if _has_perfect(cleaned):
        return _ClauseRole(c, STATE_EVENT, evidence="perfect", weak=weak)

    # --- 未完成/待办 (引号/括号跨度已由 root_text 掩码剔除) ---
    pend = _has_pending(root_text)
    if pend and _has_negated_pending(root_text):
        return _ClauseRole(c, DEFAULT, evidence="negated_pending", weak=True)
    if pend:
        role = PENDING_FIRST if c.index == 0 else PENDING
        return _ClauseRole(c, role, evidence="pending:" + pend[0], weak=weak)

    return _ClauseRole(c, DEFAULT, evidence="none", weak=weak)


# ===========================================================================
# 4. 全局决策表 (A-4)
# ===========================================================================

def _signals_snapshot(clauses: Sequence[Clause],
                      roles: Sequence[_ClauseRole],
                      has_date: bool) -> Dict[str, Any]:
    norm, state, pending, downgraded = [], [], [], []
    for c, r in zip(clauses, roles):
        if r.role == RULE_LABEL:
            norm.append(r.evidence)
        elif r.role in _STATE_ROLES:
            state.append(r.evidence)
        elif r.role in (PENDING, PENDING_FIRST):
            pending.append(r.evidence)
        if r.weak and r.evidence not in ("none",):
            downgraded.append(f"{r.role}:{r.evidence}")
    # 短审计摘要: 每列 ≤4 项, 单项 ≤28 字
    def _norm_list(xs: List[str]) -> List[str]:
        return [x[:28] for x in xs[:4]]
    return {
        "has_date": bool(has_date),
        "norm": _norm_list(norm),
        "state": _norm_list(state),
        "pending": _norm_list(pending),
        "downgraded": _norm_list(downgraded),
        "clause_count": len(clauses),
    }


def judge_entry(content: str,
                type_hint: Optional[str] = None) -> JudgeResult:
    """SAFE-JUDGE v3 入口: 返回三值 JudgeResult (纯规则, 不调 LLM)。"""
    text = (content or "").strip()
    if type_hint in ("state", "rule"):
        sig = {"has_date": bool(_DATE_RE.search(text)), "norm": [],
               "state": [], "pending": [], "downgraded": [],
               "type_hint": type_hint}
        return JudgeResult(type_hint, type_hint, 1.0, "strong", sig,
                           f"type_hint={type_hint}")

    has_date = bool(text and _DATE_RE.search(text))
    clauses = _split_units(text)
    roles: List[_ClauseRole] = []
    prior_state = False
    for c in clauses:
        r = _classify_clause(c, prior_state, False)
        roles.append(r)
        if r.role in _STATE_ROLES and not r.weak:
            prior_state = True
    sig = _signals_snapshot(clauses, roles, has_date)

    root_norm = any(r.role in _NORM_ROLES and not r.weak for r in roles)
    state_roles = [r for r in roles if r.role in _STATE_ROLES and not r.weak]
    state_ev = bool(state_roles)
    weak_ability = any(r.role == WEAK_ABILITY for r in roles)
    pending_first = any(r.role == PENDING_FIRST for r in roles) \
        or _first_clause_is_pending(clauses)
    any_pending = any(r.role in (PENDING, PENDING_FIRST) for r in roles)
    weak = _has_uncertain_followup(text)
    # B-1.3 安全出口: 只有引号/括号嵌入证据, 或箭头/因此派生证据时,
    # 不升格为根级 rule/state; 返回 ambiguous 留热层 + review_at.
    # 普通括号注释/技术列表 (内部无闭类证据) 不触发.
    embedded_evidence = any(_span_has_evidence(x)
                            for x in _embedded_contents(text))
    derived_evidence = any(c.derived and c.text.strip() for c in clauses)
    only_embedded = embedded_evidence or derived_evidence

    # 回滚开关: ambiguous_hold=0 → 直接当 rule, 不产生 review 字段
    hold = bool(getattr(_cfg, "JUDGE_AMBIGUOUS_HOLD", True))

    def _rule(band: str, conf: float, reason: str) -> JudgeResult:
        return JudgeResult("rule", "rule", conf, band, sig, reason)

    def _state(band: str, conf: float, reason: str) -> JudgeResult:
        return JudgeResult("state", "state", conf, band, sig, reason)

    def _amb(reason: str) -> JudgeResult:
        if not hold:
            return _rule("weak", 0.5, f"{reason} (ambiguous_hold off → rule)")
        return JudgeResult("ambiguous", "rule", 0.5, "ambiguous", sig, reason)

    if root_norm:
        # 根级 NORM / 道义 / 态度 / 条件 / 全称 > 任何位置完成态
        return _rule("strong", 0.9, "root_norm_evidence")
    if pending_first:
        return _rule("medium", 0.65, "first_clause_pending")
    if state_ev and not has_date:
        return _amb("completed_state_without_date_anchor")
    if state_ev and weak:
        return _amb("completed_state_with_uncertain_followup")
    if state_ev:
        band = "weak" if all(r.role == STATE_SYMPTOM for r in state_roles) \
            else "strong"
        conf = 0.75 if band == "weak" else 0.9
        return _state(band, conf, "state_evidence")
    if any_pending:
        return _rule("medium", 0.65, "non_first_clause_pending")
    if weak_ability:
        return _amb("ability_or_negation_subject_frame_unknown")
    if only_embedded:
        return _amb("only_embedded_or_derived_evidence")
    return _rule("weak", 0.5, "no_strong_evidence_default_rule")


def judge_entry_detail(content: str,
                       type_hint: Optional[str] = None) -> Dict[str, Any]:
    r = judge_entry(content, type_hint=type_hint)
    return {
        "type": r.public_type,
        "decision": r.decision,
        "public_type": r.public_type,
        "band": r.band,
        "confidence": r.confidence,
        "signals": r.signals,
        "reason": r.reason,
        "judge_policy": "v3",
    }


def ambiguous_review_at(now: Optional[datetime] = None) -> datetime:
    days = int(getattr(_cfg, "JUDGE_AMBIGUOUS_REVIEW_DAYS", 7))
    return (now or datetime.now(timezone.utc)) + timedelta(days=days)


__all__ = ["JudgeResult", "judge_entry", "judge_entry_detail", "Clause",
           "ambiguous_review_at"]
