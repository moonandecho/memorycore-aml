#!/usr/bin/env python3
"""tests/test_e_visible.py — E 节静默降级治理回归测试 (Phase 5)

原则: 降级可以, 但必须可见 (计数 / 日志 / 报告字段 / 该失败就失败)。
覆盖: E3 回收站写失败计数+阻断删除 / E4 活性扫描失败计数 /
      E5 merge 路径嵌入失败对齐 embed_fail / E8 env 开关惰性解析。
"""
import pytest

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import overflow as ov_mod  # noqa: E402
from memorycore.core import metadata as meta_mod  # noqa: E402
from conftest import MockMnemosyneClient  # noqa: E402


# ---- E3: 回收站写失败 → 计数 + 不许继续删源 --------------------------------

def test_e3_trash_add_failure_blocks_forget():
    """回收站写失败 → forget 不得执行 (可恢复性保护), stat["trash_fail"] 计数。"""
    from memorycore.core import maintenance as maint

    class FailingTrash:
        def add(self, *a, **k):
            raise OSError("disk full (mock)")

        def get_all(self):
            return []

        def get_expired(self):
            return []

        def count(self):
            return 0

        def remove(self, *a):
            return True

    client = MockMnemosyneClient()
    stat = {}
    entries = [{"id": "x1", "content": "旧条目", "importance": 0.1,
                "last_recalled": "2020-01-01T00:00:00Z"}]
    forgotten, errors = maint._forget_decayed(client, entries,
                                              trash=FailingTrash(), stat=stat)
    assert forgotten == 0
    assert client.forgotten == [], "回收站写失败时不许 forget (删源被阻断)"
    assert stat["trash_fail"] == 1


def test_e3_add_observed_helper(tmp_path):
    from pathlib import Path

    from memorycore.trash_store import TrashStore, add_observed

    class FailingTrash(TrashStore):
        def add(self, *a, **k):
            raise OSError("disk full (mock)")

    stat = {}
    assert add_observed(FailingTrash(), "m1", "content", "r", "s", stat) is False
    assert stat["trash_fail"] == 1

    # 真实写失败场景 (终审低危修复): 原断言传 str 路径, 靠 str 无 with_suffix
    # 的 AttributeError 巧合通过 — 触发方式非预期且语义失真 (并在 /tmp 留垃圾)。
    # 现改为: 父路径是普通文件 → lock 文件 mkdir 抛 FileExistsError (真 OSError),
    # 触发"回收站磁盘写失败"预期语义; tmp_path 自动清理。
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    ok_stat = {}
    assert add_observed(TrashStore(path=Path(blocker) / "trash.json"),
                        "m2", "content", "r", "s", ok_stat) is False
    assert ok_stat["trash_fail"] == 1


def test_e3_merge_duplicates_reversal_trash_first(monkeypatch):
    """终审 R1 回归: 规则反转分支必须回收站先写成功才许 forget — 写失败 →
    计数 stat["trash_fail"] + 阻断删除 (不再出现先删后备份)。"""
    from memorycore.core import maintenance as maint
    from memorycore.trash_store import TrashStore, add_observed

    entries = [
        {"id": "c0", "content": "沟通时使用中文。",
         "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "c1", "content": "停止沟通时使用中文。",
         "timestamp": "2026-02-01T00:00:00Z"},
    ]
    client = MockMnemosyneClient(cold_items=[
        {"content": entries[0]["content"], "dense_score": 0.9},
        {"content": entries[1]["content"], "dense_score": 0.9},
    ])
    stat = {}
    monkeypatch.setattr(TrashStore, "add",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("disk full (mock)")))
    merged, _, rev, _, to_forget = maint._merge_duplicates(client, entries, stat)
    assert merged == 0
    assert rev == 0
    assert client.forgotten == [], "回收站写失败时反转分支不许 forget (先删后备份禁止)"
    assert not to_forget, "写失败时不得标记已删 (下轮应重试)"
    assert stat["trash_fail"] == 1


def test_e3_merge_duplicates_hashdedup_trash_failure_blocks_forget(monkeypatch):
    """终审 R2 回归: hash-dedup 分支回收站写失败 → add_observed 计数 + 阻断
    forget (victim 保留, 下轮重试; 原裸 add 零计数零告警)。"""
    from memorycore.core import maintenance as maint
    from memorycore.trash_store import TrashStore, add_observed

    entries = [
        {"id": "c0", "content": "沟通时使用中文。",
         "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "c1", "content": "沟通时使用中文。",
         "timestamp": "2026-01-02T00:00:00Z"},
    ]
    client = MockMnemosyneClient(cold_items=[])  # 无向量邻居 → 只走哈希兜底
    stat = {}
    monkeypatch.setattr(TrashStore, "add",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("disk full (mock)")))
    merged, _, rev, _, to_forget = maint._merge_duplicates(client, entries, stat)
    assert merged == 0
    assert client.forgotten == [], "回收站写失败时 hash-dedup 不许 forget"
    assert not to_forget, "写失败时不得标记已删 (下轮应重试)"
    assert stat["trash_fail"] == 1


# ---- E4: 活性扫描失败 → 计数 + 日志 (不再零信号) ---------------------------

def test_e4_activity_scan_failure_counted(tmp_store, mock_client, monkeypatch,
                                          caplog):
    monkeypatch.setattr(ov_mod, "apply_activity_hits",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    tmp_store.add("memory", "规则甲: 用词简洁。")
    stat = ov_mod.run_overflow(tmp_store, mock_client, "memory")
    assert stat["errors"] >= 1, "活性扫描失败必须计数 (原 except: pass 零信号)"
    assert "活性命中扫描失败" in caplog.text


# ---- E5: merge 路径嵌入失败 → 对齐 activity 路径 embed_fail ----------------

def test_e5_merge_embed_fail_counted(monkeypatch):
    monkeypatch.setattr(ov_mod, "_embed_batch", lambda texts: None)
    stat = {}
    merged, count = ov_mod._merge_local_fragments(
        ["规则甲: 用词简洁, 注释用中文。", "规则甲: 用词简洁, 注释中文。"], stat)
    assert stat["embed_fail"] == 1, "merge 路径嵌入失败必须对齐 embed_fail 计数"
    # 无 stat 时静默降级 (向后兼容), 不崩溃
    ov_mod._merge_local_fragments(
        ["规则甲: 用词简洁, 注释用中文。", "规则甲: 用词简洁, 注释中文。"])


# ---- E8: 4 个 env 开关惰性解析 (修 import 冻结) -----------------------------

def test_e8_env_switches_lazy(monkeypatch):
    # conftest autouse 会把 config.ACTIVITY_LOG_ENABLED 钉为实例属性,
    # 先移除让惰性读取回到 env 通道 (monkeypatch 会自动恢复)
    monkeypatch.delattr(config_mod, "ACTIVITY_LOG_ENABLED", raising=False)
    monkeypatch.setenv("ACTIVITY_LOG_ENABLED", "1")
    monkeypatch.setenv("MEMORYCORE_RULE_BUDGET_ENABLED", "1")
    monkeypatch.setenv("MEMORYCORE_HIT_WEAK_MODE", "degraded")
    monkeypatch.setenv("MEMORYCORE_EMBED_BACKEND", "mnemosyne")
    assert ov_mod.ACTIVITY_LOG_ENABLED is True
    assert ov_mod.RULE_BUDGET_ENABLED is True
    assert ov_mod.HIT_WEAK_MODE == "degraded"
    assert ov_mod.EMBED_BACKEND == "mnemosyne"
    assert meta_mod.ACTIVITY_LOG_ENABLED is True

    # 运行中改 env → 即时生效 (import 时冻结模式这里仍是旧值)
    monkeypatch.setenv("ACTIVITY_LOG_ENABLED", "0")
    monkeypatch.setenv("MEMORYCORE_EMBED_BACKEND", "off")
    assert ov_mod.ACTIVITY_LOG_ENABLED is False
    assert ov_mod.EMBED_BACKEND == "off"
    assert meta_mod.ACTIVITY_LOG_ENABLED is False

    # monkeypatch.setattr 覆盖契约不变 (模块实例属性遮蔽惰性读取)
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    assert ov_mod.ACTIVITY_LOG_ENABLED is True
    assert meta_mod.ACTIVITY_LOG_ENABLED is True
