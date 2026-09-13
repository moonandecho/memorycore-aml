#!/usr/bin/env python3
"""tests/test_retype_migration.py — §6.2 用例 10: 存量迁移脚本 (DESIGN §Q5)。

覆盖: 干跑 19/6 校验 / apply 迁移 6 条 / 冷层失败兜底 / 幂等 /
     备份 + 回滚逐字节一致 (sha256)。
"""
import hashlib
import shutil
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import retype_20260912 as rt  # noqa: E402

from conftest import MockMnemosyneClient  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402

FX = Path(__file__).resolve().parent / "fixtures" / "snapshot_20260912"
FILES = ["MEMORY.md", "MEMORY.meta.json", "USER.md", "USER.meta.json"]


def _copied_fixture_dir(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    for f in FILES:
        shutil.copy2(FX / f, d / f)
    return d


def _make_store_meta(data_dir):
    store = LocalStore(memory_path=data_dir / "MEMORY.md",
                       user_path=data_dir / "USER.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    return store, ms


def test_dry_run_19_rule_6_state():
    """干跑: 快照 25 条 → 19 rule / 6 state, state 集合 = 6 条目标。"""
    store, ms = _make_store_meta(FX)
    res = rt._dry_run(store, ms, "memory")
    assert res["ok"] is True
    assert res["rule_n"] == 19 and res["state_n"] == 6
    assert set(res["state_hashes"]) == rt.TARGET_STATE_SET


def test_dry_run_is_readonly():
    """干跑零落盘: .md 与 sidecar 内容逐字节不变。"""
    before = {f: (FX / f).read_bytes() for f in FILES}
    store, ms = _make_store_meta(FX)
    rt._dry_run(store, ms, "memory")
    after = {f: (FX / f).read_bytes() for f in FILES}
    assert before == after, "干跑不得修改任何文件"


def _migrated_total(stat):
    return (stat.get("migrated_new", 0) + stat.get("migrated_same", 0)
            + stat.get("migrated_merged", 0))


def _cold_full_texts(client):
    return [c.get("content") for c in getattr(client, "_cold", [])]


def test_apply_migrates_6_and_usage_55(tmp_path):
    """apply 结构不变量: 6 个目标 sha256 全部离开热层、非目标一个不少、
    冷层保有全文、migrated_new+same+merged==6、usage_pct≤55。"""
    d = _copied_fixture_dir(tmp_path)
    store, ms = _make_store_meta(d)
    before_entries = list(store.entries("memory"))
    before_hashes = {hashlib.sha256(e.encode()).hexdigest() for e in before_entries}
    client = MockMnemosyneClient()
    stat = {"errors": 0}
    rt._apply(store, client, ms, "memory", stat)
    assert _migrated_total(stat) == len(rt.TARGET_ENTRIES), stat
    assert stat.get("kept_hot_backstop", 0) == 0 and stat["errors"] == 0
    after_entries = list(store.entries("memory"))
    after_hashes = {hashlib.sha256(e.encode()).hexdigest() for e in after_entries}
    # 结构不变量 1: 目标 sha256 全部不在热层
    assert after_hashes.isdisjoint(rt.TARGET_STATE_SET)
    # 结构不变量 2: 非目标条目一个不少 (字符数差异不冻结)
    assert after_hashes == before_hashes - rt.TARGET_STATE_SET
    assert len(after_entries) == len(before_entries) - len(rt.TARGET_ENTRIES)
    # 结构不变量 3: 冷层每条目标全文可见
    cold_texts = _cold_full_texts(client)
    for h, tag in rt.TARGET_ENTRIES.items():
        original = next(e for e in before_entries
                        if hashlib.sha256(e.encode()).hexdigest() == h)
        assert original in cold_texts, f"{tag} 全文必须在冷层可见"
    # 结构不变量 4: 水位阈值 (55% 是设计目标带, 不冻结具体字符数)
    assert store.usage_pct("memory") <= 55
    ms.reconcile(store.entries("memory"))  # 孤儿键 GC


def test_apply_idempotent_second_run_noop(tmp_path):
    """apply 幂等: 第二次运行零新写入零新删除 (recall 查重 + exact remove)。"""
    d = _copied_fixture_dir(tmp_path)
    store, ms = _make_store_meta(d)
    client = MockMnemosyneClient()
    stat1 = {"errors": 0}
    rt._apply(store, client, ms, "memory", stat1)
    assert _migrated_total(stat1) == len(rt.TARGET_ENTRIES)
    ms.reconcile(store.entries("memory"))
    stored_after_first = len(client.stored)
    stat2 = {"errors": 0}
    rt._apply(store, client, ms, "memory", stat2)
    assert _migrated_total(stat2) == 0, "第二次零迁移 (目标已不在热层)"
    assert stat2.get("already_absent", 0) == len(rt.TARGET_ENTRIES)
    assert stat2.get("errors", 0) == 0
    assert len(client.stored) == stored_after_first, "不双写冷层"


def test_apply_cold_fail_keeps_hot_with_state_stamp(tmp_path):
    """冷层失败: 6 条原样留热层 + 盖章 state 兜底 + errors 计数, 零删除。"""
    d = _copied_fixture_dir(tmp_path)
    store, ms = _make_store_meta(d)
    before_entries = list(store.entries("memory"))
    bad = MockMnemosyneClient(fail_remember=True)
    stat = {"errors": 0}
    rt._apply(store, bad, ms, "memory", stat)
    assert stat["kept_hot_backstop"] == len(rt.TARGET_ENTRIES)
    assert stat["errors"] == len(rt.TARGET_ENTRIES)
    assert store.entries("memory") == before_entries, "冷层失败零删除"
    for i, e in enumerate(before_entries, 1):
        h = hashlib.sha256(e.encode()).hexdigest()
        if h in rt.TARGET_ENTRIES:
            m = ms.get_entry(e)
            assert m and m["type"] == "state", f"目标 #{i} 应盖章 state 兜底"
            assert m.get("type_source") == "manual_migration"


def test_backup_and_rollback_byte_identical(tmp_path):
    """备份 4 文件 + sha256; apply 后从备份恢复 → 逐字节一致 (可回滚)。"""
    d = _copied_fixture_dir(tmp_path)
    store, ms = _make_store_meta(d)
    before = {f: hashlib.sha256((d / f).read_bytes()).hexdigest()
              for f in FILES if (d / f).exists()}
    client = MockMnemosyneClient()
    # 先备份再 apply
    bdir = rt._backup(d, tmp_path / "backups")
    manifest = (bdir / "BACKUP-MANIFEST.json").read_text(encoding="utf-8")
    assert "sha256" in manifest
    for f in FILES:
        assert (bdir / f).exists(), f"备份缺文件 {f}"
        assert (bdir / f).read_bytes() == (d / f).read_bytes(), \
            f"备份 {f} 与源不一致"
    stat = {"errors": 0}
    rt._apply(store, client, ms, "memory", stat)
    assert _migrated_total(stat) == len(rt.TARGET_ENTRIES)
    # 回滚: 停止写入后从备份恢复 .md/.meta.json (原子 replace)
    for f in FILES:
        shutil.copy2(bdir / f, d / f)
    after = {f: hashlib.sha256((d / f).read_bytes()).hexdigest()
             for f in FILES if (d / f).exists()}
    assert after == before, "回滚后 4 文件与迁移前逐字节一致"


def test_restamp_preserves_fields_and_embedded_date(tmp_path):
    """重标保留 importance/weight/last_active_at/cold_id; written_at=内嵌日期;
    #22 已是 state → 只补 type_source。"""
    d = _copied_fixture_dir(tmp_path)
    store, ms = _make_store_meta(d)
    entries = {hashlib.sha256(e.encode()).hexdigest(): e
               for e in store.entries("memory")}
    e20 = entries[[h for h in rt.TARGET_ENTRIES if "#20" in rt.TARGET_ENTRIES[h]][0]]
    meta20 = ms.get_entry(e20)
    rt._restamp(ms, e20, meta20, is_already_state=False)
    m = ms.get_entry(e20)
    assert m["type"] == "state"
    assert m["written_at"].startswith("2026-09-07"), "written_at=内嵌日期"
    assert m["origin"] == "migration_20260912"
    assert m["type_source"] == "manual_migration" and m["schema"] == 2
    assert m["importance"] == meta20.get("importance", 0.8), "importance 保留"
    assert m["weight"] == meta20.get("weight"), "weight 保留"
    e22 = entries[[h for h in rt.TARGET_ENTRIES if "#22" in rt.TARGET_ENTRIES[h]][0]]
    meta22 = ms.get_entry(e22)
    rt._restamp(ms, e22, meta22, is_already_state=True)
    m22 = ms.get_entry(e22)
    assert m22["type"] == "state" and m22["type_source"] == "manual_migration"
    assert m22["written_at"] == meta22["written_at"], "#22 不改 written_at"
    assert m22["origin"] == meta22.get("origin"), "#22 保留原 origin"
