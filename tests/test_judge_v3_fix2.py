#!/usr/bin/env python3
"""tests/test_judge_v3_fix2.py — SAFE-JUDGE v3 整改轮回归。

覆盖独立复核 JUDGE-REVIEW D3 的阻断/重要用例：
  - 标签路径：任意冒号前子串不得当 REPORT/NORM label；
  - 未来/计划：计划完成/预计完成/即将完成不得当历史完成态；
  - 症状分支：条件触发命中后仍按 §D-2 检查 deontic/speech/quantifier/agent；
  - §B-1.3：仅嵌入/派生证据 → ambiguous 留热层；
  - 新完成动词：年检通过了 / 审核通过了 / 刚签约 / 刚巡检完 → state；
  - 审计字段：type_source / judge_reviewed_at:null / resolved_rule_grace。

每条 rule|ambiguous 用例除了断言判型，还走 direct_write_govern 实测：
条目仍在热层、冷层零 recall/remember。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.config import JUDGE_RESOLVED_RULE_GRACE_DAYS  # noqa: E402
from memorycore.core.judge import judge_entry  # noqa: E402
from memorycore.core.metadata import direct_write_govern  # noqa: E402
from memorycore.core.overflow import _rule_activity_tier  # noqa: E402
from conftest import MockMnemosyneClient  # noqa: E402


# (id, text, expected decision) — 全部来自 JUDGE-REVIEW §2/§D3。
D3_CASES = [
    # 标签路径 & 引号/括号跨度 (B01–B09 / Q03–Q10)
    ("B01", '2026-09-01 "结论: 方案A不可行"', "ambiguous"),
    ("B02", "2026-09-01 (状态: 已完成部署)", "ambiguous"),
    ("B03", '2026-09-01 "交付完成"', "ambiguous"),
    ("B04", "2026-09-01 讨论了恢复方案: 必须先备份数据库", "rule"),
    ("B05", "2026-09-01 关于上线计划: 每周必须回归验证", "rule"),
    ("B06", "2026-09-01 部署方案: 上线前必须双人复核", "rule"),
    ("B09", "2026-09-01 关于后续数据恢复工作的安排说明: 必须双人复核", "rule"),
    # 正文道义语义必须压过名词主题 (C06/C07/L03–L06)
    ("C06", "2026-09-01 恢复流程: 必须双人确认", "rule"),
    ("C07", "2026-09-01 上线检查项: 每次发布前必须演练", "rule"),
    ("L03", "2026-09-01 完成情况: 每周必须汇报", "rule"),
    ("L04", "2026-09-01 部署状态: 每次上线前必须验证", "rule"),
    ("L05", "2026-09-01 迁移记录: 严禁直接改库", "rule"),
    ("L06", "2026-09-01 恢复流程: 严禁跳过校验", "rule"),
    # 未来/计划消解 (P01–P06)
    ("P01", "2026-09-01 计划完成部署", "rule"),
    ("P02", "2026-09-01 计划下周完成部署", "rule"),
    ("P03", "2026-09-01 计划于下周完成年检", "rule"),
    ("P04", "2026-09-01 预计完成部署", "rule"),
    ("P05", "2026-09-01 即将完成部署", "rule"),
    ("P06", "2026-09-01 将要完成部署", "rule"),
    # 症状分支禁止无条件放行 (O05–O07)
    ("O05", "2026-09-01 用户操作时不能跳过审批", "rule"),
    ("O06", "2026-09-01 每次登录时不能使用弱密码", "rule"),
    ("O07", "2026-09-01 任何发布时不允许跳过审批", "rule"),
    # §B-1.3 纯嵌入/派生证据安全出口
    ("Q03", '2026-09-01 "用户要求: 每次必须确认"', "ambiguous"),
    ("Q04", "2026-09-01 系统正常(已完成部署)", "ambiguous"),
    ("Q05", "2026-09-01 方案讨论 → 必须回退", "ambiguous"),
    ("Q06", "2026-09-01 系统正常(必须留意)", "ambiguous"),
    ("Q07", '2026-09-01 "已完成部署"', "ambiguous"),
    ("Q08", "2026-09-01 (已完成部署)", "ambiguous"),
    ("Q09", '2026-09-01 "必须回退"', "ambiguous"),
    ("Q10", "2026-09-01 (必须回退)", "ambiguous"),
]


@pytest.mark.parametrize("case_id,text,expected", D3_CASES,
                         ids=[c[0] for c in D3_CASES])
def test_d3_case_holds_hot_and_zero_cold_calls(
        case_id, text, expected, tmp_store, mock_client, meta_for):
    """判型 = rule|ambiguous + direct_write_govern 留热层 + 冷层零调用。"""
    r = judge_entry(text)
    assert r.decision == expected, (case_id, text, r.decision, r.reason)

    tmp_store.add("memory", text)
    out = direct_write_govern(tmp_store, mock_client, "memory", text,
                              action="add")
    assert out["status"] in ("stamped_rule", "held_ambiguous"), (case_id, out)
    assert text in tmp_store.entries("memory"), \
        f"{case_id}: 必须留在热层 (got {out})"
    assert mock_client.stored == [] and mock_client.recall_queries == [], \
        f"{case_id}: 判型/直写同步路径必须零冷层调用"
    m = meta_for("memory").get_entry(text) or {}
    assert m.get("type") == "rule", (case_id, m)
    assert m.get("type_source") in ("judge_v3", "judge_v3_ambiguous"), \
        (case_id, m.get("type_source"))


@pytest.mark.parametrize("text", [
    "2026-09-01 年检通过了",
    "2026-09-01 刚签约一家供应商",
    "2026-09-01 审核通过了",
    "2026-09-01 刚巡检完",
])
def test_review_missed_completions_are_state(text):
    """D3.1: VP了 与 刚+谓词 开放域补漏 — 必须判 state, 不得留热层。"""
    assert judge_entry(text).decision == "state", \
        (text, judge_entry(text).reason)


def test_ambiguous_audit_fields_land_on_disk(tmp_store, mock_client, meta_for):
    """§6.2/6.3: ambiguous 落 judge_review_at / reviewed_at:null / type_source。"""
    entry = "已通知用户验收结果"
    tmp_store.add("memory", entry)
    out = direct_write_govern(tmp_store, mock_client, "memory", entry,
                              action="add")
    assert out["status"] == "held_ambiguous", out
    m = meta_for("memory").get_entry(entry)
    assert m["type_source"] == "judge_v3_ambiguous"
    assert m["judge_review_at"], "ambiguous 必须有 review_at 期限"
    assert "judge_reviewed_at" in m and m["judge_reviewed_at"] is None, \
        "judge_reviewed_at:null 必须显式落盘"
    assert m["judge_review_count"] == 0


def test_rule_audit_type_source_lands_on_disk(tmp_store, mock_client, meta_for):
    """§6.2: v3 rule 直写落 type_source=judge_v3。"""
    entry = "用户偏好: 方案可靠性优先"
    tmp_store.add("memory", entry)
    out = direct_write_govern(tmp_store, mock_client, "memory", entry,
                              action="add")
    assert out["status"] == "stamped_rule", out
    m = meta_for("memory").get_entry(entry)
    assert m["type_source"] == "judge_v3"
    assert m["judge_decision"] == "rule"


def test_rule_activity_tier_resolved_rule_grace(tmp_store, meta_for):
    """§Q-E: LLM 终审判 rule 后 _rule_activity_tier 返回 14d R-grace。"""
    entry = "条目G: 正文内容。"
    tmp_store.add("memory", entry)
    now = datetime.now(timezone.utc)
    meta_for("memory").stamp(
        entry, "rule",
        judge_decision="ambiguous", judge_band="weak", judge_confidence=0.5,
        judge_resolution="rule",
        judge_resolved_at=now - timedelta(days=1))
    meta = meta_for("memory").get_entry(entry)
    tier, min_age = _rule_activity_tier(meta, entry, [], now)
    assert (tier, min_age) == ("resolved_rule_grace",
                               JUDGE_RESOLVED_RULE_GRACE_DAYS), (tier, min_age)
    # 过期后回到普通 idle tier, 不再占用 grace.
    meta2 = dict(meta)
    meta2["judge_resolved_at"] = (now - timedelta(
        days=JUDGE_RESOLVED_RULE_GRACE_DAYS + 1)).isoformat()
    tier2, _ = _rule_activity_tier(meta2, entry, [], now)
    assert tier2 != "resolved_rule_grace"


def test_cold_unreachable_ambiguous_and_state_both_stay_hot(
        tmp_store, meta_for):
    """§3.5: 冷层不可达时 ambiguous 与 state 100% 留热层。"""
    bad = MockMnemosyneClient(fail_recall=True)
    amb = "已通知用户验收结果"
    state = f"{datetime.now().strftime('%Y-%m-%d')} 拍板: 方案定稿"
    for e in (amb, state):
        tmp_store.add("memory", e)
    out_amb = direct_write_govern(tmp_store, bad, "memory", amb, action="add")
    assert out_amb["status"] == "held_ambiguous", out_amb
    assert amb in tmp_store.entries("memory")
    out_state = direct_write_govern(tmp_store, bad, "memory", state,
                                    action="add")
    assert out_state["status"] == "kept_hot_backstop", out_state
    assert state in tmp_store.entries("memory")
    assert bad.stored == [] and bad.recall_queries == []
    m_amb = meta_for("memory").get_entry(amb)
    m_state = meta_for("memory").get_entry(state)
    assert m_amb["judge_decision"] == "ambiguous"
    assert m_amb["type_source"] == "judge_v3_ambiguous"
    assert m_state["type"] == "state" and m_state["type_source"] == "judge_v3"


def test_store_fact_writes_type_source(tmp_store, mock_client, meta_for):
    """§6.2: memorycore_store_entry 热路径也落 type_source=judge_v3*。"""
    import json
    from memorycore import server
    server._store = tmp_store
    server._client = mock_client
    rule = "用户偏好: 极简选型, Go/Rust 单二进制"
    amb = "已通知用户验收结果"
    r1 = json.loads(server.memorycore_store_entry(rule, importance=0.8))
    assert r1["status"] == "stored", r1
    assert meta_for("memory").get_entry(rule)["type_source"] == "judge_v3"
    r2 = json.loads(server.memorycore_store_entry(amb, importance=0.8))
    assert r2["status"] == "stored", r2
    m2 = meta_for("memory").get_entry(amb)
    assert m2["type_source"] == "judge_v3_ambiguous"
    assert m2["judge_review_at"]
