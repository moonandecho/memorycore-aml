#!/usr/bin/env python3
"""tests/test_merge_two_entries_dedup.py — P8 + _merge_two_entries 吞并 bug 回归。

覆盖 (任务书 memorycore-p8-implement-report 依据):
  Bug A: _merge_two_entries 句末标点 → 空串成员误判重复 (冷层独有细节丢失)
  B1: _handle_rule_stub_sink remember 前查重 (same/similar/无匹配/失败保留)
  B2: 压缩直写 (typed rule 分支 + legacy 分支) remember 前查重

隔离模式同 conftest (tmp_store/mock_client/meta_for, 绝不碰生产数据)。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import metadata as meta_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import STUB_PREFIX  # noqa: E402
from memorycore.core.overflow import run_overflow  # noqa: E402
from conftest import MockMnemosyneClient, days_ago_str  # noqa: E402


# ---- 工具 ----------------------------------------------------------------

def _fill_to(tmp_store, target, pct):
    """填充一条大 rule 条目把占用推到目标百分比 (rule 型: 溢流不动它)。"""
    limit = 5000
    want = int(limit * pct) + 30
    need = max(0, want - tmp_store.char_count(target))
    if need:
        tmp_store.add(target, "填充条目" + "甲" * need)


def _stamp_rule(meta_for, entry, days, importance=0.8):
    meta_for("memory").stamp(
        entry, "rule",
        updated_at=datetime.now(timezone.utc) - timedelta(days=days),
        importance=importance)


def _stub_sink(store, client, entry, meta_for):
    """直接调 _handle_rule_stub_sink (精确控制冷层三态, 不走 run_overflow 干扰)。"""
    stat = {"errors": 0, "stubbed": 0}
    ov._handle_rule_stub_sink(store, client, "memory", entry,
                              meta_for("memory"), stat, [])
    return stat


def _setup_activity(monkeypatch, tmp_path, queries):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    for q in queries:
        meta_mod.log_activity_query(q)


def _judge_all_dormant(monkeypatch):
    monkeypatch.setattr(ov, "_llm_judge_dormant",
                        lambda entries, queries: {e: True for e in entries})


class FailUpdateClient(MockMnemosyneClient):
    """update 恒失败 (模拟冷层 update 非 updated 响应)。"""

    def update(self, memory_id, content, importance=None):
        self.updated.append((memory_id, content))
        return {"status": "error", "error": "update failed (mock)"}


# ---- Bug A: _merge_two_entries 吞并 bug ----------------------------------

def test_merge_local_ends_with_period_preserves_cold_detail():
    """实测 repro: local 句末标点 → 空串成员使 "" in s 恒 True →
    冷层独有句子全被误判重复。修复后必须保留冷层独有细节。"""
    merged = ov._merge_two_entries("A记录。", "B记录。新增信息。")
    assert "A记录" in merged, merged
    assert "B记录" in merged, "冷层独有句子不得丢失"
    assert "新增信息" in merged, "冷层独有细节不得被吞并"


def test_merge_local_no_terminal_punct_behavior_unchanged():
    """local 不带句末标点 (既有正确行为) 不回归: 重叠句去重 + 新句保留。"""
    merged = ov._merge_two_entries("A记录", "A记录。新增信息。")
    assert "新增信息" in merged, merged
    assert merged.count("A记录") == 1, "重叠句只保留一份"
    # 同主题近似句 (ratio > 0.8) 仍判重复
    merged2 = ov._merge_two_entries("服务器内存参数记录。", "服务器内存参数记录")
    assert merged2 == "服务器内存参数记录。", merged2


def test_merge_empty_local_returns_cold():
    """空 local (无句子可匹配) → 冷层句子全部保留 (不因空串误判)。"""
    merged = ov._merge_two_entries("", "新增信息。")
    assert "新增信息" in merged, merged


def test_merge_whitespace_only_base_sentences_filtered():
    """local 仅句末标点/空白 → 无有效句子, 冷层全部算新 (同空串边界)。"""
    merged = ov._merge_two_entries("。", "B记录。新增信息。")
    assert "B记录" in merged and "新增信息" in merged, merged


def test_merge_local_multi_punct_split_filters_empties():
    """local 含连续标点/换行 → split 空串成员被过滤, 不污染去重。"""
    merged = ov._merge_two_entries("A记录。\nB记录;", "新增信息。")
    assert "新增信息" in merged, merged


def test_merge_cold_empty_returns_base():
    """冷层空文本 → 无新增, 返回 local 原样。"""
    assert ov._merge_two_entries("A记录。", "") == "A记录。"


# ---- B1: stub-sink remember 前查重 ----------------------------------------

def test_stub_sink_cold_same_no_duplicate_remember(tmp_store, mock_client,
                                                   meta_for):
    """cold-same: 不重复 remember, stub 带冷层已有 id (restore 链路可命中)。"""
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = _stub_sink(tmp_store, cold, entry, meta_for)
    ents = tmp_store.entries("memory")
    stubs = [e for e in ents if e.startswith(STUB_PREFIX)]
    assert entry not in ents, "全文已等价于冷层 → 本地换 stub"
    assert len(stubs) == 1 and stat["stubbed"] == 1, stat
    assert cold.stored == [], "冷层已有等价全文 → 不重复 remember"
    m = meta_for("memory").get_entry(stubs[0])
    assert m and m["cold_id"] == "c0", "stub 必须指向冷层真实存在的 id"
    assert m["origin"] == "stub_sink" and m["type"] == "stub"


def test_stub_sink_cold_similar_merge_update(tmp_store, meta_for):
    """cold-similar: merge-update (merged 含冷层独有句) → stub 用 matched id。"""
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    cold_content = ("自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
                    "补充细节: 部署在服务器上的应用保持单二进制形态。")
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    cold = MockMnemosyneClient(cold_items=[{"content": cold_content,
                                            "dense_score": 0.6}])
    stat = _stub_sink(tmp_store, cold, entry, meta_for)
    ents = tmp_store.entries("memory")
    stubs = [e for e in ents if e.startswith(STUB_PREFIX)]
    assert len(stubs) == 1 and stat["stubbed"] == 1, stat
    assert cold.stored == [], "similar 走 merge-update, 不 remember"
    assert len(cold.updated) == 1, cold.updated
    assert cold.updated[0][0] == "c0", "update 必须命中 matched id"
    assert "补充细节" in cold.updated[0][1], "merged 保留冷层独有句子 (Bug A 回归)"
    m = meta_for("memory").get_entry(stubs[0])
    assert m and m["cold_id"] == "c0"


def test_stub_sink_no_match_remembers(tmp_store, mock_client, meta_for):
    """无匹配: 现状 remember(0.6) → stub, cold_id = 新 memory_id。"""
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    stat = _stub_sink(tmp_store, mock_client, entry, meta_for)
    ents = tmp_store.entries("memory")
    stubs = [e for e in ents if e.startswith(STUB_PREFIX)]
    assert len(stubs) == 1 and stat["stubbed"] == 1, stat
    assert entry in mock_client.stored, "无匹配 → 现状 remember"
    m = meta_for("memory").get_entry(stubs[0])
    assert m and m["cold_id"] == "m1", "cold_id 必须来自 remember 返回"


def test_stub_sink_recall_fail_keeps_original(tmp_store, meta_for):
    """冷层不可达 (recall 异常) → 保留原样 errors, 不 stub (保守语义)。"""
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    bad = MockMnemosyneClient(fail_recall=True)
    stat = _stub_sink(tmp_store, bad, entry, meta_for)
    assert entry in tmp_store.entries("memory"), "冷层不可达 → 原条目原样"
    assert stat["stubbed"] == 0 and stat["errors"] >= 1, stat
    assert bad.stored == [] and not [e for e in tmp_store.entries("memory")
                                     if e.startswith(STUB_PREFIX)]


def test_stub_sink_update_fail_keeps_original(tmp_store, meta_for):
    """similar 但 update 失败 → 保留原样 errors, 不 stub。"""
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    cold_content = ("自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
                    "补充细节: 部署在服务器上的应用保持单二进制形态。")
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    bad = FailUpdateClient(cold_items=[{"content": cold_content,
                                        "dense_score": 0.6}])
    stat = _stub_sink(tmp_store, bad, entry, meta_for)
    assert entry in tmp_store.entries("memory"), "update 失败 → 原条目原样"
    assert stat["stubbed"] == 0 and stat["errors"] >= 1, stat
    assert bad.stored == [], "update 失败不得退回 remember"


def test_stub_sink_cold_same_integration(tmp_store, meta_for, tmp_path,
                                         monkeypatch):
    """run_overflow 全链: 冷层已有等价全文时 S5 (闲置≥30d) 先于 S4 拦截,
    本地直接删 (信息零丢失), 不 remember 不 stub — S4 的 same 分支是防御性
    兑底 (S5 未命中时的第二道保险), 两路径均不重复写冷层。"""
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path,
                    ["帮我写一个 Python 脚本处理 Excel", "今天天气怎么样"])
    _judge_all_dormant(monkeypatch)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    ents = tmp_store.entries("memory")
    assert entry not in ents, "冷层已有等价全文 → 本地删除 (S5 零丢失)"
    assert stat["overflowed"] >= 1, stat
    assert cold.stored == [], "冷层已有等价全文 → 不重复 remember"
    assert not [e for e in ents if e.startswith(STUB_PREFIX)], "无需 stub"


# ---- B2: 压缩直写 remember 前查重 ------------------------------------------

def _long_rule_entry():
    filler = "".join(
        f"这是第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。"
        for i in range(1, 8))
    entry = (f"用户偏好({days_ago_str(40)}): 选型原则是极简, 单二进制部署, "
             f"拒绝重依赖。" + filler)
    assert len(entry) > 200
    return entry


def test_typed_compress_cold_same_no_duplicate_remember(tmp_store, meta_for,
                                                        monkeypatch):
    """typed rule 压缩分支: 冷层已有原文 → 不重复 remember, 直接本地 replace。"""
    entry = _long_rule_entry()
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=40)
    compressed = "用户偏好: 极简选型, 单二进制部署, 拒绝重依赖 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    ents = tmp_store.entries("memory")
    assert compressed in ents and entry not in ents, "压缩版替换本地"
    assert stat["compressed"] == 1, stat
    assert cold.stored == [], "冷层已有原文 → 不重复 remember"


def test_typed_compress_cold_similar_merge_then_replace(tmp_store, meta_for,
                                                        monkeypatch):
    """typed 压缩分支 cold-similar: merge-update (含冷层独有句) 后再 replace。"""
    entry = _long_rule_entry()
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=40)
    cold_content = entry[:60] + "。不同补充细节, 用于测试 merge 语义。"
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    cold = MockMnemosyneClient(cold_items=[{"content": cold_content,
                                            "dense_score": 0.6}])
    stat = run_overflow(tmp_store, cold, "memory")
    ents = tmp_store.entries("memory")
    assert compressed in ents and entry not in ents
    assert stat["compressed"] == 1, stat
    assert cold.stored == [], "similar 走 merge-update, 不 remember"
    assert len(cold.updated) == 1 and cold.updated[0][0] == "c0"
    assert "不同补充细节" in cold.updated[0][1], "merged 保留冷层独有句"


def test_typed_compress_recall_fail_keeps_original(tmp_store, meta_for,
                                                   monkeypatch):
    """typed 压缩分支冷层不可达 (recall 异常) → 保留原样 errors。"""
    entry = _long_rule_entry()
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=40)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    bad = MockMnemosyneClient(fail_recall=True)
    stat = run_overflow(tmp_store, bad, "memory")
    ents = tmp_store.entries("memory")
    assert entry in ents and compressed not in ents, "冷层不可达 → 原样保留"
    assert stat["compressed"] == 0 and stat["errors"] >= 1, stat
    assert bad.stored == []


def test_typed_compress_remember_fail_keeps_original(tmp_store, meta_for,
                                                     monkeypatch):
    """typed 压缩分支无匹配且 remember 失败 → 保留原样 (原子性铁律)。"""
    entry = _long_rule_entry()
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=40)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    ents = tmp_store.entries("memory")
    assert entry in ents and compressed not in ents
    assert stat["compressed"] == 0 and stat["errors"] >= 1, stat


def test_legacy_compress_cold_same_no_duplicate_remember(tmp_store,
                                                         monkeypatch):
    """legacy 压缩分支 (reconcile 故障降级): 冷层已有原文 → 不重复 remember。"""
    def _boom(self, entries, now=None):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "reconcile", _boom)
    filler = "".join(f"第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。"
                     for i in range(1, 8))
    entry = "用户偏好: 极简选型, 单二进制部署, 拒绝重依赖。" + filler
    assert len(entry) > 200
    tmp_store.add("memory", entry)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    ents = tmp_store.entries("memory")
    assert compressed in ents and entry not in ents
    assert stat["compressed"] == 1, stat
    assert cold.stored == [], "冷层已有原文 → 不重复 remember"
    assert stat["errors"] >= 1, "reconcile 故障本身计 errors (F1 语义)"


def test_legacy_compress_recall_fail_keeps_original(tmp_store, monkeypatch):
    """legacy 压缩分支冷层不可达 → 保留原样 (errors 含 reconcile 故障 +1)。"""
    def _boom(self, entries, now=None):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "reconcile", _boom)
    filler = "".join(f"第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。"
                     for i in range(1, 8))
    entry = "用户偏好: 极简选型, 单二进制部署, 拒绝重依赖。" + filler
    tmp_store.add("memory", entry)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    bad = MockMnemosyneClient(fail_recall=True)
    stat = run_overflow(tmp_store, bad, "memory")
    ents = tmp_store.entries("memory")
    assert entry in ents and compressed not in ents, "冷层不可达 → 原样保留"
    assert stat["compressed"] == 0 and stat["errors"] >= 2, stat
    assert bad.stored == []


def test_legacy_compress_no_match_remembers(tmp_store, monkeypatch):
    """legacy 压缩分支无匹配 → 现状 remember → replace (既有行为不回归)。"""
    def _boom(self, entries, now=None):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "reconcile", _boom)
    filler = "".join(f"第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。"
                     for i in range(1, 8))
    entry = "用户偏好: 极简选型, 单二进制部署, 拒绝重依赖。" + filler
    tmp_store.add("memory", entry)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)
    cold = MockMnemosyneClient()
    stat = run_overflow(tmp_store, cold, "memory")
    ents = tmp_store.entries("memory")
    assert compressed in ents and entry not in ents
    assert stat["compressed"] == 1, stat
    assert entry in cold.stored, "无匹配 → 现状 remember"
