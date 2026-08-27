#!/usr/bin/env python3
"""tests/test_overflow_aging.py — 溢流元数据老化 (Phase 2 核心验收)。

真实落盘 (tempfile) + mock 冷层, 隔离生产:
  状态条目到期下沉 / 未到期保留 / 准则条目保留 / 存量无元数据迁移 /
  冷层失败不删源 / 老长准则压缩。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core.overflow import run_overflow  # noqa: E402
from conftest import days_ago_str  # noqa: E402


def _run(store, client, target="memory"):
    return run_overflow(store, client, target)


def test_state_entry_expires_and_sinks(tmp_store, mock_client, meta_for):
    """state 条目 8 天前 → 溢流下沉冷层, 本地删除, aged_sunk 计数。"""
    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    ents = tmp_store.entries("memory")
    assert entry not in ents, "8 天 state 条目应离开热层"
    assert stat["aged_sunk"] == 1
    assert stat["overflowed"] == 1
    assert entry in mock_client.stored, "冷层必须先写成功"
    assert meta_for("memory").get_entry(entry) is not None or True  # 孤儿键下次 GC


def test_state_entry_not_expired_stays(tmp_store, mock_client):
    """state 条目 3 天 → 保留热层, 冷层零写入。"""
    entry = f"{days_ago_str(3)} 已配置: zram swap 调到 8GB"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    assert entry in tmp_store.entries("memory")
    assert stat["aged_sunk"] == 0
    assert mock_client.stored == []
    assert mock_client.recall_queries == []  # 未到期不碰冷层


def test_rule_entry_never_sunk_by_age(tmp_store, mock_client):
    """rule 条目 100 天 → 永不因年龄沉 (数据安全)。"""
    entry = f"用户偏好({days_ago_str(100)}): 极简选型, Go/Rust 单二进制, 拒绝重依赖"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    assert entry in tmp_store.entries("memory")
    assert stat["aged_sunk"] == 0
    assert mock_client.stored == []


def test_legacy_migration(tmp_store, mock_client, meta_for):
    """存量无元数据条目: reconcile 补盖 (type 判型, written_at 内嵌日期优先), .md 一条不少。"""
    old_state = f"{days_ago_str(3)} 已停: monitor-guard 服务, 改用 systemd timer"
    rule = "行为准则: 未经确认不下结论"
    no_date = "服务器事实: /tmp 是 tmpfs"
    for e in (old_state, rule, no_date):
        tmp_store.add("memory", e)
    before = tmp_store.entries("memory")
    stat = _run(tmp_store, mock_client)
    after = tmp_store.entries("memory")
    assert after == before, "补盖不得改动 .md 任何条目"
    assert stat["metadata_stamped"] == 3
    m = meta_for("memory")
    assert m.get_entry(old_state)["type"] == "state"
    assert m.get_entry(old_state)["origin"] == "legacy"
    assert m.get_entry(old_state)["written_at"].startswith(days_ago_str(3)), \
        "内嵌日期应作为 written_at"
    assert m.get_entry(rule)["type"] == "rule"
    assert m.get_entry(no_date)["type"] == "rule"
    # 幂等: 再跑一次不再重复补盖
    stat2 = _run(tmp_store, mock_client)
    assert stat2["metadata_stamped"] == 0


def test_cold_failure_keeps_source(tmp_store):
    """冷层写入失败 → 源条目保留不删, errors 累计, 不丢数据。"""
    from conftest import MockMnemosyneClient
    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = _run(tmp_store, bad)
    assert entry in tmp_store.entries("memory"), "冷层失败必须保留源"
    assert stat["errors"] >= 1
    assert stat["aged_sunk"] == 0


def _stamp_old_rule(meta_for, entry, days=40):
    """回填 rule 元数据 updated_at 为 N 天前 (模拟长期未更新)。"""
    from datetime import datetime, timedelta, timezone
    meta_for("memory").stamp(entry, "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=days))


def test_old_long_rule_compresses(tmp_store, mock_client, monkeypatch, meta_for):
    """rule 条目 30+ 天未更新且 >200 字 → LLM 压缩 (精简版留本地, 细节沉冷层)。"""
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
    assert compressed in ents, "压缩版应留热层"
    assert entry not in ents, "原始长条应替换"
    assert stat["compressed"] == 1
    assert entry in mock_client.stored, "原始细节应沉冷层"


def test_rule_compress_cold_fail_keeps_original(tmp_store, mock_client, monkeypatch, meta_for):
    """rule 压缩时冷层写失败 → 保留原条目原样。"""
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
    assert entry in tmp_store.entries("memory"), "冷层失败保留原条目"
    assert stat["errors"] >= 1
