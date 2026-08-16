#!/usr/bin/env python3
"""tests/test_direct_write.py — direct-write governance (core + plugin smoke)."""
import importlib.util
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from memorycore.core.metadata import direct_write_govern  # noqa: E402
from conftest import MockMnemosyneClient, days_ago_str  # noqa: E402


def test_direct_write_state_migrated(tmp_store, mock_client, meta_for):
    """State-typed direct write (already in hot tier) -> cold write confirmed
    -> removed from hot tier."""
    entry = f"{days_ago_str(0)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry, action="add")
    assert r["status"] == "migrated_new", r
    assert entry not in tmp_store.entries("memory"), "migrated entry must leave hot"
    assert entry in mock_client.stored


def test_direct_write_rule_stamped(tmp_store, mock_client, meta_for):
    """Rule-typed direct write -> stays hot + stamped origin=hermes."""
    entry = "用户偏好: 极简选型, Go/Rust 单二进制"
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry, action="add")
    assert r["status"] == "stamped_rule", r
    assert entry in tmp_store.entries("memory")
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "rule" and m["origin"] == "hermes"
    assert mock_client.stored == [] and mock_client.recall_queries == []


def test_direct_write_cold_fail_keeps_hot(tmp_store, meta_for):
    """Cold failure -> entry stays hot + state stamp as 7-day backstop."""
    entry = f"{days_ago_str(0)} 拍板: 方案定稿"
    tmp_store.add("memory", entry)
    bad = MockMnemosyneClient(fail_remember=True)
    r = direct_write_govern(tmp_store, bad, "memory", entry, action="add")
    assert r["status"] == "kept_hot_backstop", r
    assert entry in tmp_store.entries("memory"), "cold failure must keep the source"
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "state" and m["origin"] == "hermes", "backstop stamp"


def test_direct_write_cold_unreachable_stamps(tmp_store, meta_for):
    """Cold unreachable (recall raises) -> kept + stamped, never raises."""
    entry = f"{days_ago_str(0)} 已配置: zram swap 调 8GB"
    tmp_store.add("memory", entry)
    bad = MockMnemosyneClient(fail_recall=True)
    r = direct_write_govern(tmp_store, bad, "memory", entry, action="add")
    assert r["status"] == "kept_hot_backstop"
    assert entry in tmp_store.entries("memory")
    assert meta_for("memory").get_entry(entry)["type"] == "state"


def test_on_memory_write_hook(tmp_store):
    """Plugin smoke: on_memory_write routes to governance (isolated via
    module-level replacement, zero host/production side effects).

    LocalStore/ColdStoreClient are replaced in the plugin module namespace;
    the governance logic itself is covered by the tests above, so the worker
    is stubbed here. Assertions: add/replace trigger governance, remove does
    not, and the F5 single-flight worker is shared across a write burst.
    """
    plugin_path = REPO_ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py"
    spec = importlib.util.spec_from_file_location("memorycore_prefetch_smoke",
                                                  plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    mod.LocalStore = lambda: tmp_store      # isolated store (0% -> no overflow)
    mod.ColdStoreClient = lambda *a, **k: MockMnemosyneClient()

    provider = mod.MemoryCorePrefetchProvider()
    govern_calls = []
    overflow_calls = []
    provider._run_govern_bg = lambda t, c, a: govern_calls.append((t, c, a))
    provider._spawn_overflow = lambda t: overflow_calls.append(t)

    # F5: a burst of writes shares one worker thread (no fan-out)
    worker_ids = []
    for i in range(5):
        provider.on_memory_write("add", "memory", f"2026-08-16 已配置: 设置项{i}")
        worker_ids.append(provider._govern_worker)
    provider.on_memory_write("replace", "user", "2026-08-16 拍板: 方案定稿",
                             metadata={"old_text": "旧"})
    provider.on_memory_write("remove", "memory", "", metadata={"old_text": "x"})
    time.sleep(0.5)
    assert [(t, a) for t, c, a in govern_calls] == [
        ("memory", "add")] * 5 + [("user", "replace")], \
        "add/replace trigger governance, remove does not, queue loses nothing"
    assert len({id(w) for w in worker_ids if w is not None}) == 1, \
        "F5: all writes share one worker thread"
    assert provider._govern_queue.empty(), "queue must be drained"
    assert overflow_calls == [], "isolated store at 0% must not trigger overflow"
