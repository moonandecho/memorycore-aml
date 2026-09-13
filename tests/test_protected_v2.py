#!/usr/bin/env python3
"""tests/test_protected_v2.py — Q4 v2 protected 判定 (2026-09-12, DESIGN §Q4)。

覆盖:
  ① 泛词 准确/严谨/验证/覆盖 不再保护 (历史记录误标修复)
  ② 新保护面: 用户明确 / 用户对…期望 / 问题清单 / protect_override
  ③ protected 不进候选池 + PROTECT_SKIP_LRU 回滚 (资格豁免语义)
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.overflow import _is_protected_rule  # noqa: E402


@pytest.mark.parametrize("entry", [
    "2026-09-01 定位再确认: 覆盖交互需求的方案已彻底用起来",
    "严谨调研结论: 该方案不准确, 经过验证不可行",
    "验证记录: 测试覆盖率达到 90%",
])
def test_vague_words_no_longer_protect(entry):
    """泛词 准确/严谨/验证/覆盖 不再是保护判据 (DESIGN §Q4 收紧)。"""
    assert _is_protected_rule(entry, {"importance": 0.8}) is False, entry


@pytest.mark.parametrize("entry", [
    "用户2026-09-05明确: 性能优先于省电",       # 用户(日期)?明确 正则
    "用户明确区分模型能力与 agent 能力",
    "用户对 Linux 服务器的期望: 自动管理软件生命周期",
    "方案对比时必须列出完整问题清单, 不得压缩缺陷",
    "澄清用户表述时先拆成朴素的是非问题",
    "MemoryCore 范围边界(用户多次强调): 只改 memorycore 目录",
])
def test_new_protected_surfaces(entry):
    """新保护面 (Q4 判据 4/5): 用户明确 / 用户对+期望 / 问题清单 / 澄清用户 /
    用户多次强调 → protected。"""
    assert _is_protected_rule(entry, {"importance": 0.8}) is True, entry


def test_protect_override_true_and_false():
    """protect_override 人工加保/解保: true 强制保护, false 解除文本保护。"""
    plain = "普通技术记录: 服务器内存参数。"
    assert _is_protected_rule(plain, {"importance": 0.8}) is False
    assert _is_protected_rule(plain, {"importance": 0.8,
                                      "protect_override": True}) is True
    redline = "红线: 绝不向用户隐藏事实。"
    assert _is_protected_rule(redline, {"importance": 0.8}) is True
    # protect_override=False 不解除红线 (安全方向: 显式 false 只降级文本保护,
    # 红线硬词仍保护 — 与 Q4 判据 3 口径一致)
    assert _is_protected_rule(redline, {"importance": 0.8,
                                        "protect_override": False}) is True


def test_importance_protect_line_unchanged():
    """importance≥0.9 保护线不变 (Q4 判据 1)。"""
    e = "普通规则: 无任何保护词。"
    assert _is_protected_rule(e, {"importance": 0.9}) is True
    assert _is_protected_rule(e, {"importance": 0.89}) is False


def test_user_pref_prefix_head25():
    """用户偏好前缀只认 head25 (Q4 判据 5)。"""
    head = "用户要求呈现复习材料时用扁平短句格式"
    assert _is_protected_rule(head, {"importance": 0.8}) is True
    tail = ("这是一段很长的技术叙述内容, 用于描述服务器硬件配置与内存参数调优"
            "的细节, 与用户要求呈现复习材料时用扁平短句格式无关")
    assert len(tail) > 25 and "用户要求" not in tail[:25]
    assert _is_protected_rule(tail, {"importance": 0.8}) is False


def test_weekly_tidy_respects_v2_protected(tmp_store, mock_client, meta_for,
                                           monkeypatch):
    """smart_tidy 保护面与 Q4 v2 同口径: 泛词不再拦截, 新保护词拦截。"""
    from memorycore import weekly_maintenance as wm
    from conftest import days_ago_str
    # 泛词历史记录 (旧词表会误保护) → 现在可进入候选 (但 LLM 未配置 → 保守跳过,
    # 验证的是保护判定本身不拦)
    vague = f"{days_ago_str(30)} 已删: 打印机驱动冲突。覆盖交互需求已解决。"
    tmp_store.add("memory", vague)
    meta_for("memory").stamp(vague, "state",
                             written_at=datetime.now(timezone.utc)
                             - timedelta(days=30))
    for f in ["规则甲: 用词简洁。", "规则乙: 代码注释用中文。",
              "规则丙: 提交信息写清楚。"]:
        tmp_store.add("memory", f)
    calls = []
    monkeypatch.setattr(wm, "_llm_confirm_sink",
                        lambda e: (calls.append(e), True)[1])
    stat = {"sunk": 0, "merged": 0, "errors": 0, "sink_dry": [],
            "merge_dry": [], "merge_skipped": 0}
    wm.smart_tidy(tmp_store, mock_client, "memory", stat, dry=False)
    assert stat["sunk"] == 1, "泛词不再拦截 → 进入候选并下沉"
    assert calls == [vague]


def test_snapshot_protected_coverage_is_membership_not_frozen_count():
    """快照 protected 断言改为结构不变量: 只做成员判定, 不冻结 14/19。

    具体正反例由上方参数化单测覆盖; 这里验证:
      - protected 条目集合是 rule 条目的子集 (不会把 state 目标误保护);
      - 存在 protected 与非 protected 两类, 且均为成员判定而非计数快照。
    """
    import hashlib
    import json
    from memorycore.core.classifier import classify_entry_type
    fx = Path(__file__).resolve().parent / "fixtures" / "snapshot_20260912"
    entries = [e.strip() for e in
               (fx / "MEMORY.md").read_text(encoding="utf-8").split("\n§\n")
               if e.strip()]
    meta = json.loads((fx / "MEMORY.meta.json").read_text(encoding="utf-8"))
    rule_hashes = set()
    protected_hashes = set()
    for e in entries:
        h = hashlib.sha256(e.encode()).hexdigest()
        if classify_entry_type(e) == "rule":
            rule_hashes.add(h)
            m = meta.get(h, {})
            if _is_protected_rule(e, m):
                protected_hashes.add(h)
    assert protected_hashes, "快照应至少存在 protected 规则"
    assert protected_hashes < rule_hashes, "应同时存在非 protected 规则"
    assert protected_hashes <= rule_hashes, "protected 判定只能落在 rule 集合内"
