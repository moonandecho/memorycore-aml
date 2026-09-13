#!/usr/bin/env python3
"""tests/test_fix6_runtime.py — FIX6 根治轮回归 (R1-R4 + 评审次要项)。

覆盖:
  R1 出生时刻粘性 / 压缩继承 / legacy reconcile 兜底标记;
  R2 _ts_anchor 单一入口 / 未来时间戳不制造宽限 / ts_anomaly 可见;
  R3 新鲜窗口只是排序乘数 (FIX8 换挡后的行为) / soft=0 对照;
  R4 统一候选池无分档 / 低 rank 候选优先;
  次要#5 set_entry_type 字段保留 / #6 defer 去重 / #7 server audit 与选择器一致 /
  #8 stub GC 显式不识别宽限;
  评审自造用例的 enforce/empty/need0/all-stub/grace×protected/时间戳组合分类并入。

所有期望顺序在测试侧独立构造 (权重/时间显式推导), 不调用 _rule_rank 当 oracle。
"""
import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import metadata as meta_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import (MAX_EVICT_PER_RUN, RULE_BUDGET_CHARS,  # noqa: E402
                         WEIGHT_INIT)
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402
from memorycore import server  # noqa: E402


UTC = timezone.utc


# ---- 工具 -----------------------------------------------------------------

def _add(store, ms, text, *, typ="rule", written_at=None, updated_at=None,
         last_active_at=None, weight=1.0, protected=None,
         last_recall_hit_at=None, last_evicted_at=None, retire_count=None):
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
    kw = {"written_at": written_at, "updated_at": updated_at,
          "last_active_at": last_active_at, "weight": weight}
    if protected is not None:
        kw["protected"] = protected
    if last_recall_hit_at is not None:
        kw["last_recall_hit_at"] = last_recall_hit_at
    if last_evicted_at is not None:
        kw["last_evicted_at"] = last_evicted_at
    if retire_count is not None:
        kw["retire_count"] = retire_count
    ms.stamp(text, typ, **kw)
    return text


def _patch_budget(monkeypatch, content_budget, stub_budget=RULE_BUDGET_CHARS):
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", content_budget)
    monkeypatch.setattr(config_mod, "RULE_BUDGET_CHARS", stub_budget)


def _drain_selector(store, ms, need=10 ** 9):
    order = []
    while True:
        batch = ov._select_retirement_candidates(store, ms, "memory", need, {})
        if not batch:
            break
        assert len(batch) <= MAX_EVICT_PER_RUN
        order.extend(batch)
        for e in batch:
            assert store.remove_by_exact("memory", e)["success"]
    return order


class _SelectiveCold:
    """选择性冷失败 mock: fail_for 中的内容 remember 返回 error。"""

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.stored = []
        self._n = 0

    def recall_results(self, query, top_k=5, bump=True):
        return []

    def remember(self, content, importance=0.6, scope="global"):
        if content in self.fail_for:
            return {"status": "error", "error": "selective mock failure"}
        self._n += 1
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"c{self._n}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}


# ---- R1: 出生时刻粘性 / 压缩继承 / reconcile 兜底 --------------------------

def test_r1_stamp_sticky_birth_and_explicit_reset(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    e = _add(tmp_store, ms, "R1 出生粘性条目: " + "甲" * 20,
             written_at=old, updated_at=old, last_active_at=old,
             weight=0.5, last_recall_hit_at=old)
    before = ms.get_entry(e)

    # 不传 written_at 的反复 stamp 必须继承原出生时刻/活动/权重/写回.
    ms.stamp(e, "rule", origin="repeated_stamp_without_birth")
    after = ms.get_entry(e)
    assert after["written_at"] == before["written_at"]
    assert after["weight"] == before["weight"]
    assert after["last_active_at"] == before["last_active_at"]
    assert after["last_recall_hit_at"] == before["last_recall_hit_at"]

    # 显式传 written_at 才是重置出生时刻的合法行为.
    new_birth = now - timedelta(days=1)
    ms.stamp(e, "rule", written_at=new_birth)
    assert ms.get_entry(e)["written_at"] == new_birth.isoformat()


def test_r1_compression_inherits_original_activity(tmp_store, meta_for,
                                                   mock_client, monkeypatch):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=90)
    long_rule = "R1 长期技术记录: " + "内容" * 140
    e = _add(tmp_store, ms, long_rule, written_at=old, updated_at=old,
             last_active_at=old, weight=0.5, last_recall_hit_at=old)
    compressed = "R1 精简技术记录: " + "内容" * 20
    monkeypatch.setattr(ov, "_llm_compress", lambda client, entry: compressed)

    stat = {"compressed": 0, "errors": 0, "kept": 0}
    handled = ov._handle_typed_entry(
        tmp_store, mock_client, "memory", long_rule, ms.get_entry(long_rule),
        ms, stat, [])
    assert handled is True
    assert long_rule not in tmp_store.entries("memory")
    assert compressed in tmp_store.entries("memory")
    cm = ms.get_entry(compressed)
    assert cm["written_at"] == old.isoformat(), cm
    assert cm["updated_at"] == old.isoformat(), cm
    assert cm["last_active_at"] == old.isoformat(), cm
    assert cm["last_recall_hit_at"] == old.isoformat(), cm
    assert cm["weight"] == 0.5, cm
    assert cm["origin"] == "overflow"


def test_r1_legacy_reconcile_fallback_no_grace(tmp_store, meta_for):
    ms = meta_for("memory")
    e = "R1 无内嵌日期的旧条目: 内容"
    tmp_store.add("memory", e)
    assert ms.reconcile(tmp_store.entries("memory"))["stamped"] == 1
    m = ms.get_entry(e)
    assert m.get("reconcile_anchor_fallback") is True, m
    assert m.get("written_at"), "补章仍需保留可见时间戳"
    assert ov._soft_residency_grace_ts(
        m, datetime.now(UTC), entry=e) is None, \
        "legacy reconcile 兜底不得获得 7 天软驻留宽限"


# ---- R2: 单一时间锚点 / 未来时间戳 / 异常可见 ------------------------------

@pytest.mark.parametrize("delta", [
    timedelta(hours=1), timedelta(days=30), timedelta(days=400)])
def test_r2_future_anchor_no_grace_and_anomaly_visible(tmp_store, meta_for,
                                                        caplog, delta):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = f"R2 未来条目 {delta}: " + "乙" * 20
    tmp_store.add("memory", e)
    ms.stamp(e, "rule", written_at=now + delta)
    m = ms.get_entry(e)

    with caplog.at_level(logging.WARNING, logger="memorycore.metadata"):
        grace = ov._soft_residency_grace_ts(m, datetime.now(UTC), entry=e)
        clamped = meta_mod._ts_anchor(
            m["written_at"], datetime.now(UTC), field="written_at",
            sha=hashlib.sha256(e.encode()).hexdigest())

    assert grace is None, f"未来 {delta} 不得制造宽限"
    assert clamped is not None
    assert abs((clamped - datetime.now(UTC)).total_seconds()) < 5, clamped
    snap = meta_mod._ts_anomaly_snapshot()
    assert snap["count"] >= 1, snap
    sha8 = hashlib.sha256(e.encode()).hexdigest()[:8]
    assert any(r.get("sha8") == sha8 for r in snap["recent"]), snap
    assert any("TS_ANOMALY" in (r.getMessage() or "") for r in caplog.records)


def test_r2_future_last_recall_does_not_extend_old_birth(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = "R2 旧写入未来召回: " + "丙" * 20
    e = _add(tmp_store, ms, e, written_at=now - timedelta(days=30),
             last_recall_hit_at=now + timedelta(days=400), weight=1.0)
    assert ov._soft_residency_grace_ts(
        ms.get_entry(e), now, entry=e) is None, \
        "未来 last_recall 不得压过旧 written_at 制造宽限"


def test_r2_age_weight_tier_share_single_anchor(tmp_store, meta_for):
    meta_mod._reset_ts_anomaly()
    now = datetime.now(UTC)
    future = now + timedelta(days=400)
    m_state = {"type": "state", "written_at": future.isoformat()}
    assert meta_mod.entry_age_days(m_state, now=now) == 0
    m_rule = {"type": "rule", "weight": 1.0,
              "last_active_at": future.isoformat()}
    assert abs(ov._rule_weight_eff(m_rule, now=now) - 1.0) < 1e-9
    e = "R2 活跃分级未来戳: 内容"
    m_amb = {"judge_decision": "ambiguous", "judge_resolution": None,
             "judge_review_count": 0,
             "written_at": future.isoformat(), "type": "rule"}
    tier, min_age = ov._rule_activity_tier(m_amb, e, [], now)
    assert tier == "ambiguous" and min_age > 0
    # 未来活动戳统一计入异常, 且不因未来而获得额外排名收益.
    assert meta_mod._ts_anomaly_snapshot()["count"] >= 1


def test_r2_ambiguous_hold_future_review_at_is_not_an_anomaly(
        tmp_store, meta_for):
    meta_mod._reset_ts_anomaly()
    now = datetime.now(UTC)
    e = "R2 ambiguous hold: 内容"
    m = {"judge_decision": "ambiguous", "judge_resolution": None,
         "judge_review_at": (now + timedelta(days=7)).isoformat()}
    assert ov._ambiguous_hold_valid(m, now=now, entry=e) is True
    assert meta_mod._ts_anomaly_snapshot()["count"] == 0, \
        "计划性 review_at 是合法未来期限, 不得计入 ts_anomaly"


def test_r2_unparsable_anchor_is_none_and_visible():
    meta_mod._reset_ts_anomaly()
    now = datetime.now(UTC)
    assert meta_mod._ts_anchor("not-a-date", now, field="written_at",
                               sha="deadbeef") is None
    snap = meta_mod._ts_anomaly_snapshot()
    assert snap["count"] == 1 and snap["recent"][0]["reason"] == "unparsable"


# ---- R3/R4 (FIX8 换挡后): 新鲜窗口是排序乘数, 不是资格 / 无分档 --------

def test_r3_fresh_multiplier_is_ordering_not_qualification(
        tmp_store, meta_for):
    """FIX8 B1: 新鲜只乘 GRACE_MULT; 低权新鲜仍按统一 rank 先出."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    fresh_low = _add(tmp_store, ms, "R3 新鲜低权: " + "甲" * 40,
                     written_at=now, weight=0.01)
    normal = _add(tmp_store, ms, "R3 普通旧: " + "乙" * 40,
                  written_at=old, weight=1.0)
    fresh_high = _add(tmp_store, ms, "R3 新鲜高权: " + "丙" * 40,
                      written_at=now, weight=5.0)
    # 统一 rank 升序: 新鲜低权 (0.09) < 普通旧 (1.0) < 新鲜高权 (45.0)
    order = _drain_selector(tmp_store, ms)
    assert order == [fresh_low, normal, fresh_high], order
    assert all(ov._in_soft_residency(ms.get_entry(e), now, entry=e)
               for e in (fresh_low, fresh_high))


def test_r3_soft_zero_disables_multiplier_and_restores_pure_rank(
        tmp_store, meta_for, monkeypatch):
    """窗口 <=0 回滚: 与纯 _rule_rank 排序一致, 新鲜不再靠后."""
    monkeypatch.setattr(ov, "RULE_MIN_RESIDENCY_DAYS", 0)
    monkeypatch.setattr(config_mod, "RULE_MIN_RESIDENCY_DAYS", 0)
    ms = meta_for("memory")
    now = datetime.now(UTC)
    fresh = _add(tmp_store, ms, "R3 关断新: " + "丁" * 40,
                 written_at=now, weight=0.1)
    old = _add(tmp_store, ms, "R3 关断旧: " + "戊" * 40,
               written_at=now - timedelta(days=30), weight=5.0)
    assert _drain_selector(tmp_store, ms) == [fresh, old]
    assert not ov._in_soft_residency(ms.get_entry(fresh), now, entry=fresh)


def test_r4_unified_pool_has_no_normal_grace_partition(
        tmp_store, meta_for):
    """FIX8 B2: 删除两档/fallback 后, 新鲜低 rank 不再被非宽限组短路."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old_high = _add(tmp_store, ms, "R4 旧高权: " + "己" * 50,
                    written_at=now - timedelta(days=30), weight=5.0)
    fresh_low = _add(tmp_store, ms, "R4 新低权: " + "庚" * 50,
                     written_at=now, weight=0.1)
    # need 只需一条; 统一 rank 下 fresh_low(0.9) 先于旧高权(5.0).
    assert ov._select_retirement_candidates(
        tmp_store, ms, "memory", 1, {}) == [fresh_low]
    assert old_high not in ov._select_retirement_candidates(
        tmp_store, ms, "memory", 1, {})


# ---- 评审自造用例并入: empty/need0/all-stub/grace×protected/组合 -----------

def test_fix6_empty_need_zero_and_all_stub_two_phase(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    assert ov._select_retirement_candidates(
        tmp_store, ms, "memory", 123, {}) == []
    assert ov._select_retirement_candidates(
        tmp_store, ms, "memory", 0, {}) == []

    s_old = _add(tmp_store, ms, "[规则指针]旧指针甲→recall:aaaaaaaaaaaaaaaa",
                 typ="stub", written_at=now - timedelta(days=30), weight=0.5)
    s_new = _add(tmp_store, ms, "[规则指针]新指针乙→recall:bbbbbbbbbbbbbbbb",
                 typ="stub", written_at=now - timedelta(days=1), weight=0.1)
    assert ov._select_retirement_candidates(
        tmp_store, ms, "memory", 10 ** 9, {}, include_stubs=False) == []
    got = ov._select_retirement_candidates(
        tmp_store, ms, "memory", 10 ** 9, {}, include_stubs=True)
    # FIX8 I8: 独立顺序期望, 不用 set 弱断言.
    #   s_old rank=0.5 (30d 前); s_new rank=0.1×9=0.9 (fresh 低权).
    # 统一 rank 升序: 旧高权指针先出; stub GC 与候选池同一个 _rule_rank.
    assert got == [s_old, s_new], got


def test_fix6_grace_with_protected_multiplier_order(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    grace_anchor = now - timedelta(days=2)
    nu = _add(tmp_store, ms, "普通未保护: " + "未" * 50,
              written_at=old, weight=1.0)
    np = _add(tmp_store, ms, "普通受保护: " + "护" * 50,
              written_at=old, weight=1.0, protected=True)
    gu = _add(tmp_store, ms, "宽限未保护: " + "宽" * 50,
              written_at=grace_anchor, weight=1.0)
    gp = _add(tmp_store, ms, "宽限受保护: " + "保" * 50,
              written_at=grace_anchor, weight=1.0, protected=True)

    # 统一 rank 升序: 未保护 ×1 先于 protected ×3; 新鲜再乘 GRACE_MULT,
    # 故 [nu, np, gu, gp] (gu=1×GRACE_MULT < gp=3×GRACE_MULT).
    assert _drain_selector(tmp_store, ms) == [nu, np, gu, gp]


def test_fix6_written_at_last_recall_combination_order(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    both_old = _add(tmp_store, ms, "双旧: " + "双" * 50,
                    written_at=now - timedelta(days=30),
                    last_recall_hit_at=now - timedelta(days=40), weight=1.0)
    recall_5d = _add(tmp_store, ms, "旧写近召回: " + "召" * 50,
                     written_at=now - timedelta(days=30),
                     last_recall_hit_at=now - timedelta(days=5), weight=1.0)
    written_2d = _add(tmp_store, ms, "近写旧召回: " + "写" * 50,
                      written_at=now - timedelta(days=2),
                      last_recall_hit_at=now - timedelta(days=30),
                      weight=1.0)
    recall_1h = _add(tmp_store, ms, "旧写新召回: " + "新" * 50,
                     written_at=now - timedelta(days=6),
                     last_recall_hit_at=now - timedelta(hours=1), weight=1.0)
    # FIX8: 三条新鲜同 rank=9, tie-break 复用 last_active_at 早→晚;
    # both_old 无新鲜乘数, 仍以 rank=1 最前.
    assert _drain_selector(tmp_store, ms) == [
        both_old, recall_5d, recall_1h, written_2d]


def test_fix6_future_entries_are_not_deferred_forever(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    normal = _add(tmp_store, ms, "普通旧: " + "甲" * 50,
                  written_at=old, weight=1.0)
    fut = _add(tmp_store, ms, "未来戳: " + "乙" * 50,
               written_at=now + timedelta(days=400), weight=0.01)
    stat = {}
    sel = ov._select_retirement_candidates(
        tmp_store, ms, "memory", len(normal) + len(fut) + 1, stat)
    assert fut in sel and normal in sel
    assert "residency_deferred" not in stat, stat


# ---- 评审次要项: #5 #6 #7 #8 ----------------------------------------------

def test_fix6_set_entry_type_preserves_audit_fields(
        tmp_store, mock_client, meta_for):
    server._store = tmp_store
    server._client = mock_client
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=20)
    e = "字段保留条目: " + "护" * 30
    _add(tmp_store, ms, e, written_at=old, weight=0.5)
    ms.stamp(e, "rule", last_recall_hit_at=old, last_injected_at=old,
             last_evicted_at=old, writeback_count=2, retire_count=1,
             handle="#h1234567")
    before = ms.get_entry(e)

    raw = server.memorycore_set_entry_type(
        target="memory", match_text="字段保留条目", type_override="rule")
    out = json.loads(raw)
    assert out["status"] == "ok", out
    after = ms.get_entry(e)
    for k in ("last_recall_hit_at", "last_injected_at", "last_evicted_at",
              "writeback_count", "retire_count", "handle"):
        assert after.get(k) == before.get(k), (k, before, after)


def test_fix6_server_audit_next_candidates_matches_selector(
        tmp_store, mock_client, meta_for):
    server._store = tmp_store
    server._client = mock_client
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    normal = _add(tmp_store, ms, "审计普通: " + "甲" * 50,
                  written_at=old, weight=1.0)
    grace = _add(tmp_store, ms, "审计宽限低权: " + "乙" * 50,
                 written_at=now, weight=0.01)
    # FIX8 I8: 不用 selector 自比较; 独立算期望: 统一 rank 下新鲜低权
    # (0.01×9=0.09) 先于旧 normal(1.0), 且 need=1 即满足.
    data = json.loads(server.memorycore_get_rule_weight("memory"))
    audit_next = data["memory"]["summary"]["next_eviction_candidates"]
    assert audit_next == [grace[:60]], audit_next
    meta_mod._reset_ts_anomaly()
    data = json.loads(server.memorycore_get_rule_weight("memory"))
    assert data["memory"]["summary"]["ts_anomaly"]["count"] == 0, data


def test_fix6_stub_gc_uses_unified_rank_no_separate_tier(tmp_store, meta_for, monkeypatch):
    """FIX8: 指针 GC 与统一 rank 同池; 低 rank 新鲜 stub 照样先 GC."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    grace_stub = _add(tmp_store, ms, "[规则指针]宽限低rank指针→recall:aaaaaaaaaaaaaaaa",
                      typ="stub", written_at=now, weight=0.01,
                      last_active_at=now, last_recall_hit_at=now,
                      last_evicted_at=now - timedelta(days=2), retire_count=2)
    normal_stub = _add(tmp_store, ms, "[规则指针]普通高rank指针→recall:bbbbbbbbbbbbbbbb",
                       typ="stub", written_at=old, weight=5.0,
                       last_active_at=old, last_evicted_at=now - timedelta(days=2),
                       retire_count=2)
    monkeypatch.setattr(ov, "MAX_STUB_PER_RUN", 1)
    assert ov._in_soft_residency(ms.get_entry(grace_stub), now,
                                 entry=grace_stub) is True
    stat = {"stub_gc": 0}
    ov._stub_gc(tmp_store, ms, "memory", stat, force=True)
    assert stat["stub_gc"] == 1
    assert grace_stub not in tmp_store.entries("memory")
    assert normal_stub in tmp_store.entries("memory")


def test_fix6_audits_expose_ts_anomaly(tmp_store, mock_client, meta_for):
    server._store = tmp_store
    server._client = mock_client
    meta_mod._reset_ts_anomaly()
    # FIX7 I8: 构造恰好一条已知异常, 断言精确条数与 reason.
    fixed = datetime.now(UTC)
    meta_mod._ts_anchor("not-a-date", fixed, field="written_at",
                        sha="deadbeef")
    assert meta_mod._ts_anomaly_snapshot()["count"] == 1
    raw = server.memorycore_get_memory_usage()
    usage = json.loads(raw)
    assert "timestamp_anomaly" in usage
    assert usage["timestamp_anomaly"]["count"] == 1, usage
    assert usage["timestamp_anomaly"]["recent"][0]["reason"] == "unparsable"
