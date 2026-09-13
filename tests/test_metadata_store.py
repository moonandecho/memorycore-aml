#!/usr/bin/env python3
"""tests/test_metadata_store.py — MetaStore 单元 (reconcile/替换重判/孤儿GC)。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import days_ago_str  # noqa: E402


def test_hermes_replace_reconcile(tmp_store, meta_for):
    """Hermes 直改条目 (replace) → hash mismatch → reconcile 重判不丢元数据;
    删条目 → 孤儿键 GC。"""
    ms = meta_for("memory")
    ms.stamp("旧内容A", "rule", origin="hermes")
    ms.stamp("旧内容B", "state", origin="hermes")
    # Hermes replace A → 新内容
    tmp_store.add("memory", "旧内容A")
    tmp_store.replace("memory", "旧内容A", f"{days_ago_str(9)} 拍板: 新方案定稿")
    # reconcile: 新内容判型 state, 旧键 GC
    st = ms.reconcile(tmp_store.entries("memory"))
    assert st["stamped"] == 1 and st["gc"] == 2, "两个旧键 (A 替换后 + B) 都应 GC"
    assert ms.get_entry("旧内容A") is None, "被替换内容的旧键应 GC"
    new_meta = ms.get_entry(f"{days_ago_str(9)} 拍板: 新方案定稿")
    assert new_meta["type"] == "state", "新内容应按当前内容重新判型"
    # Hermes remove → reconcile GC 剩余键
    tmp_store.remove("memory", "新方案定稿")
    st2 = ms.reconcile(tmp_store.entries("memory"))
    assert st2["gc"] == 1
    assert ms.get_entry(f"{days_ago_str(9)} 拍板: 新方案定稿") is None


def test_meta_roundtrip_and_corruption(tmp_store, meta_for):
    """元数据 round-trip; sidecar 损坏 → 降级返回空, 不抛异常。"""
    ms = meta_for("memory")
    ms.stamp("条目X", "rule", origin="store_fact")
    m = ms.get_entry("条目X")
    assert m["type"] == "rule" and m["origin"] == "store_fact"
    assert ms.get_entry("不存在") is None
    # 损坏的 sidecar 不抛异常
    ms.meta_path.write_text("{not json", encoding="utf-8")
    assert ms.get_entry("条目X") is None
    assert ms.reconcile(["条目X"])["stamped"] == 1  # 损坏后重建


def test_reconcile_writes_ambiguous_hold_fields(tmp_store, meta_for):
    """SAFE-JUDGE v3: 新条目 ambiguous → type=rule + review_at, 不冷迁。"""
    ms = meta_for("memory")
    tmp_store.add("memory", "已通知用户验收结果")
    st = ms.reconcile(tmp_store.entries("memory"))
    assert st["stamped"] == 1
    m = ms.get_entry("已通知用户验收结果")
    assert m["type"] == "rule"
    assert m["judge_decision"] == "ambiguous"
    assert m["judge_review_at"]
    assert m["type_source"] == "judge_v3_ambiguous"


def test_reconcile_existing_keys_not_rejudged(tmp_store, meta_for):
    """已有键不重判: 人工/旧章保留, 不因 v3 重新判型或写放大。"""
    ms = meta_for("memory")
    e = "2026-09-01 已完成部署。已通知用户验收结果"
    ms.stamp(e, "rule", origin="manual", type_source="manual_override",
             type_override="rule")
    st = ms.reconcile([e])
    assert st["stamped"] == 0
    m = ms.get_entry(e)
    assert m["type"] == "rule" and m["origin"] == "manual"
    assert m["type_source"] == "manual_override"
    assert "judge_decision" not in m
