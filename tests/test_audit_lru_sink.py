#!/usr/bin/env python3
"""tests/test_audit_lru_sink.py — 缺口2: audit 活性维度可沉判定。

验证点:
  1. 低 weight + 老 last_active_at + 非 protected rule → sink_candidate
     + sink_reason="low_weight+inactive", 计入 lru_sink_candidates
  2. protected (红线词) 即使低权重+久远 → 不标 (保护线)
  3. 高 weight 或近期活跃 → 不标
  4. 现有 audit 字段不破 (plan/keep/weight/w_eff 仍输出)
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore import server  # noqa: E402


def _patch_server(tmp_store, mock_client):
    server._store = tmp_store
    server._client = mock_client


def test_audit_marks_low_weight_inactive_rule(tmp_store, mock_client, meta_for):
    """低权重 + 30 天未活跃 + 非保护 → sink_candidate + sink_reason。"""
    _patch_server(tmp_store, mock_client)
    old = datetime.now(timezone.utc) - timedelta(days=45)
    e1 = "低频准则甲: 早期写作细节要求。"
    tmp_store.add("memory", e1)
    meta_for("memory").stamp(e1, "rule", origin="hermes", weight=0.5,
                             last_active_at=old,
                             updated_at=datetime.now(timezone.utc) - timedelta(days=100))
    r = json.loads(server.memorycore_memory_audit("memory"))
    rows = r["memory"]["rows"]
    assert len(rows) == 1
    row = rows[0]
    assert row["sink_candidate"] is True, row
    assert row["sink_reason"] == "low_weight+inactive"
    assert r["memory"]["lru_sink_candidates"] == 1


def test_audit_protected_not_marked(tmp_store, mock_client, meta_for):
    """protected (红线类) 低权重+久远 → 不标 sink_candidate (保护线不误伤)。"""
    _patch_server(tmp_store, mock_client)
    old = datetime.now(timezone.utc) - timedelta(days=45)
    e1 = "红线: 绝不向用户隐藏事实, 零容忍。"
    tmp_store.add("memory", e1)
    meta_for("memory").stamp(e1, "rule", origin="hermes", weight=0.5,
                             last_active_at=old)
    r = json.loads(server.memorycore_memory_audit("memory"))
    row = r["memory"]["rows"][0]
    assert row.get("sink_candidate") is not True, row
    assert r["memory"]["lru_sink_candidates"] == 0


def test_audit_high_weight_or_recent_not_marked(tmp_store, mock_client, meta_for):
    """高权重 或 近期活跃 → 不标。"""
    _patch_server(tmp_store, mock_client)
    now = datetime.now(timezone.utc)
    e1 = "准则乙: 高权重但久远。"
    tmp_store.add("memory", e1)
    meta_for("memory").stamp(e1, "rule", weight=4.0,
                             last_active_at=now - timedelta(days=45))
    e2 = "准则丙: 低权重但近期活跃。"
    tmp_store.add("memory", e2)
    meta_for("memory").stamp(e2, "rule", weight=0.4, last_active_at=now)
    r = json.loads(server.memorycore_memory_audit("memory"))
    rows = {row["text"]: row for row in r["memory"]["rows"]}
    assert rows[e1[:40]].get("sink_candidate") is not True
    assert rows[e2[:40]].get("sink_candidate") is not True
    assert r["memory"]["lru_sink_candidates"] == 0


def test_audit_existing_fields_intact(tmp_store, mock_client, meta_for):
    """现有字段不破: keep/plan/weight/w_eff/last_active_at 仍输出。"""
    _patch_server(tmp_store, mock_client)
    e1 = "准则丁: 常规准则, 结论先行。"
    tmp_store.add("memory", e1)
    meta_for("memory").stamp(e1, "rule", weight=2.0)
    r = json.loads(server.memorycore_memory_audit("memory"))
    row = r["memory"]["rows"][0]
    assert "keep" in row and "plan" in row
    assert "weight" in row and "w_eff" in row and "last_active_at" in row
    assert "rule_chars" in r["memory"] and "rule_budget" in r["memory"]
