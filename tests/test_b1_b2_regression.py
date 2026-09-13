#!/usr/bin/env python3
"""tests/test_b1_b2_regression.py — 独立评审 B-1/B-2 锁定回归 (2026-09-12)。

B-1: 11 条"要求/偏好/进行时"句子不得判 state, 更不得经直写通道冷迁;
     关键断言是"留在热层且冷层未写", 不是只断判型返回值。
B-2: 3 条技术对象含行为词子串的完成态记录仍判 state 并正常冷迁。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.classifier import classify_entry_type  # noqa: E402
from memorycore.core.metadata import direct_write_govern  # noqa: E402


B1_BEHAVIOR_RULES = [
    "2026-09-01 用户要求: 每次交付完成后必须通知我",
    "2026-09-01 用户要求: 新功能已部署前必须先确认",
    "2026-09-01 用户要求: 恢复完成后做验证",
    "2026-09-01 用户希望: 上线完成后自动通知",
    "2026-09-01 待上线功能, 上线完成后通知用户",
    "2026-09-01 待上线功能已部署到测试环境",
    "2026-09-01 交付完成前必须先确认",
    "2026-09-01 部署完成后必须验证再上线",
    "2026-09-01 规范: 已部署环境变更前必须回退",
    "2026-09-01 原则: 恢复成功后先验证",
    "2026-09-01 规则: 已上线功能修改必须先回退",
]

B2_TECHNICAL_STATES = [
    "2026-09-01 调研结论: 禁止使用方案A(技术原因)",
    "2026-09-01 已完成迁移: 用户偏好设置已修复",
    "2026-09-01 已交付: 修复用户旧习惯导致的崩溃",
]


@pytest.mark.parametrize("entry", B1_BEHAVIOR_RULES)
def test_b1_behavior_rules_stay_hot_and_cold_untouched(
        tmp_store, mock_client, meta_for, entry):
    """B-1 关键回归: 判 rule + 直写后仍在热层 + 冷层零写入。"""
    assert classify_entry_type(entry) == "rule", entry
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry,
                            action="add")
    assert r["status"] == "stamped_rule", (entry, r)
    assert entry in tmp_store.entries("memory"), \
        "行为准则/偏好/进行时句必须留在热层"
    assert mock_client.stored == [], "冷层不得被写入"
    assert mock_client.recall_queries == [], "冷层连查重都不应发生"
    m = meta_for("memory").get_entry(entry)
    assert m and m["type"] == "rule" and m["origin"] == "hermes"


@pytest.mark.parametrize("entry", B2_TECHNICAL_STATES)
def test_b2_technical_objects_with_behavior_substrings_are_state(
        tmp_store, mock_client, entry):
    """B-2 回归: 行为词子串在技术对象里不干扰 state 判型与冷迁。"""
    assert classify_entry_type(entry) == "state", entry
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry,
                            action="add")
    assert r["status"] == "migrated_new", (entry, r)
    assert entry not in tmp_store.entries("memory")
    assert entry in mock_client.stored


# ---- v3 基线 FAIL 9 条纳入 (R12/S04-S09/S12/S13) -------------------------------
# 这些正是 2026-09-13 设计基线 20/29 的 9 条 FAIL; v3 修复后在此锁定。
V3_BASELINE_FIXES = [
    ("R12", "rule", "2026-09-01 上线完成，规则: 每次上线前先演练回退"),
    ("S04", "state", "2026-09-01 部署完成。已通知用户验收结果"),
    ("S05", "state", "2026-09-01 调研完成。已按要求归档结论"),
    ("S06", "state", "2026-09-01 交付完成。已请客户验收"),
    ("S07", "state", "2026-09-01 已部署新版本，验证时不能访问服务"),
    ("S08", "state", "2026-09-01 已完成恢复，重启后不能进入桌面"),
    ("S09", "state", "2026-09-01 已交付，客户验收时不允许导出数据"),
    ("S12", "state", "2026-09-01 已完成部署，无待上线功能"),
    ("S13", "state", "2026-09-01 已完成恢复，未完成项清单为空"),
]


@pytest.mark.parametrize("case_id,expected,entry", V3_BASELINE_FIXES)
def test_v3_baseline_failures_classify(case_id, expected, entry):
    assert classify_entry_type(entry) == expected, (case_id, entry)


@pytest.mark.parametrize("case_id,expected,entry", V3_BASELINE_FIXES)
def test_v3_baseline_failures_direct_write_hot_cold(
        case_id, expected, entry, tmp_store, mock_client, meta_for):
    """集成断言: R 类留热且冷层零写入; S 类冷层成功才删本地。"""
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry,
                            action="add")
    if expected == "rule":
        assert r["status"] in ("stamped_rule", "held_ambiguous"), (case_id, r)
        assert entry in tmp_store.entries("memory")
        assert mock_client.stored == [] and mock_client.recall_queries == []
    else:
        assert r["status"] == "migrated_new", (case_id, r)
        assert entry not in tmp_store.entries("memory")
        assert entry in mock_client.stored
