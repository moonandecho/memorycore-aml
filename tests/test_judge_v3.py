#!/usr/bin/env python3
"""tests/test_judge_v3.py — SAFE-JUDGE v3 机制级判型验收。

覆盖:
  - 设计稿 26+3 攻击用例逐条命中 (机制决策表, 无逐条特判);
  - 临界/模糊表 C1/C2 (无日期完成态、完成态+不确定后续 → ambiguous);
  - 解析单元: 逗号/句号后标签、括号/引号内指令降级、箭头派生、
    `无待上线`/`未完成项清单为空` 否定作用域、type_hint 优先;
  - 回滚开关: JUDGE_AMBIGUOUS_HOLD=0 退二值; JUDGE_V3_ENABLED=0 回 legacy;
  - 直写通道: ambiguous 留热层 + 元数据 review_at, 冷层零 recall/remember。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core.classifier import classify_entry_type, classify_entry_type_detail  # noqa: E402
from memorycore.core.judge import judge_entry  # noqa: E402
from memorycore.core.metadata import MetaStore, direct_write_govern  # noqa: E402


# ---------------- 设计稿 26+3 攻击用例 (文本来自 JUDGE-DESIGN §F-1) ----------
ATTACK_CASES = [
    ("R01", "rule", "2026-09-01 用户要求: 每次交付完成后必须通知我"),
    ("R02", "rule", "2026-09-01 用户要求: 新功能已部署前必须先确认"),
    ("R03", "rule", "2026-09-01 用户要求: 恢复完成后做验证"),
    ("R04", "rule", "2026-09-01 用户希望: 上线完成后自动通知"),
    ("R05", "rule", "2026-09-01 待上线功能, 上线完成后通知用户"),
    ("R06", "rule", "2026-09-01 待上线功能已部署到测试环境"),
    ("R07", "rule", "2026-09-01 交付完成前必须先确认"),
    ("R08", "rule", "2026-09-01 部署完成后必须验证再上线"),
    ("R09", "rule", "2026-09-01 规范: 已部署环境变更前必须回退"),
    ("R10", "rule", "2026-09-01 原则: 恢复成功后先验证"),
    ("R11", "rule", "2026-09-01 规则: 已上线功能修改必须先回退"),
    ("R12", "rule", "2026-09-01 上线完成，规则: 每次上线前先演练回退"),
    ("R13", "rule", "2026-09-01 上线完成。规则: 每次上线前先演练回退"),
    ("S01", "state", "2026-09-01 调研结论: 禁止使用方案A(技术原因)"),
    ("S02", "state", "2026-09-01 已完成迁移: 用户偏好设置已修复"),
    ("S03", "state", "2026-09-01 已交付: 修复用户旧习惯导致的崩溃"),
    ("S04", "state", "2026-09-01 部署完成。已通知用户验收结果"),
    ("S05", "state", "2026-09-01 调研完成。已按要求归档结论"),
    ("S06", "state", "2026-09-01 交付完成。已请客户验收"),
    ("S07", "state", "2026-09-01 已部署新版本，验证时不能访问服务"),
    ("S08", "state", "2026-09-01 已完成恢复，重启后不能进入桌面"),
    ("S09", "state", "2026-09-01 已交付，客户验收时不允许导出数据"),
    ("S10", "state", "2026-09-01 用户对: 方案已定稿"),
    ("S11", "state", "2026-09-01 用户明确: 方案已定稿"),
    ("S12", "state", "2026-09-01 已完成部署，无待上线功能"),
    ("S13", "state", "2026-09-01 已完成恢复，未完成项清单为空"),
    ("G01", "rule", "用户偏好: 方案可靠性优先"),
    ("G02", "rule", "准则: 每次先确认"),
    ("G03", "rule", "方案可靠性偏好: 依赖第三方镜像=不可靠"),
]


@pytest.mark.parametrize("case_id,expected,text", ATTACK_CASES)
def test_attack_cases_29(case_id, expected, text):
    assert classify_entry_type(text) == expected, (case_id, text)


@pytest.mark.parametrize("case_id,expected,text", ATTACK_CASES)
def test_attack_detail_exposes_decision(case_id, expected, text):
    d = classify_entry_type_detail(text)
    assert d["type"] == expected, (case_id, d)
    assert d["decision"] in ("state", "rule", "ambiguous")
    assert "signals" in d and "reason" in d


# ---------------- 模糊带 + 临界表 (F-4) --------------------------------------

def test_c1_completed_without_date_anchor_is_ambiguous():
    r = judge_entry("已通知用户验收结果")
    assert r.decision == "ambiguous" and r.public_type == "rule"
    assert r.band == "ambiguous"


def test_c2_completed_with_uncertain_followup_is_ambiguous():
    r = judge_entry("2026-09-01 已完成部署，但可能需要回退")
    assert r.decision == "ambiguous" and r.public_type == "rule"


def test_c3_isolated_negated_pending_defaults_rule():
    d = classify_entry_type_detail("2026-09-01 无待上线功能")
    assert d["type"] == "rule" and d["decision"] == "rule"


def test_c4_condition_trigger_not_completion():
    assert classify_entry_type("2026-09-01 方案定稿前两版都保留") == "rule"


def test_c10_norm_label_beats_completion():
    assert classify_entry_type("2026-09-01 用户要求: 方案已完成") == "rule"


def test_ambiguous_hold_switch_off_becomes_rule(monkeypatch):
    monkeypatch.setattr(config_mod, "JUDGE_AMBIGUOUS_HOLD", False)
    r = judge_entry("已通知用户验收结果")
    assert r.decision == "rule" and r.public_type == "rule"
    assert r.band == "weak"


def test_judge_v3_switch_off_uses_legacy_v2(monkeypatch):
    text = "2026-09-01 上线完成，规则: 每次上线前先演练回退"
    assert classify_entry_type(text) == "rule"  # v3 机制修复 R12
    monkeypatch.setattr(config_mod, "JUDGE_V3_ENABLED", False)
    # v2 legacy 对 R12 的现役基线是 state (即 V3=0 实测回滚有效)
    assert classify_entry_type(text) == "state"


# ---------------- 解析单元 (括号/引号/箭头/否定/标签边界) ----------------------

def test_parse_label_after_all_separators():
    for sep in ["，", "。", "；", ",", ".", "！", "\n"]:
        text = f"2026-09-01 上线完成{sep}规则: 每次先演练回退"
        assert classify_entry_type(text) == "rule", sep


def test_paren_instruction_is_not_root_rule():
    # 括号内 "必须" 只作注释, 无其他根级证据 → §B-1.3 ambiguous 留热层
    r = judge_entry("2026-09-01 上线完成(注释: 必须回退)")
    assert r.decision == "state"  # 句外 完成 是根级完成态, 括号内必须不构成 rule
    # 纯括号内完成态不作为根级 state 证据 → §B-1.3 ambiguous (不是默认 rule)
    assert judge_entry("2026-09-01 (已完成部署)").decision == "ambiguous"


def test_quoted_instruction_is_not_root_rule():
    # 引号内是引述内容, 不是根级指令; 只有嵌入证据 → §B-1.3 ambiguous
    assert judge_entry('2026-09-01 用户说"必须回退"').decision == "ambiguous"
    # REPORT 标签后的引述完成态仍按被记录内容判 state
    assert judge_entry('2026-09-01 记录: "已完成部署"').decision == "state"


def test_arrow_derived_evidence_is_weak():
    # 箭头后建议是派生位置, 不构成根级 rule; 有日期完成态仍在根级 → state
    assert classify_entry_type("2026-09-01 部署完成 → 建议回退") == "state"
    # 仅箭头后指令、无根级证据 → §B-1.3 ambiguous (留热层)
    assert judge_entry("2026-09-01 方案讨论 → 必须回退").decision == "ambiguous"


def test_negated_pending_scope():
    assert classify_entry_type("2026-09-01 已完成部署，无待上线功能") == "state"
    assert classify_entry_type("2026-09-01 已完成恢复，未完成项清单为空") == "state"
    assert classify_entry_type("2026-09-01 无待上线功能") == "rule"


def test_type_hint_priority_and_detail_schema():
    assert classify_entry_type("已通知用户验收结果", type_hint="state") == "state"
    assert classify_entry_type("用户要求: 方案已完成", type_hint="rule") == "rule"
    d = classify_entry_type_detail("2026-09-01 已完成部署，但可能需要回退")
    assert d["type"] == "rule" and d["decision"] == "ambiguous"
    assert d["band"] == "ambiguous" and d["reason"]


# ---------------- 直写通道 ambiguous 安全路由 --------------------------------

def test_direct_write_ambiguous_holds_hot_with_metadata(
        tmp_store, mock_client, meta_for):
    entry = "已通知用户验收结果"
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry,
                            action="add")
    assert r["status"] == "held_ambiguous", r
    assert entry in tmp_store.entries("memory"), "ambiguous 必须留热层"
    assert mock_client.stored == [] and mock_client.recall_queries == [], \
        "判型同步路径零冷层调用"
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "rule"
    assert m["judge_decision"] == "ambiguous"
    assert m["judge_review_at"], "必须写 7d 复审期限"
    assert m["judge_review_count"] == 0


def test_direct_write_rule_has_judge_fields(tmp_store, mock_client, meta_for):
    entry = "用户偏好: 方案可靠性优先"
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry,
                            action="add")
    assert r["status"] == "stamped_rule"
    m = meta_for("memory").get_entry(entry)
    assert m["judge_decision"] == "rule" and m["judge_policy"] == "v3"


# ---------------- 反过拟合自测: 设计稿未出现的新用例 (机制外推) --------------
# 逐条标注期望判定与依据的机制规则; 不含任何具体用例文本特判。
SELF_CASES = [
    # (文本, 期望 decision, 依据机制)
    ("2026-09-01 用户要求: 灰度发布完成后必须保留回滚通道",
     "rule", "NORM 标签 + 条件触发 完成/后 + 主句道义"),
    ("2026-09-01 已完成订单导出，规则: 每次导出前先脱敏",
     "rule", "逗号后 NORM 标签 > 前置完成态"),
    ("2026-09-01 升级网关后，压测时无法建立连接",
     "state", "能力否定 + 同子句时间状语观察框架"),
    ("2026-09-01 灰度未完成前不得切流",
     "rule", "条件触发 未完成前 + 主句道义 不得"),
    ('2026-09-01 运营说"必须预热缓存"，但预热已完成',
     "state", "引号内指令掩码 + 根级 已完成 完成态"),
    ("2026-09-01 已完成数据回填(注意: 不得重跑)",
     "state", "括号内指令掩码 + 根级 已完成"),
    ("2026-09-01 已完成灰度剥离: 旧链路已下线",
     "state", "已+谓词体貌 (领域动词仅填槽)"),
    ("2026-09-01 客户签收前必须先质检",
     "rule", "条件触发 签收前 + 主句道义 必须"),
    ("任何时候都不允许绕过审批",
     "rule", "全称量词 + 能力否定落入道义规则作用域"),
    ("2026-09-01 待回滚变更清单为空",
     "rule", "首子句待办主题 → 项目既有口径留热"),
    ("2026-09-01 部署完成后已通知值班，接着开始压测",
     "state", "条件触发剥离后主句仍有 已 完成体"),
    ("2026-09-01 备份已完成 → 建议下班前再导出",
     "state", "根级完成态; 箭头后派生建议仅弱证据"),
    ("2026-09-01 系统正常(必须留意)",
     "ambiguous", "§B-1.3: 只有嵌入指令证据 → ambiguous 留热层"),
    ("2026-09-01 已签署供应商变更单: 交付物已归档",
     "state", "已+新领域谓词; 新动词不改代码"),
    ("2026-09-01 已完成偏好设置迁移，无行为规则",
     "state", "完成态 + 技术对象词不产生 rule"),
]

SELF_AMBIGUOUS_CASES = [
    ("已通知用户验收结果", "completed_state_without_date_anchor"),
    ("2026-09-01 数据可能已丢失", "completed_state_with_uncertain_followup"),
    ("无法访问共享目录", "ability_or_negation_subject_frame_unknown"),
    ("2026-09-01 已完成部署，但可能需要回退",
     "completed_state_with_uncertain_followup"),
    ("已按要求归档结论", "completed_state_without_date_anchor"),
]


@pytest.mark.parametrize("text,expected,mechanism", SELF_CASES)
def test_anti_overfit_new_domain_cases(text, expected, mechanism):
    r = judge_entry(text)
    assert r.decision == expected, (text, mechanism, r)


@pytest.mark.parametrize("text,reason_fragment", SELF_AMBIGUOUS_CASES)
def test_anti_overfit_ambiguous_samples(text, reason_fragment):
    r = judge_entry(text)
    assert r.decision == "ambiguous", (text, r)
    assert r.public_type == "rule"
    assert reason_fragment in r.reason, (text, r.reason)
