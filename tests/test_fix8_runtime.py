#!/usr/bin/env python3
"""tests/test_fix8_runtime.py — FIX8 换挡轮回归。

覆盖:
  B1 新鲜窗口乘数标定语义 / B2 统一候选池;
  B3 四类坏输入只抬升位次 (有界, 可换出), 全带乘数仍可清空 (含 protected);
  P1 候选饥饿: need 极小 + 头部永久冷失败, 有限轮/单 call 内继续换下一顺位;
  P2 stamp(type=None/invalid) 安全默认;
  P3 reconcile / retype 内嵌未来日期过 _ts_anchor(field="embedded_date");
  P4 server.set_entry_type 改型清 ambiguous 审计键;
  P5 --budget 0 换出口径 + stub GC 独立口径固定.
"""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import metadata as meta_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import MAX_EVICT_PER_RUN, RULE_BUDGET_CHARS  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    _rule_rank, _select_retirement_candidates, _soft_residency_grace_ts,
)
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
    assert store.add("memory", text).get("success")
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


def _drain(store, ms, *, need=10 ** 9, cap=MAX_EVICT_PER_RUN):
    removed = []
    while True:
        batch = _select_retirement_candidates(
            store, ms, "memory", need, {}, max_evict=cap)
        if not batch:
            break
        for e in batch:
            assert store.remove_by_exact("memory", e).get("success")
        removed.extend(batch)
        if len(removed) > 1000:
            raise AssertionError("drain did not terminate")
    return removed


class _SelectiveCold:
    """选择性冷失败 mock; 记录实际尝试顺序与成功内容。"""

    def __init__(self, fail_for=()):
        self.fail_for = set(fail_for)
        self.stored = []
        self.attempts = []
        self._n = 0

    def recall_results(self, query, top_k=5, bump=True):
        return []

    def remember(self, content, importance=0.6, scope="global"):
        self.attempts.append(content)
        if content in self.fail_for:
            return {"status": "error", "error": "selective mock failure"}
        self._n += 1
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"c{self._n}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}


# =========================================================================
# B1 / B2
# =========================================================================

def test_b1_fresh_is_fourth_multiplier_not_tier(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    fresh = _add(tmp_store, ms, "B1 新鲜: " + "甲" * 40,
                 written_at=now, weight=1.0, protected=False)
    m = ms.get_entry(fresh)
    assert _soft_residency_grace_ts(m, now, entry=fresh) is not None
    assert abs(_rule_rank(fresh, m, now) - 1.0 * config_mod.GRACE_MULT) < 1e-9
    old = _add(tmp_store, ms, "B1 旧: " + "乙" * 40,
               written_at=now - timedelta(days=30), weight=5.0)
    # 乘数作用下旧高权先出; 压力足够时 fresh 仍被选走 (统一池非分档).
    order = _drain(tmp_store, ms)
    assert order == [old, fresh], order


def test_b2_no_partition_fresh_low_weight_still_first(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    normal = _add(tmp_store, ms, "B2 普通: " + "甲" * 40,
                  written_at=now - timedelta(days=30), weight=1.0)
    fresh_low = _add(tmp_store, ms, "B2 新鲜低权: " + "乙" * 40,
                     written_at=now, weight=0.01)
    assert _select_retirement_candidates(
        tmp_store, ms, "memory", 1, {}) == [fresh_low]
    assert _select_retirement_candidates(
        tmp_store, ms, "memory", 1, {}, exclude={fresh_low}) == [normal]


def test_b2_grace_defer_count_is_inert_legacy_key(tmp_store, meta_for):
    """FIX8 B2: 字段仅保留 I5 三态 (legacy optional), 不参与排序/让位计数."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    a = _add(tmp_store, ms, "B2-inert-a: " + "甲" * 40,
             written_at=old, weight=1.0, grace_defer_count=0)
    b = _add(tmp_store, ms, "B2-inert-b: " + "乙" * 40,
             written_at=old, weight=1.0, grace_defer_count=999)
    ma, mb = ms.get_entry(a), ms.get_entry(b)
    assert ma["grace_defer_count"] == 0 and mb["grace_defer_count"] == 999
    assert abs(_rule_rank(a, ma, now) - _rule_rank(b, mb, now)) < 1e-12
    # 计数不同不影响候选集合/顺序; need 要两条就两条都出.
    need = len(a) + len(b)
    sel = _select_retirement_candidates(tmp_store, ms, "memory", need, {})
    assert set(sel) == {a, b}, sel


# =========================================================================
# B3 坏输入矩阵: 只抬升位次 (有界), 无永久宽限; 全带乘数仍可清空
# =========================================================================

def test_b3_future_written_at_no_infinite_grace_and_selectable(
        tmp_store, meta_for):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "B3 未来出生: " + "甲" * 50,
             written_at=now + timedelta(days=400), weight=1.0,
             last_active_at=now - timedelta(days=30))
    m = ms.get_entry(e)
    # 未来出生不授予新鲜乘数; w_eff 仍按 last_active 诚实衰减。
    assert _soft_residency_grace_ts(m, now, entry=e) is None
    assert _rule_rank(e, m, now) <= 1.0
    assert _drain(tmp_store, ms) == [e]
    assert meta_mod._ts_anomaly_snapshot()["count"] >= 1


def test_b3_missing_birth_no_infinite_grace_and_selectable(
        tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "B3 出生缺失: " + "乙" * 50,
             written_at=now, weight=1.0, last_active_at=now)
    ms.update_fields(e, written_at=None)
    m = ms.get_entry(e)
    assert "written_at" not in m
    assert _soft_residency_grace_ts(m, now, entry=e) is None
    assert _rule_rank(e, m, now) <= 1.0
    assert _drain(tmp_store, ms) == [e]


def test_b3_clamped_writeback_rank_bounded_and_decays(
        tmp_store, meta_for, monkeypatch):
    """夹 now 写回只产生有界 ×GRACE_MULT 抬升; 过窗后随 w_eff 衰减."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "B3 夹now写回: " + "丙" * 50,
             written_at=now, weight=1.0, last_active_at=now)
    m = ms.get_entry(e)
    r_now = _rule_rank(e, m, now)
    assert r_now <= 1.0 * config_mod.GRACE_MULT
    r_after_window = _rule_rank(
        e, m, now + timedelta(days=config_mod.RULE_MIN_RESIDENCY_DAYS + 1))
    assert r_after_window < r_now
    # 过窗后仍可被压力换出。
    assert _select_retirement_candidates(
        tmp_store, ms, "memory", 1, {}) == [e]


def test_b3_embedded_future_date_anchored_visible_and_selectable(
        tmp_store, meta_for):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    e = "B3 内嵌未来 2099-01-01 记录"
    assert tmp_store.add("memory", e).get("success")
    r = ms.reconcile(tmp_store.entries("memory"))
    assert r["stamped"] == 1
    m = ms.get_entry(e)
    written = datetime.fromisoformat(m["written_at"])
    assert written <= datetime.now(UTC) + timedelta(seconds=300)
    assert "2099" not in m["written_at"]
    snap = meta_mod._ts_anomaly_snapshot()
    assert snap["count"] >= 1
    assert any(x.get("field") == "embedded_date" for x in snap["recent"]), snap
    # 夹 now 后最多带来有限 ×GRACE_MULT 的位次抬升, 仍可清空。
    assert _rule_rank(e, m, datetime.now(UTC)) <= (
        config_mod.GRACE_MULT * config_mod.WEIGHT_PROTECT_MULT)
    assert _drain(tmp_store, ms) == [e]


def test_b3_all_multiplier_protected_combo_drainable(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    entries = []
    for i in range(9):
        entries.append(_add(
            tmp_store, ms, f"B3 全乘数{i}: ".ljust(20, "甲"),
            written_at=now - timedelta(hours=i), weight=5.0,
            protected=True))
    removed = _drain(tmp_store, ms, need=10 ** 9, cap=MAX_EVICT_PER_RUN)
    assert set(removed) == set(entries), removed
    assert len(removed) == len(entries)


def test_b3_bad_input_plus_protected_no_permanent_residency(
        tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    items = [
        _add(tmp_store, ms, "B3 坏压甲: " + "甲" * 40,
             written_at=now + timedelta(days=400), weight=5.0,
             protected=True),
        _add(tmp_store, ms, "B3 坏压乙: " + "乙" * 40,
             written_at=now, weight=5.0, protected=True,
             reconcile_anchor_fallback=True),
        _add(tmp_store, ms, "B3 坏压丙: " + "丙" * 40,
             written_at=now, weight=0.01, last_recall_hit_at=None),
    ]
    assert set(_drain(tmp_store, ms)) == set(items)


def test_b3_all_multiplier_protected_enforce_converges(
        tmp_store, meta_for, monkeypatch):
    """B3 压测: 9 条全新鲜 ×protected 高权, enforce 多 call 仍能清零, 且每次
    全文换出 ≤ MAX_EVICT_PER_RUN."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    entries = []
    for i in range(6):
        e = _add(tmp_store, ms, f"B3-ENF-{i}:" + "甲" * 120,
                 written_at=now, weight=5.0, protected=True)
        entries.append(e)
    _patch_budget(monkeypatch, 0, stub_budget=RULE_BUDGET_CHARS)
    cold = _SelectiveCold()
    for _ in range(5):
        stat = {}
        ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
        assert stat["lru_evicted"] <= MAX_EVICT_PER_RUN, stat
        content = [e for e in tmp_store.entries("memory")
                   if (ms.get_entry(e) or {}).get("type") != "stub"]
        if not content:
            break
    remaining = [e for e in tmp_store.entries("memory")
                 if (ms.get_entry(e) or {}).get("type") != "stub"]
    assert remaining == [], ("全乘数+protected 组合必须能清空", remaining)
    assert set(cold.stored) == set(entries)


# =========================================================================
# P1 候选饥饿: need 极小 + 头部永久冷失败
# =========================================================================

def test_p1_need_one_head_cold_fail_still_attempts_next(
        tmp_store, meta_for, monkeypatch):
    """REVIEW7 H-1.2: need=1, 最低 rank A 永久冷失败 → B 同 call 内被尝试."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    a = _add(tmp_store, ms, "P1-A:" + "甲" * 94, written_at=old, weight=0.1)
    b = _add(tmp_store, ms, "P1-B:" + "乙" * 94, written_at=old, weight=1.0)
    c = _add(tmp_store, ms, "P1-C:" + "丙" * 94, written_at=old, weight=2.0)
    total = sum(len(e) for e in (a, b, c))
    _patch_budget(monkeypatch, total - 1)
    cold = _SelectiveCold(fail_for={a})
    stat = {}
    ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
    assert cold.attempts[:2] == [a, b], cold.attempts
    assert b in cold.stored and a not in cold.stored
    assert stat["lru_evicted"] == 1
    content = sum(len(e) for e in tmp_store.entries("memory")
                  if (ms.get_entry(e) or {}).get("type") != "stub")
    assert content <= total - 1, (content, total)
    assert c in tmp_store.entries("memory")


def test_p1_single_call_cap_holds_with_persistent_head_failures(
        tmp_store, meta_for, monkeypatch):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    entries = []
    for i in range(9):
        # 固定等长, need 远大于单条, 强制多批; 前 3 条永久冷失败.
        entries.append(_add(tmp_store, ms, f"P1C{i:02d}:" + "甲" * 190,
                            written_at=old, weight=0.1 + i * 0.01))
    total = sum(len(e) for e in entries)
    _patch_budget(monkeypatch, total - 5 * len(entries[0]))
    cold = _SelectiveCold(fail_for=set(entries[:3]))
    stat = {}
    ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
    assert stat["lru_evicted"] == MAX_EVICT_PER_RUN, stat
    assert len(cold.stored) == MAX_EVICT_PER_RUN
    assert stat["lru_evicted"] <= MAX_EVICT_PER_RUN
    # 前三名失败候选确实被尝试过; 后续候选在单 call 内被尝试并成功.
    assert all(e in cold.attempts for e in entries[:3]), cold.attempts
    assert entries[3] in cold.attempts, cold.attempts


def test_p1_multi_round_converges_when_candidate_write_succeeds(
        tmp_store, meta_for, monkeypatch):
    """H-1.2 有限轮收敛的跨调用版本: 头部永久失败, 每轮至少挪动一条."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    a = _add(tmp_store, ms, "P1R-A:" + "甲" * 94, written_at=old, weight=0.1)
    b = _add(tmp_store, ms, "P1R-B:" + "乙" * 94, written_at=old, weight=1.0)
    c = _add(tmp_store, ms, "P1R-C:" + "丙" * 94, written_at=old, weight=2.0)
    total = sum(len(e) for e in (a, b, c))
    _patch_budget(monkeypatch, total - 1)
    cold = _SelectiveCold(fail_for={a})
    rounds = []
    for i in range(5):
        content = sum(len(e) for e in tmp_store.entries("memory")
                      if (ms.get_entry(e) or {}).get("type") != "stub")
        if content <= total - 1:
            break
        stat = {}
        ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
        rounds.append({
            "round": i,
            "lru": stat.get("lru_evicted", 0),
            "attempts": list(cold.attempts),
            "content": content,
        })
        assert stat.get("lru_evicted", 0) <= MAX_EVICT_PER_RUN
    final = sum(len(e) for e in tmp_store.entries("memory")
                if (ms.get_entry(e) or {}).get("type") != "stub")
    assert final <= total - 1, (rounds, final)
    assert len(rounds) == 1, rounds


# =========================================================================
# P2 type 安全默认
# =========================================================================

def test_p2_stamp_type_none_or_invalid_safe_default(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    cases = [
        ("P2 普通文本: " + "甲" * 20, None, {"rule", "state"}),
        ("P2 非法类型: " + "乙" * 20, "bogus", {"rule", "state"}),
        ("[规则指针]P2stub->recall:" + "c" * 16, None, {"stub"}),
    ]
    for text, bad_type, allowed in cases:
        assert tmp_store.add("memory", text).get("success")
        ms.stamp(text, bad_type, written_at=now, updated_at=now,
                 last_active_at=now, weight=1.0)
        m = ms.get_entry(text)
        assert m.get("type") in allowed, (text, m)
        assert m.get("type") is not None, (text, m)
    # 所有条目仍在统一候选池中, 不因 type:null 永久脱离.
    removed = set(_drain(tmp_store, ms))
    assert all(c[0] in removed for c in cases)


# =========================================================================
# P3 内嵌日期锚点
# =========================================================================

def test_p3_reconcile_embedded_future_uses_anchor(tmp_store, meta_for):
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    e = "P3 reconcile 未来内嵌 2099-12-31"
    tmp_store.add("memory", e)
    ms.reconcile(tmp_store.entries("memory"))
    m = ms.get_entry(e)
    assert "2099" not in m["written_at"]
    written = datetime.fromisoformat(m["written_at"])
    assert written <= datetime.now(UTC) + timedelta(seconds=300)
    snap = meta_mod._ts_anomaly_snapshot()
    assert any(x.get("field") == "embedded_date" for x in snap["recent"]), snap


def test_p3_tool_retype_embedded_future_uses_anchor(tmp_store, meta_for):
    _restamp = _load_retype_tool()._restamp
    meta_mod._reset_ts_anomaly()
    ms = meta_for("memory")
    e = "P3 retype 工具未来内嵌 2099-12-31"
    tmp_store.add("memory", e)
    ms.stamp(e, "rule", written_at=datetime.now(UTC),
             updated_at=datetime.now(UTC), last_active_at=datetime.now(UTC),
             weight=1.0)
    _restamp(ms, e, ms.get_entry(e), is_already_state=False)
    m = ms.get_entry(e)
    assert "2099" not in m["written_at"], m
    written = datetime.fromisoformat(m["written_at"])
    assert written <= datetime.now(UTC) + timedelta(seconds=300)
    snap = meta_mod._ts_anomaly_snapshot()
    assert any(x.get("field") == "embedded_date" for x in snap["recent"]), snap


# =========================================================================
# P4 set_entry_type 清 ambiguous 审计键
# =========================================================================

def test_p4_set_entry_type_clears_ambiguous_audit_keys(
        tmp_store, meta_for, mock_client):
    server._store = tmp_store
    server._client = mock_client
    ms = meta_for("memory")
    now = datetime.now(UTC)
    e = _add(tmp_store, ms, "P4 改型清审计: " + "甲" * 30,
             written_at=now - timedelta(days=30),
             updated_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30), weight=1.0,
             judge_decision="ambiguous", judge_resolution=None,
             judge_review_at=now + timedelta(days=7),
             judge_reviewed_at=now, judge_resolved_at=now,
             judge_review_count=2)
    raw = server.memorycore_set_entry_type(
        target="memory", match_text="P4 改型清审计", type_override="rule")
    out = json.loads(raw)
    assert out.get("status") == "ok", out
    m = ms.get_entry(e)
    for k in ("judge_review_at", "judge_reviewed_at", "judge_resolved_at"):
        assert k not in m, (k, m)
    assert m.get("judge_decision") == "rule"
    assert m.get("judge_resolution") == "manual_override"


# =========================================================================
# P5 口径: T3 budget 0 与 stub GC 独立
# =========================================================================

def test_p5_budget_zero_evict_cap_scopes(tmp_store, meta_for, monkeypatch):
    """--budget 0 时全文换出仍 ≤MAX_EVICT_PER_RUN; 冷写失败零本地删除."""
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    entries = [_add(tmp_store, ms, f"P5T3-{i}:" + "甲" * 180,
                    written_at=old, weight=1.0 + i)
               for i in range(6)]
    _patch_budget(monkeypatch, 0, stub_budget=0)
    cold = _SelectiveCold(fail_for={entries[0]})
    stat = {}
    ov.enforce_rule_budget(tmp_store, cold, "memory", ms, stat)
    assert stat["lru_evicted"] <= MAX_EVICT_PER_RUN, stat
    assert stat["lru_evicted"] == len(cold.stored), stat
    assert entries[0] in tmp_store.entries("memory"), "冷失败必须保留本地"
    assert stat.get("t3_cold_only_mode") is True


def test_p5_stub_gc_has_independent_cap_and_not_lru_counted(
        tmp_store, meta_for, monkeypatch):
    ms = meta_for("memory")
    now = datetime.now(UTC)
    old = now - timedelta(days=30)
    stubs = []
    for i in range(6):
        e = _add(tmp_store, ms, f"[规则指针]P5GC{i}->recall:" + "a" * 16,
                 typ="stub", written_at=old, updated_at=old,
                 last_active_at=old, weight=1.0 + i,
                 last_evicted_at=old, retire_count=2)
        stubs.append(e)
    monkeypatch.setattr(ov, "MAX_STUB_PER_RUN", 2)
    stat = {"stub_gc": 0, "lru_evicted": 0}
    ov._stub_gc(tmp_store, ms, "memory", stat, force=True)
    assert stat["stub_gc"] <= 2, stat
    assert stat["lru_evicted"] == 0, stat
