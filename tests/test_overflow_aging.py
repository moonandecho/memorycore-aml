#!/usr/bin/env python3
"""tests/test_overflow_aging.py — overflow metadata aging (Phase 2 core).

Real files in tempfile + mock cold tier, isolated:
  state expires and sinks / not expired stays / rule never sinks by age /
  legacy migration / cold failure keeps source / old long rule compresses.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.overflow import run_overflow  # noqa: E402
from conftest import days_ago_str  # noqa: E402


def _run(store, client, target="memory"):
    return run_overflow(store, client, target)


def test_state_entry_expires_and_sinks(tmp_store, mock_client, meta_for):
    """8-day-old state entry -> sinks to cold tier, removed locally, aged_sunk counted."""
    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    ents = tmp_store.entries("memory")
    assert entry not in ents, "8-day state entry must leave the hot tier"
    assert stat["aged_sunk"] == 1
    assert stat["overflowed"] == 1
    assert entry in mock_client.stored, "cold write must succeed first"


def test_state_entry_not_expired_stays(tmp_store, mock_client):
    """3-day-old state entry -> stays hot, zero cold writes."""
    entry = f"{days_ago_str(3)} 已配置: zram swap 调到 8GB"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    assert entry in tmp_store.entries("memory")
    assert stat["aged_sunk"] == 0
    assert mock_client.stored == []
    assert mock_client.recall_queries == []


def test_rule_entry_never_sunk_by_age(tmp_store, mock_client):
    """100-day-old rule entry -> never sinks by age (data safety)."""
    entry = f"用户偏好({days_ago_str(100)}): 极简选型, Go/Rust 单二进制, 拒绝重依赖"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    assert entry in tmp_store.entries("memory")
    assert stat["aged_sunk"] == 0
    assert mock_client.stored == []


def test_legacy_migration(tmp_store, mock_client, meta_for):
    """Untyped legacy entries: reconcile stamps them (embedded date preferred),
    the .md file loses nothing."""
    old_state = f"{days_ago_str(3)} 已停: monitor-guard 服务, 改用 systemd timer"
    rule = "行为准则: 未经确认不下结论"
    no_date = "服务器事实: /tmp 是 tmpfs"
    for e in (old_state, rule, no_date):
        tmp_store.add("memory", e)
    before = tmp_store.entries("memory")
    stat = _run(tmp_store, mock_client)
    after = tmp_store.entries("memory")
    assert after == before, "stamping must not modify the .md entries"
    assert stat["metadata_stamped"] == 3
    m = meta_for("memory")
    assert m.get_entry(old_state)["type"] == "state"
    assert m.get_entry(old_state)["origin"] == "legacy"
    assert m.get_entry(old_state)["written_at"].startswith(days_ago_str(3)), \
        "embedded date should become written_at"
    assert m.get_entry(rule)["type"] == "rule"
    assert m.get_entry(no_date)["type"] == "rule"
    # idempotent: a second run stamps nothing new
    stat2 = _run(tmp_store, mock_client)
    assert stat2["metadata_stamped"] == 0


def test_cold_failure_keeps_source(tmp_store):
    """Cold write failure -> source entry stays, errors counted, no data loss."""
    from conftest import MockMnemosyneClient
    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = _run(tmp_store, bad)
    assert entry in tmp_store.entries("memory"), "cold failure must keep the source"
    assert stat["errors"] >= 1
    assert stat["aged_sunk"] == 0


def _stamp_old_rule(meta_for, entry, days=40):
    """Backdate a rule's updated_at N days (simulates long silence)."""
    from datetime import datetime, timedelta, timezone
    meta_for("memory").stamp(entry, "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=days))


def test_old_long_rule_compresses(tmp_store, mock_client, monkeypatch, meta_for):
    """Rule 30+ days without update and >200 chars -> LLM compression
    (compressed version stays local, details sink to cold)."""
    from memorycore.core import overflow as ov
    filler = "".join(f"这是第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。" for i in range(1, 8))
    entry = (f"用户偏好({days_ago_str(40)}): 选型原则是极简, 单二进制部署, "
             f"拒绝重依赖。" + filler)
    assert len(entry) > 200
    tmp_store.add("memory", entry)
    _stamp_old_rule(meta_for, entry)
    compressed = "用户偏好: 极简选型, 单二进制部署, 拒绝重依赖 (压缩精简版, 保留核心结论)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    stat = _run(tmp_store, mock_client)
    ents = tmp_store.entries("memory")
    assert compressed in ents, "compressed version must stay local"
    assert entry not in ents, "original long entry must be replaced"
    assert stat["compressed"] == 1
    assert entry in mock_client.stored, "original details must sink to cold"


def test_rule_compress_cold_fail_keeps_original(tmp_store, mock_client, monkeypatch, meta_for):
    """Compression cold-write failure -> original entry kept unchanged."""
    from memorycore.core import overflow as ov
    from conftest import MockMnemosyneClient
    filler = "".join(f"补充细节第{i}条, 过程描述与背景解释, 压缩时应当删除的冗余内容。" for i in range(1, 9))
    entry = f"用户偏好({days_ago_str(40)}): 极简选型原则。" + filler
    assert len(entry) > 200
    tmp_store.add("memory", entry)
    _stamp_old_rule(meta_for, entry)
    monkeypatch.setattr(ov, "_llm_compress",
                        lambda client, e: "压缩版内容压缩版内容压缩版内容压缩版内容")
    bad = MockMnemosyneClient(fail_remember=True)
    stat = _run(tmp_store, bad)
    assert entry in tmp_store.entries("memory"), "cold failure keeps the original"
    assert stat["errors"] >= 1
