#!/usr/bin/env python3
"""tests/test_get_rule_weight.py — Phase 3 监控工具 memorycore_get_rule_weight。

验证点:
  1. 只列 rule 型条目 (stub/state 不列)
  2. 按 w_eff 升序 (退役顺序), 权重最低者排最前
  3. summary: rule_chars vs rule_budget / over_budget / w_eff min/avg/max
  4. next_eviction_candidates 至多 MAX_EVICT_PER_RUN 条
  5. 只读: 调用前后热层条目/元数据不变
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore import server  # noqa: E402
from memorycore.core.config import MAX_EVICT_PER_RUN, RULE_BUDGET_CHARS  # noqa: E402


def _patch_server(tmp_store, mock_client):
    server._store = tmp_store
    server._client = mock_client


def test_get_rule_weight_sorted_and_scoped(tmp_store, mock_client, meta_for):
    _patch_server(tmp_store, mock_client)
    now = datetime.now(timezone.utc)
    ms = meta_for("memory")
    # 3 条 rule 不同权重 + 1 条 stub + 1 条 state (应只列 rule)
    for e, w in [("规则A: 高权重准则。", 5.0),
                 ("规则B: 中权重准则。", 2.0),
                 ("规则C: 低权重准则。", 0.3)]:
        tmp_store.add("memory", e)
        ms.stamp(e, "rule", weight=w, last_active_at=now)
    tmp_store.add("memory", "[规则指针]主题X→recall(\"主题X\")")
    ms.stamp(tmp_store.entries("memory")[-1], "stub", origin="stub_sink")
    tmp_store.add("memory", "2026-08-01 已配置: 历史状态记录")
    ms.stamp(tmp_store.entries("memory")[-1], "state")
    before_entries = tmp_store.entries("memory")
    r = json.loads(server.memorycore_get_rule_weight("memory"))
    mem = r["memory"]
    assert [x["text"] for x in mem["rules"]] == [
        "规则C: 低权重准则。", "规则B: 中权重准则。", "规则A: 高权重准则。"], mem["rules"]
    assert mem["summary"]["rule_count"] == 3
    assert mem["summary"]["rule_chars"] == sum(
        len(e) for e in before_entries[:3])
    assert mem["summary"]["rule_budget"] == RULE_BUDGET_CHARS
    assert mem["summary"]["over_budget"] is False
    assert mem["summary"]["w_eff_min"] == 0.3
    assert mem["summary"]["w_eff_max"] == 5.0
    assert len(mem["summary"]["next_eviction_candidates"]) <= MAX_EVICT_PER_RUN
    assert mem["summary"]["next_eviction_candidates"][0] == "规则C: 低权重准则。"
    # 只读验证
    assert tmp_store.entries("memory") == before_entries
    m = ms.get_entry("规则C: 低权重准则。")
    assert m["weight"] == 0.3, "只读工具不得改动元数据"


def test_get_rule_weight_over_budget_flag(tmp_store, mock_client, meta_for):
    """超预算场景: over_budget=true, 候选按 w_eff 升序。"""
    _patch_server(tmp_store, mock_client)
    now = datetime.now(timezone.utc)
    ms = meta_for("memory")
    tmp_store.add("memory", "填充填充" + "甲" * (RULE_BUDGET_CHARS + 100))
    ms.stamp(tmp_store.entries("memory")[-1], "rule",
             weight=0.1, last_active_at=now,
             updated_at=now - timedelta(days=100))
    tmp_store.add("memory", "正常规则乙。")
    ms.stamp(tmp_store.entries("memory")[-1], "rule", weight=4.0,
             last_active_at=now)
    r = json.loads(server.memorycore_get_rule_weight("memory"))
    s = r["memory"]["summary"]
    assert s["over_budget"] is True
    assert s["next_eviction_candidates"][0].startswith("填充填充")
