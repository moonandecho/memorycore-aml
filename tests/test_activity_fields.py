#!/usr/bin/env python3
"""tests/test_activity_fields.py — §6.2 用例 9: 活性分级字段落盘 (2026-09-12)。

  ① 强命中写 last_strong_hit_at (apply_activity_hits 语义嵌入路径)
  ② 弱命中 (降级词法) 写 last_weak_hit_at 且不刷新 last_active_at
  ③ 分级输入回读: _rule_activity_tier 按两戳 + 现场词法给三级门槛
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import overflow as ov  # noqa: E402


def _rule(tmp_store, meta_for, text, days=100):
    tmp_store.add("memory", text)
    meta_for("memory").stamp(text, "rule",
                             updated_at=datetime.now(timezone.utc)
                             - timedelta(days=days),
                             last_active_at=datetime.now(timezone.utc)
                             - timedelta(days=days))
    return text


def test_strong_hit_writes_strong_stamp(tmp_store, meta_for, tmp_path,
                                        monkeypatch):
    """强命中: last_strong_hit_at 落盘, last_active_at 刷新。"""
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "act.jsonl")
    from memorycore.core import config as config_mod
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    e = _rule(tmp_store, meta_for, "规则甲: 服务器共享目录配置。")
    meta_mod.log_activity_query("服务器怎么共享文件夹")

    class FakeClient:
        def embed_texts(self, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)

    stat = {}
    ov.apply_activity_hits({"memory": meta_for("memory")},
                           {"memory": [e]}, FakeClient(), stat)
    m = meta_for("memory").get_entry(e)
    assert m.get("last_strong_hit_at"), "强命中应写 last_strong_hit_at"
    assert not m.get("last_weak_hit_at"), "强命中不写弱戳"
    old_active = datetime.now(timezone.utc) - timedelta(days=100)
    assert m.get("last_active_at") != old_active.isoformat(), \
        "强命中仍刷新 last_active_at"


def test_degraded_weak_hit_writes_weak_stamp_no_anchor_refresh(tmp_store,
                                                               meta_for,
                                                               tmp_path,
                                                               monkeypatch):
    """降级词法弱命中: last_weak_hit_at 落盘, last_active_at 不刷新。"""
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "act.jsonl")
    from memorycore.core import config as config_mod
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    e = _rule(tmp_store, meta_for, "规则乙: 打印机型号与耗材。")
    old_active = datetime.now(timezone.utc) - timedelta(days=100)
    meta_for("memory").stamp(e, "rule", last_active_at=old_active,
                             updated_at=old_active)
    meta_mod.log_activity_query("打印机怎么设置双面打印")  # sb≥2 词法命中
    stat = {}
    # embed 不可用 → 降级纯词法弱命中
    ov.apply_activity_hits({"memory": meta_for("memory")},
                           {"memory": [e]},
                           _NoEmbedClient(), stat)
    m = meta_for("memory").get_entry(e)
    assert m.get("last_weak_hit_at"), "弱命中应写 last_weak_hit_at"
    assert not m.get("last_strong_hit_at"), "弱命中不写强戳"
    assert m.get("last_active_at") == old_active.isoformat(), \
        "弱命中不刷新 last_active_at (refresh_anchor=False)"


class _NoEmbedClient:
    def embed_texts(self, texts):
        return None


def test_activity_tier_reads_stamps(tmp_store, meta_for):
    """_rule_activity_tier: strong→active/30, weak→warm/14, 现场词法→warm, 缺→idle/7。"""
    now = datetime.now(timezone.utc)
    m_strong = {"type": "rule",
                "last_strong_hit_at": (now - timedelta(days=1)).isoformat()}
    assert ov._rule_activity_tier(m_strong, "x", [], now) == ("active", 30)
    m_weak = {"type": "rule",
              "last_weak_hit_at": (now - timedelta(days=1)).isoformat()}
    assert ov._rule_activity_tier(m_weak, "x", [], now) == ("warm", 14)
    m_old_strong = {"type": "rule",
                    "last_strong_hit_at": (now - timedelta(days=8)).isoformat()}
    assert ov._rule_activity_tier(m_old_strong, "x", [], now) == ("idle", 7), \
        "超 7 天的强命中不再算 active"
    m_none = {"type": "rule"}
    assert ov._rule_activity_tier(m_none, "x", [], now) == ("idle", 7)
    assert ov._rule_activity_tier(
        m_none, "打印机型号与耗材",
        ["打印机怎么设置双面打印"], now) == ("warm", 14), "现场词法命中 → warm"
