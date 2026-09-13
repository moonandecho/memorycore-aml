#!/usr/bin/env python3
"""tests/test_fix7_runtime.py — FIX7 整改轮回归 (I1–I8)。

覆盖:
  I1 单次 enforce 调用上限 (含 fallback) + 多轮收敛;
  I2 R2 活动路径统一时间入口 (活动扫描 / 降级 / stub-sink / server /
     retype 回退 / tools.retype);
  I3 清掉 written_at 不得静默续 now 重获宽限;
  I4 显式出生重置可清除 reconcile_anchor_fallback=True;
  I5 stamp 哨兵: 未传=保留, 显式 None=清空 (26 个可选键);
  I6 legacy 口径选择 (b): protected 保留 + malformed review_at 不再
     ambiguous_hold; 两处差异显式断言, 不保留 bug 兼容;
  I7 改型路径清除 ambiguous (server set_entry_type + retype);
  I8 测试质量: 无恒真 count 断言 / all-stub 顺序断言 / server audit 独立期望.

所有期望都在测试内独立构造, 不拿生产 selector 当 oracle。
"""
import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import metadata as meta_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import MAX_EVICT_PER_RUN, RULE_BUDGET_CHARS  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402
from memorycore import server  # noqa: E402

def _load_retype_tool():
    """按文件路径加载 tools/retype_20260912.py。

    不能写 `from tools.retype_20260912 import ...`：在装有 Hermes 仓库的机器上
    （~/.hermes/hermes-agent/tools 是带 __init__.py 的正规包），该名字会解析到
    Hermes 的 tools 包而不是本仓库的 tools 目录（2026-09-13 实测 ModuleNotFoundError）。
    """
    import importlib.util
    p = Path(__file__).resolve().parent.parent / "tools" / "retype_20260912.py"
    spec = importlib.util.spec_from_file_location("_retype_tool_local", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


UTC = timezone.utc


def _add(store, ms, text, *, typ="rule", written_at=None, updated_at=None,
         last_active_at=None, weight=1.0, **extra):
    text = text.strip()
    r = store.add("memory", text)
    assert r.get("success"), r
    now = datetime.now(UTC)
    if written_at is None:
        written_at = now
    if updated_at is None:
        updated_at = written_at
    if last_active_at is None:
        last_active_at = written_at
    ms.stamp(text, typ, written_at=written_at, updated_at=updated_at,
             last_active_at=last_active_at, weight=weight, **extra)
    return text


def _patch_budget(monkeypatch, content_budget, stub_budget=RULE_BUDGET_CHARS):
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", content_budget)
    monkeypatch.setattr(config_mod, "RULE_BUDGET_CHARS", stub_budget)


class _SelectiveCold:
    """按内容选择性冷写失败; 记录成功 remember 内容。"""

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.stored = []
        self._n = 0

    def recall_results(self, query, top_k=5, bump=True):
        return []

    def remember(self, content, importance=0.6, scope="global"):
        if content in self.fail_for:
            return {"status": "error", "error": "selective"}
        self._n += 1
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"c{self._n}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}


# =========================================================================
# I1 单次调用上限 + 多轮收敛 (含 fallback)
# =========================================================================

def test_i1_single_call_cap_and_multi_round_convergence(
        tmp_store, meta_for, monkeypatch):
    """12 条同长全文, 预算需换 9 条: 每次 enforce ≤MAX, 3 轮收敛。"""
    ms = meta_for("memory")
    old = datetime.now(UTC) - timedelta(days=100)
    entries = []
    for i in range(12):
        # 固定前缀 + 固定正文 → 所有条目等长, 便于精确构造预算.
        e = _add(tmp_store, ms, "I1收敛%02d:" % i + "甲" * 200,
                 written_at=old, weight=1.0)
        entries.append(e)
    assert len(set(map(len, entries))) == 1
    one = len(entries[0])
    total = one * len(entries)
    # 需要换出恰好 9 条才落回预算.
    _patch_budget(monkeypatch, total - 9 * one)
    cold = _SelectiveCold()
    calls = 0
    contents = []
    while calls < 10:
        content_chars = sum(
            len(e) for e in tmp_store.entries("memory")
            if (ms.get_entry(e) or {}).get("type") != "stub")
        if content_chars <= total - 9 * one:
            break
        stat = {}
        ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
        calls += 1
        assert stat.get("lru_evicted", 0) <= MAX_EVICT_PER_RUN, stat
        assert stat.get("lru_evicted", 0) == MAX_EVICT_PER_RUN, (
            "本用例首轮/中间轮预算缺口均 >=MAX, 应恰好用满单次上限",
            calls, stat)
        contents.append(stat["lru_evicted"])
    assert calls == 3, (calls, contents)
    assert len(cold.stored) == 9, cold.stored
    # 多轮收敛成立: 该继续时必须继续, 不能在第三批后死循环/不再打满.
    final_chars = sum(
        len(e) for e in tmp_store.entries("memory")
        if (ms.get_entry(e) or {}).get("type") != "stub")
    assert final_chars <= total - 9 * one, final_chars


def test_i1_fallback_uses_only_remaining_call_quota(
        tmp_store, meta_for, monkeypatch):
    """普通候选全冷失败触发 fallback 时, 也只换剩余额度 (合计=3, 不是 6/9)。"""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=100)
    normals, graces = [], []
    for i in range(12):
        e = _add(tmp_store, ms, "I1-N%02d:" % i + "甲" * 180,
                 written_at=old, weight=5.0 + i * 0.01)
        normals.append(e)
    for i in range(12):
        e = _add(tmp_store, ms, "I1-G%02d:" % i + "乙" * 180,
                 written_at=now, weight=0.1 + i * 0.001)
        graces.append(e)
    one = len(normals[0])
    total = sum(len(x) for x in normals + graces)
    _patch_budget(monkeypatch, total - 3 * one)
    cold = _SelectiveCold(fail_for=set(normals))
    stat = {}
    ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
    # stage1 3 条普通全失败, fallback 只能补 3 条宽限; 不能因 3 批循环变 9.
    assert stat.get("lru_evicted", 0) == MAX_EVICT_PER_RUN, stat
    assert len(cold.stored) == MAX_EVICT_PER_RUN, cold.stored
    assert all(x in graces for x in cold.stored), cold.stored
    assert normals == [e for e in tmp_store.entries("memory")
                       if e in set(normals)]


# =========================================================================
# I2 统一时间入口 (活动扫描/降级/stub-sink/server/retype fallback/tools)
# =========================================================================

def test_i2_apply_activity_hits_future_stamp_normalized(
        tmp_store, meta_for, monkeypatch):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    e = _add(tmp_store, ms, "I2 活动扫描 future: " + "戊" * 30,
             written_at=future, updated_at=future, last_active_at=now,
             weight=1.0)
    monkeypatch.setattr(ov, "_load_fresh_queries", lambda metastores: ["查询"])
    monkeypatch.setattr(ov, "_embed_texts",
                        lambda client, queries: [[1.0]])
    monkeypatch.setattr(ov, "_rule_vectors",
                        lambda metastores, rules, client: {"memory": {e: [1.0]}})
    stat = {}
    ov.apply_activity_hits({"memory": ms}, {"memory": [e]}, None, stat)
    m = ms.get_entry(e)
    assert meta_mod._ts_anomaly_snapshot()["count"] == 2, m
    assert m["written_at"] <= (now + timedelta(seconds=300)).isoformat(), m
    assert m["updated_at"] <= (now + timedelta(seconds=300)).isoformat(), m


def test_i2_degraded_future_stamp_normalized(tmp_store, meta_for):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    e = _add(tmp_store, ms, "I2 降级 future: " + "己" * 30,
             written_at=future, updated_at=future, last_active_at=now,
             weight=1.0)
    ov._degraded_lexical_hits({"memory": ms}, {"memory": [e]},
                              ["未来测试"], {})
    m = ms.get_entry(e)
    assert meta_mod._ts_anomaly_snapshot()["count"] == 2, m
    assert m["written_at"] <= (now + timedelta(seconds=300)).isoformat(), m


def test_i2_stub_sink_future_last_strong_normalized(
        tmp_store, meta_for, mock_client):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    e = _add(tmp_store, ms, "I2 stub 换形 future: " + "庚" * 50,
             written_at=now - timedelta(days=30),
             updated_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30), weight=1.0,
             last_strong_hit_at=future)
    stat = {}
    ov._handle_rule_stub_sink(tmp_store, mock_client, "memory", e, ms,
                              stat, [])
    stub = next(x for x in tmp_store.entries("memory")
                if (ms.get_entry(x) or {}).get("type") == "stub")
    assert meta_mod._ts_anomaly_snapshot()["count"] == 1, ms.get_entry(stub)


def test_i2_server_set_entry_type_future_ts_visible(
        tmp_store, meta_for, mock_client):
    meta_mod._reset_ts_anomaly()
    server._store = tmp_store
    server._client = mock_client
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    e = _add(tmp_store, ms, "I2 server future: " + "辛" * 20,
             written_at=future, updated_at=future, last_active_at=future,
             weight=1.0)
    raw = server.memorycore_set_entry_type(
        target="memory", match_text="I2 server future", type_override="rule")
    out = json.loads(raw)
    assert out.get("status") == "ok", out
    # written_at/updated_at/last_active_at 三条 future 均被夹到 now 可见异常.
    assert meta_mod._ts_anomaly_snapshot()["count"] == 3, out


def test_i2_retype_fallback_future_ts_normalized(
        tmp_store, meta_for, monkeypatch):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    e = _add(tmp_store, ms, "I2 retype 回退 future: " + "壬" * 30,
             written_at=future, updated_at=future, last_active_at=future,
             weight=1.0)
    # 模拟冷迁移失败: 本地不动 → _handle_rule_retype 走回退章.
    monkeypatch.setattr(ov, "_handle_cold_migration",
                        lambda *a, **k: None)
    ov._handle_rule_retype(tmp_store, None, "memory", e, ms.get_entry(e),
                           ms, {}, [])
    m = ms.get_entry(e)
    assert meta_mod._ts_anomaly_snapshot()["count"] == 3, m
    assert m["written_at"] <= (now + timedelta(seconds=300)).isoformat(), m
    assert m["updated_at"] <= (now + timedelta(seconds=300)).isoformat(), m


def test_i2_restore_stubs_future_ts_normalized(tmp_store, meta_for):
    """I2 遗漏路径: restore_stubs_from_results 读 stub 旧 last_* 也走锚点."""
    from memorycore.core.overflow import restore_stubs_from_results
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    stub = "[规则指针]I2写回未来->recall:aaaaaaaaaaaaaaaa"
    full = "I2 写回未来的全文: " + "辛" * 50
    tmp_store.add("memory", stub)
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="cold-i2",
             written_at=now - timedelta(days=30),
             updated_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30), weight=1.0,
             last_strong_hit_at=future)
    remaining = restore_stubs_from_results(
        tmp_store, {"memory": ms},
        [{"id": "cold-i2", "content": full}])
    assert remaining == []
    m = ms.get_entry(full)
    assert meta_mod._ts_anomaly_snapshot()["count"] == 1, m
    assert m["last_strong_hit_at"] <= (
        now + timedelta(seconds=300)).isoformat(), m


def test_i2_tools_retype_future_ts_normalized(tmp_store, meta_for):
    _restamp = _load_retype_tool()._restamp
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    e = _add(tmp_store, ms, "I2 工具重标 future: " + "癸" * 30,
             written_at=future, updated_at=future, last_active_at=future,
             weight=1.0)
    _restamp(ms, e, ms.get_entry(e), is_already_state=True)
    m = ms.get_entry(e)
    assert meta_mod._ts_anomaly_snapshot()["count"] == 3, m
    assert m["written_at"] <= (now + timedelta(seconds=300)).isoformat(), m


# =========================================================================
# I3 清掉 written_at 后不得静默续 now
# =========================================================================

def test_i3_cleared_birth_does_not_renew_grace(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    e = _add(tmp_store, ms, "I3 出生清除: " + "甲" * 20,
             written_at=old, updated_at=old, last_active_at=old, weight=0.1)
    assert not ov._in_soft_residency(ms.get_entry(e), now, entry=e)
    assert ms.update_fields(e, written_at=None) is not None
    assert "written_at" not in ms.get_entry(e)
    ms.stamp(e, "rule")  # 调用方未显式传出生时刻
    m = ms.get_entry(e)
    assert "written_at" not in m, m
    assert not ov._in_soft_residency(m, now, entry=e), m


# =========================================================================
# I4 显式出生重置清除 reconcile fallback
# =========================================================================

def test_i4_explicit_birth_reset_clears_reconcile_fallback(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = "I4 无内嵌日期 legacy: 内容"
    tmp_store.add("memory", e)
    assert ms.reconcile(tmp_store.entries("memory"))["stamped"] == 1
    assert ms.get_entry(e).get("reconcile_anchor_fallback") is True
    ms.stamp(e, "rule", written_at=now, updated_at=now, last_active_at=now)
    m = ms.get_entry(e)
    assert m.get("reconcile_anchor_fallback") is not True, m
    assert ov._soft_residency_grace_ts(m, now, entry=e) is not None, m


def test_i4_activity_scan_preserves_reconcile_fallback(
        tmp_store, meta_for):
    """I4 安全边界: 活动扫描继承旧 written_at, 不是出生重置, 不得清掉
    reconcile 兜底标记 (否则 legacy 无内嵌日期条目会重获 7d 宽限)."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = "I4 活动扫描不洗 fallback: 内容"
    tmp_store.add("memory", e)
    ms.reconcile(tmp_store.entries("memory"))
    assert ms.get_entry(e).get("reconcile_anchor_fallback") is True
    ov._degraded_lexical_hits({"memory": ms}, {"memory": [e]},
                              ["不命中查询"], {})
    m = ms.get_entry(e)
    assert m.get("reconcile_anchor_fallback") is True, m
    assert ov._soft_residency_grace_ts(m, now, entry=e) is None, m


# =========================================================================
# I5 stamp 显式 None 清空
# =========================================================================

def test_i5_stamp_explicit_none_clears_optional_keys(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "I5 清空可选键: " + "乙" * 20,
             written_at=now - timedelta(days=1), updated_at=now,
             last_active_at=now - timedelta(days=1), weight=0.5,
             last_scan_at=now - timedelta(days=2),
             last_strong_hit_at=now - timedelta(days=3),
             last_weak_hit_at=now - timedelta(days=4),
             last_recall_hit_at=now - timedelta(days=5),
             last_injected_at=now - timedelta(days=6),
             last_evicted_at=now - timedelta(days=7),
             cold_id="cold-1", handle="#handle1", type_override="state",
             type_source="manual_override", protect_override=True,
             protected=True, schema=2,
             judge_decision="ambiguous", judge_band="strong",
             judge_confidence=0.9, judge_signals={"x": 1},
             judge_reason="old", judge_review_at=now + timedelta(days=5),
             judge_review_count=2, judge_reviewed_at=now,
             judge_resolution="manual_override", judge_resolved_at=now,
             judge_policy="v3", writeback_count=3, retire_count=2,
             grace_defer_count=2, reconcile_anchor_fallback=True)
    clear_keys = [
        "cold_id", "handle", "type_override", "type_source",
        "protect_override", "judge_decision", "judge_band",
        "judge_confidence", "judge_signals", "judge_reason",
        "judge_review_at", "judge_reviewed_at", "judge_resolution",
        "judge_resolved_at", "judge_policy", "last_scan_at",
        "last_strong_hit_at", "last_weak_hit_at", "last_recall_hit_at",
        "last_injected_at", "last_evicted_at", "writeback_count",
        "retire_count", "grace_defer_count", "reconcile_anchor_fallback",
        "schema",
    ]
    ms.stamp(e, "rule", **{k: None for k in clear_keys})
    after = ms.get_entry(e)
    stuck = [k for k in clear_keys if k in after]
    assert stuck == [], (stuck, after)


def test_i5_unset_still_retains_optional_keys(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    e = _add(tmp_store, ms, "I5 未传保留: " + "丙" * 20,
             written_at=old, updated_at=old, last_active_at=old,
             weight=0.5, cold_id="c-old", handle="#h-old",
             type_override="state", judge_decision="ambiguous",
             judge_review_at=now + timedelta(days=3),
             grace_defer_count=2)
    ms.stamp(e, "rule")  # 未传可选键 = 保留
    after = ms.get_entry(e)
    assert after["cold_id"] == "c-old"
    assert after["handle"] == "#h-old"
    assert after["type_override"] == "state"
    assert after["judge_decision"] == "ambiguous"
    assert after["grace_defer_count"] == 2
    assert "judge_review_at" in after


# =========================================================================
# I6 legacy 口径 (b): 两处差异显式记录 + 候选排序主体不变
# =========================================================================

def test_i6_legacy_activity_stamp_keeps_protected_and_excludes_candidate(
        tmp_store, meta_for, monkeypatch):
    """差异①: FIX5 整字典覆盖会丢 protected; 本轮回滚路径按 (b) 不兼容 bug。

    FIX6 合并式 stamp 保留 protected=True, legacy 资格豁免因此继续排除
    该中性条目。本测试显式锁住新行为, 并在 DESIGN-DEVIATIONS §11 记录。
    """
    monkeypatch.setattr(config_mod, "CACHE_POLICY_V2", False)
    monkeypatch.setattr(config_mod, "PROTECT_SKIP_LRU", True)
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "I6 legacy 中性文本: " + "甲" * 30,
             written_at=now - timedelta(days=100),
             updated_at=now - timedelta(days=100),
             last_active_at=now - timedelta(days=100), weight=1.0,
             protected=True)
    ov._degraded_lexical_hits({"memory": ms}, {"memory": [e]},
                              ["不命中查询"], {})
    # 新行为: 合并式 stamp 未丢 protected.
    assert ms.get_entry(e).get("protected") is True, ms.get_entry(e)
    selected = ov._select_retirement_candidates(
        tmp_store, ms, "memory", 10 ** 6, {})
    assert selected == [], selected


def test_i6_legacy_malformed_review_at_is_not_ambiguous_hold(
        tmp_store, meta_for, monkeypatch):
    """差异②: malformed judge_review_at 在 legacy 不再算 ambiguous_hold。

    FIX5 旧判据只 bool(review_at); FIX6+ 要求经 _ts_anchor 可解析期限。
    本测试显式锁住 None, 不假装回滚路径一字未变。
    """
    monkeypatch.setattr(config_mod, "CACHE_POLICY_V2", False)
    monkeypatch.setattr(ov, "TARGET_RATIO", 0.0)
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "I6 malformed review: " + "乙" * 30,
             written_at=now, updated_at=now, last_active_at=now, weight=1.0)
    ms.update_fields(e, judge_decision="ambiguous",
                     judge_review_at="not-a-date", judge_review_count=0)
    assert ov._ambiguous_hold_valid(ms.get_entry(e), now=now, entry=e) is False
    reason = ov._compute_plateau_reason(tmp_store, ms, "memory", {})
    assert reason is None, reason
    assert meta_mod._ts_anomaly_snapshot()["count"] >= 1


def test_i6_stub_sidecar_does_not_grow_default_grace_key(
        tmp_store, meta_for, mock_client):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "I6 stub sidecar schema: " + "丙" * 50,
             written_at=now - timedelta(days=30),
             updated_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30), weight=1.0)
    assert "grace_defer_count" not in ms.get_entry(e)
    ov._handle_rule_stub_sink(tmp_store, mock_client, "memory", e, ms, {}, [])
    stub = next(x for x in tmp_store.entries("memory")
                if (ms.get_entry(x) or {}).get("type") == "stub")
    assert "grace_defer_count" not in ms.get_entry(stub), ms.get_entry(stub)


# =========================================================================
# I7 改型清除 ambiguous
# =========================================================================

def test_i7_server_set_entry_type_clears_ambiguous_quickpath(
        tmp_store, meta_for, mock_client):
    server._store = tmp_store
    server._client = mock_client
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "I7 server 改型: " + "丁" * 30,
             written_at=now - timedelta(days=30),
             updated_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30), weight=1.0,
             judge_decision="ambiguous", judge_resolution=None,
             judge_review_count=2)
    assert e in ov._plan_stub_candidates(ms, None, [e])
    raw = server.memorycore_set_entry_type(
        target="memory", match_text="I7 server 改型", type_override="rule")
    out = json.loads(raw)
    assert out.get("status") == "ok", out
    m = ms.get_entry(e)
    assert m.get("judge_decision") != "ambiguous", m
    assert m.get("judge_resolution") == "manual_override", m
    assert e not in ov._plan_stub_candidates(ms, None, [e]), m


def test_i7_retype_state_stamp_clears_ambiguous(tmp_store, meta_for,
                                                monkeypatch):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "I7 retype 改型: " + "戊" * 30,
             written_at=now - timedelta(days=30),
             updated_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30), weight=1.0,
             judge_decision="ambiguous", judge_resolution=None,
             judge_review_count=2)
    # 模拟冷迁移成功: 本地删除, 留下 state sidecar 章可查.
    monkeypatch.setattr(
        ov, "_handle_cold_migration",
        lambda store, client, target, entry, stat: store.remove_by_exact(
            target, entry))
    stat = {"errors": 0, "aged_sunk": 0, "retyped": 0, "kept": 0}
    ov._handle_rule_retype(tmp_store, None, "memory", e, ms.get_entry(e),
                           ms, stat, [])
    m = ms.get_entry(e)
    assert m is not None and m.get("type") == "state", m
    assert m.get("judge_decision") is None, m
    assert m.get("judge_resolution") is None, m
    assert "judge_review_at" not in m, m
    assert e not in ov._plan_stub_candidates(ms, None, [e])


# =========================================================================
# I8 测试质量 (结构检查; 行为断言另见 test_fix6_runtime.py)
# =========================================================================

def test_i8_no_tautological_audit_counts_in_fix6_suite():
    src = (ROOT / "tests" / "test_fix6_runtime.py").read_text()
    assert "timestamp_anomaly\"][\"count\"] >= 0" not in src
    assert "ts_anomaly\"][\"count\"] >= 0" not in src
    # all-stub 已从 set 比较改为顺序断言.
    assert "assert got == [s_old, s_new]" in src
    # server audit 已改为独立期望, 不再用 selector 自比较.
    assert "assert audit_next == [grace[:60]]" in src


def test_i2_legacy_direct_parse_documented_exception():
    """FIX7 I2: legacy 回滚路径唯一直接 _parse_iso 必须是带注释的明确例外。"""
    text = (ROOT / "memorycore" / "core" / "overflow.py").read_text(encoding="utf-8")
    assert "FIX7 I2 明确例外" in text
    assert text.count('_parse_iso(str(m.get("judge_resolved_at")))') == 1
    # server / tools / weekly 的活动路径不得再直接 parse.
    for rel in ("memorycore/server.py", "tools/retype_20260912.py",
                "memorycore/weekly_maintenance.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "_parse_iso(" not in src, rel
