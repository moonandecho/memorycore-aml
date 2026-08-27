#!/usr/bin/env python3
"""tests/test_direct_write.py — 直写通道治理 (core 核心 + prefetch 插件烟测)。"""
import importlib.util
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.metadata import direct_write_govern  # noqa: E402
from conftest import MockMnemosyneClient, days_ago_str  # noqa: E402


def test_direct_write_state_migrated(tmp_store, mock_client, meta_for):
    """state 型直写 (条目已在热层) → 冷层写成功 → 热层删除。"""
    entry = f"{days_ago_str(0)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry, action="add")
    assert r["status"] == "migrated_new", r
    assert entry not in tmp_store.entries("memory"), "迁移后热层应删除"
    assert entry in mock_client.stored


def test_direct_write_rule_stamped(tmp_store, mock_client, meta_for):
    """rule 型直写 → 条目留热层 + 盖章 origin=hermes。"""
    entry = "用户偏好: 极简选型, Go/Rust 单二进制"
    tmp_store.add("memory", entry)
    r = direct_write_govern(tmp_store, mock_client, "memory", entry, action="add")
    assert r["status"] == "stamped_rule", r
    assert entry in tmp_store.entries("memory")
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "rule" and m["origin"] == "hermes"
    assert mock_client.stored == [] and mock_client.recall_queries == []


def test_direct_write_cold_fail_keeps_hot(tmp_store, meta_for):
    """冷层失败 → 热层保留 + 盖章 state 兜底 (7 天到期由溢流退役)。"""
    entry = f"{days_ago_str(0)} 拍板: 方案定稿"
    tmp_store.add("memory", entry)
    bad = MockMnemosyneClient(fail_remember=True)
    r = direct_write_govern(tmp_store, bad, "memory", entry, action="add")
    assert r["status"] == "kept_hot_backstop", r
    assert entry in tmp_store.entries("memory"), "冷层失败必须保留源"
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "state" and m["origin"] == "hermes", "兜底盖章"


def test_direct_write_cold_unreachable_stamps(tmp_store, meta_for):
    """冷层不可达 (recall 抛异常) → 保留 + 盖章兜底, 不抛异常。"""
    entry = f"{days_ago_str(0)} 已配置: zram swap 调 8GB"
    tmp_store.add("memory", entry)
    bad = MockMnemosyneClient(fail_recall=True)
    r = direct_write_govern(tmp_store, bad, "memory", entry, action="add")
    assert r["status"] == "kept_hot_backstop"
    assert entry in tmp_store.entries("memory")
    assert meta_for("memory").get_entry(entry)["type"] == "state"


def test_on_memory_write_hook(tmp_store):
    """插件烟测: on_memory_write → 直写治理接线 (模块级替换, 零生产副作用)。

    LocalStore/MnemosyneClient 在插件模块命名空间替换为隔离实例,
    _run_govern_bg stub 记录调用 (治理逻辑本身已由上面用例覆盖),
    验证: add/replace 触发治理, remove 不触发, 阈值检查用隔离 store。
    """
    plugin_path = (Path(__file__).resolve().parent.parent
               / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")
    spec = importlib.util.spec_from_file_location("memorycore_prefetch_smoke",
                                                  plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    class _StubStore(tmp_store.__class__):
        pass

    mod.LocalStore = lambda: tmp_store      # 阈值检查隔离 (0% 占用 → 不溢流)
    mod.MnemosyneClient = lambda *a, **k: MockMnemosyneClient()

    provider = mod.MemoryCorePrefetchProvider()
    govern_calls = []
    overflow_calls = []
    provider._run_govern_bg = lambda t, c, a: govern_calls.append((t, c, a))
    provider._spawn_overflow = lambda t: overflow_calls.append(t)

    # F5 修复验证: 突发写入 → 单工作线程串行消费 (不扇出 N 线程)
    worker_ids = []
    for i in range(5):
        provider.on_memory_write("add", "memory", f"2026-08-16 已配置: 设置项{i}")
        worker_ids.append(provider._govern_worker)
    provider.on_memory_write("replace", "user", "2026-08-16 拍板: 方案定稿",
                             metadata={"old_text": "旧"})
    provider.on_memory_write("remove", "memory", "", metadata={"old_text": "x"})
    time.sleep(0.5)
    assert [(t, a) for t, c, a in govern_calls] == [
        ("memory", "add")] * 5 + [("user", "replace")],         "add/replace 触发治理, remove 不触发, 队列不丢"
    assert len({id(w) for w in worker_ids if w is not None}) == 1,         "F5: 单飞闸 — 全部写入共用同一工作线程"
    assert provider._govern_queue.empty(), "队列应已清空"
    assert overflow_calls == [], "隔离 store 0% 占用不得触发溢流"
