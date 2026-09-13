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


def test_state_entry_not_sunk_by_age_without_budget(tmp_store, mock_client,
                                                   meta_for):
    """CACHE-POLICY-V2: state 无 TTL 直接换出分支; 低占用下保留热层。

    必须能换出 ≠ 到龄自动换出; 换出统一由活性+预算决定 (预算测试另测)。
    """
    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    stat = _run(tmp_store, mock_client)
    ents = tmp_store.entries("memory")
    assert entry in ents, "无预算压力时 state 不再因 TTL 自动冷迁"
    assert stat["aged_sunk"] == 0
    assert mock_client.stored == []


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


def test_legacy_v2_completion_words_stamped_state(tmp_store, mock_client, meta_for):
    """v2 (2026-09-12): 新完成态词 (已部署/决定不做) 的 legacy 条目补盖为 state。"""
    e1 = f"{days_ago_str(3)} demo-host 已部署 巡检脚本 每周自动更新"
    e2 = f"{days_ago_str(3)} 调研: 无现成方案, 决定不做"
    for e in (e1, e2):
        tmp_store.add("memory", e)
    stat = _run(tmp_store, mock_client)
    assert stat["metadata_stamped"] == 2
    m = meta_for("memory")
    assert m.get_entry(e1)["type"] == "state", "已部署 → v2 补盖 state"
    assert m.get_entry(e2)["type"] == "state", "决定不做 → v2 补盖 state"
    assert m.get_entry(e1).get("type_source") == "judge_v3", \
        "reconcile 标注判型来源: v3 生效时必须是 judge_v3 (不放宽断言)"
    # 未到 TTL (3 天) → 仍留热层 (state TTL 路径不变)
    assert e1 in tmp_store.entries("memory") and e2 in tmp_store.entries("memory")


def test_cold_failure_keeps_source(tmp_store, meta_for):
    """冷层写失败 → 统一预算路径源条目保留不删, errors/cold_errors 累计。"""
    from conftest import MockMnemosyneClient
    from memorycore.core.config import RULE_BUDGET_CHARS
    from datetime import datetime, timezone
    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    filler = "填充甲" + "乙" * (RULE_BUDGET_CHARS + 100)
    tmp_store.add("memory", filler)
    meta_for("memory").stamp(filler, "rule", weight=0.1,
                             updated_at=datetime.now(timezone.utc))
    bad = MockMnemosyneClient(fail_remember=True)
    stat = _run(tmp_store, bad)
    ents = tmp_store.entries("memory")
    assert entry in ents and filler in ents, "冷层失败必须保留源 (零删除)"
    assert stat["errors"] >= 1 and stat["cold_errors"] >= 1
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


def test_plateau_cold_backstop_reported(tmp_store, meta_for):
    """Q6 (G-1): 冷层不可达且 usage>40% → plateau_reason=cold_backstop。"""
    from conftest import MockMnemosyneClient
    entry = "历史记录: " + "细节" * 1300
    assert len(entry) > 2000, "需超过 40% 水位 (5000*0.4)"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", type_override="state")
    assert tmp_store.usage_pct("memory") > 40
    bad = MockMnemosyneClient(fail_recall=True)
    stat = _run(tmp_store, bad)
    assert stat["cold_errors"] >= 1, stat
    assert stat["plateau_reason"] == "cold_backstop", stat
    assert entry in tmp_store.entries("memory"), "冷层失败必须保留源"
