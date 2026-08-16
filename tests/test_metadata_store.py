#!/usr/bin/env python3
"""tests/test_metadata_store.py — MetaStore units (reconcile / replace re-type / orphan GC)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from conftest import days_ago_str  # noqa: E402


def test_hermes_replace_reconcile(tmp_store, meta_for):
    """Host edits an entry (replace) -> hash mismatch -> reconcile re-types it
    without losing metadata; removing an entry -> orphan key GC."""
    ms = meta_for("memory")
    ms.stamp("旧内容A", "rule", origin="hermes")
    ms.stamp("旧内容B", "state", origin="hermes")
    # host replaces A with new content
    tmp_store.add("memory", "旧内容A")
    tmp_store.replace("memory", "旧内容A", f"{days_ago_str(9)} 拍板: 新方案定稿")
    # reconcile: new content re-typed state, old keys GC'd
    st = ms.reconcile(tmp_store.entries("memory"))
    assert st["stamped"] == 1 and st["gc"] == 2, "replaced key + B key both GC'd"
    assert ms.get_entry("旧内容A") is None, "replaced content's old key must be GC'd"
    new_meta = ms.get_entry(f"{days_ago_str(9)} 拍板: 新方案定稿")
    assert new_meta["type"] == "state", "new content must be re-typed by current content"
    # host removes the entry -> reconcile GCs the remaining key
    tmp_store.remove("memory", "新方案定稿")
    st2 = ms.reconcile(tmp_store.entries("memory"))
    assert st2["gc"] == 1
    assert ms.get_entry(f"{days_ago_str(9)} 拍板: 新方案定稿") is None


def test_meta_roundtrip_and_corruption(tmp_store, meta_for):
    """Metadata round-trip; a corrupted sidecar degrades to empty, never raises."""
    ms = meta_for("memory")
    ms.stamp("条目X", "rule", origin="store_fact")
    m = ms.get_entry("条目X")
    assert m["type"] == "rule" and m["origin"] == "store_fact"
    assert ms.get_entry("不存在") is None
    # corrupted sidecar does not raise
    ms.meta_path.write_text("{not json", encoding="utf-8")
    assert ms.get_entry("条目X") is None
    assert ms.reconcile(["条目X"])["stamped"] == 1  # rebuilds after corruption
