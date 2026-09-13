#!/usr/bin/env python3
"""tests/test_soft_residency_fix5.py — FIX8 B1/B2 新鲜窗口乘数语义。

语义 (FIX8 换挡后):
  - 新鲜窗口 = written_at 或 last_recall_hit_at 距今 ≤
    RULE_MIN_RESIDENCY_DAYS (7); 窗口内 rank 乘 GRACE_MULT;
  - 统一候选池 + 单一 rank, 没有 normals/graces 分组 / grace_fallback;
  - 新鲜不是资格: 低权新鲜仍按 rank 先出, 足够压力下全池可清空;
  - 窗口 <=0 → 关闭乘数, 精确回到纯 _rule_rank;
  - reconcile_anchor_fallback=True → 不给新鲜乘数, 但仍留在候选池。

断言全部为行为断言 (实际选出的条目集合/顺序/rank), 无恒真文本特判。
"""
import hashlib
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import MAX_EVICT_PER_RUN  # noqa: E402
from memorycore.core.overflow import _in_soft_residency, _rule_rank  # noqa: E402
from memorycore.core.overflow import _select_retirement_candidates  # noqa: E402


def _add(tmp_store, meta_for, text, *, written_at=None, updated_at=None,
         last_recall_hit_at=None, weight=1.0, entry_type="rule",
         protected=None, reconcile_anchor_fallback=None):
    tmp_store.add("memory", text)
    kw = {"weight": weight}
    if written_at is not None:
        kw["written_at"] = written_at
    if updated_at is not None:
        kw["updated_at"] = updated_at
    if last_recall_hit_at is not None:
        kw["last_recall_hit_at"] = last_recall_hit_at
    if protected is not None:
        kw["protected"] = protected
    if reconcile_anchor_fallback is not None:
        kw["reconcile_anchor_fallback"] = reconcile_anchor_fallback
    meta_for("memory").stamp(text, entry_type, **kw)
    return text


def _pure_rule_rank_order(tmp_store, meta_for):
    """独立参考实现 (不含新鲜乘数), 用于 soft=0 关闭口径对照。"""
    now = datetime.now(timezone.utc)
    ms = meta_for("memory")
    cands = []
    for e in tmp_store.entries("memory"):
        m = ms.get_entry(e) or {}
        weight = float(m.get("weight") or 1.0)
        last = (m.get("last_active_at") or m.get("written_at")
                or m.get("updated_at") or "")
        days = 0
        if last:
            try:
                dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                days = max((now - dt).days, 0)
            except ValueError:
                days = 0
        eff = weight * math.pow(0.5, days / 30.0)
        mult = 3.0 if m.get("protected") is True else 1.0
        cands.append(((eff * mult, last, -len(e),
                       hashlib.sha256(e.encode()).hexdigest()), e))
    cands.sort(key=lambda c: c[0])
    return [e for _k, e in cands]


# ---------------------------------------------------------------------------
# B1: 新鲜窗口是乘数而非资格
# ---------------------------------------------------------------------------

def test_fresh_multiplier_changes_order_but_old_can_still_suffice(
        tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    old = _add(tmp_store, meta_for, "旧高权: " + "甲" * 60,
               written_at=now - timedelta(days=30), weight=5.0)  # rank 5
    new = _add(tmp_store, meta_for, "新写入: " + "乙" * 60,
               written_at=now, weight=1.0)                      # rank 9
    assert _in_soft_residency(ms.get_entry(new), now, entry=new) is True
    assert _rule_rank(old, ms.get_entry(old), now) < _rule_rank(
        new, ms.get_entry(new), now)

    # 典型压力由旧条目单独满足 → 新写入不进候选 (保护位次但非资格)。
    need = len(old)
    sel = _select_retirement_candidates(tmp_store, ms, "memory", need, {})
    assert sel == [old], sel
    assert new not in sel

    # 压力再大一点 → 新写入照常被换出, 且排在旧之后。
    sel2 = _select_retirement_candidates(tmp_store, ms, "memory",
                                         need + 1, {})
    assert sel2 == [old, new], sel2


def test_expired_window_returns_to_pure_rank(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    low = _add(tmp_store, meta_for, "过期低权: " + "低" * 60,
               written_at=now - timedelta(days=8), weight=0.5)
    high = _add(tmp_store, meta_for, "过期高权: " + "高" * 60,
                written_at=now - timedelta(days=8), weight=5.0)
    assert not _in_soft_residency(ms.get_entry(low), now, entry=low)
    assert not _in_soft_residency(ms.get_entry(high), now, entry=high)
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})
    assert sel == _pure_rule_rank_order(tmp_store, meta_for) == [low, high], sel


def test_recall_writeback_gets_fresh_multiplier(tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    old = _add(tmp_store, meta_for, "写回旧基: " + "甲" * 60,
               written_at=now - timedelta(days=30), weight=5.0)
    wb = _add(tmp_store, meta_for, "写回新鲜: " + "乙" * 60,
              written_at=now - timedelta(days=30),
              last_recall_hit_at=now, weight=1.0)
    assert _in_soft_residency(ms.get_entry(wb), now, entry=wb)
    assert _rule_rank(old, ms.get_entry(old), now) < _rule_rank(
        wb, ms.get_entry(wb), now)
    sel = _select_retirement_candidates(tmp_store, ms, "memory", len(old), {})
    assert sel == [old], sel
    sel2 = _select_retirement_candidates(tmp_store, ms, "memory",
                                         len(old) + 1, {})
    assert sel2 == [old, wb], sel2


def test_all_fresh_still_fully_selectable_no_permanent_residency(
        tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    entries = [
        _add(tmp_store, meta_for, "全新鲜甲: " + "甲" * 50,
             written_at=now - timedelta(days=6), weight=5.0),
        _add(tmp_store, meta_for, "全新鲜乙: " + "乙" * 50,
             written_at=now - timedelta(days=3), weight=1.0),
        _add(tmp_store, meta_for, "全新鲜丙: " + "丙" * 50,
             written_at=now - timedelta(days=1), weight=0.1),
    ]
    sel = _select_retirement_candidates(tmp_store, ms, "memory",
                                        10 ** 9, {})
    # 全新鲜时排序仍是 w × GRACE_MULT 升序 (低权先出), 但一个不少。
    assert set(sel) == set(entries), sel
    assert sel == [entries[2], entries[1], entries[0]], sel
    assert all(_in_soft_residency(ms.get_entry(e), now, entry=e)
               for e in entries)


def test_reconcile_fallback_removes_multiplier_not_candidacy(
        tmp_store, meta_for):
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    flagged = _add(tmp_store, meta_for, "兜底出生: " + "甲" * 50,
                   written_at=now, weight=1.0,
                   reconcile_anchor_fallback=True)
    normal = _add(tmp_store, meta_for, "普通旧: " + "乙" * 50,
                  written_at=now, weight=1.0,
                  reconcile_anchor_fallback=False)
    m_flag = ms.get_entry(flagged)
    m_norm = ms.get_entry(normal)
    assert _in_soft_residency(m_flag, now, entry=flagged) is False
    assert _rule_rank(flagged, m_flag, now) == 1.0
    assert _rule_rank(normal, m_norm, now) == float(config_mod.GRACE_MULT)
    # 没有乘数 ≠ 不进候选池: 兜底条目仍按 rank 先出。
    need = 1
    assert _select_retirement_candidates(
        tmp_store, ms, "memory", need, {})[0] == flagged


# ---------------------------------------------------------------------------
# 回滚口径: 窗口<=0 = 精确纯 rank; 乘数常量<=0 亦关闭
# ---------------------------------------------------------------------------

def test_min_residency_zero_is_exact_pure_rule_rank(tmp_store, meta_for,
                                                    monkeypatch):
    monkeypatch.setattr(ov, "RULE_MIN_RESIDENCY_DAYS", 0)
    monkeypatch.setattr(config_mod, "RULE_MIN_RESIDENCY_DAYS", 0)
    now = datetime.now(timezone.utc)
    fresh = _add(tmp_store, meta_for, "关断新: " + "新" * 50,
                 written_at=now, weight=0.1)
    old_a = _add(tmp_store, meta_for, "关断老甲: " + "甲" * 50,
                 written_at=now - timedelta(days=30), weight=5.0)
    old_b = _add(tmp_store, meta_for, "关断老乙: " + "乙" * 50,
                 written_at=now - timedelta(days=20), weight=1.0)
    ms = meta_for("memory")
    expected = _pure_rule_rank_order(tmp_store, meta_for)[:MAX_EVICT_PER_RUN]
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})
    assert sel == expected, f"关断后必须逐条一致: {sel} != {expected}"
    assert sel[0] == fresh, "窗口<=0 时新写入按原 rank 首发"
    assert _in_soft_residency(ms.get_entry(fresh), now, entry=fresh) is False


def test_grace_mult_zero_rolls_back_to_pure_rank(tmp_store, meta_for,
                                                 monkeypatch):
    monkeypatch.setattr(ov, "GRACE_MULT", 0)
    monkeypatch.setattr(config_mod, "GRACE_MULT", 0)
    now = datetime.now(timezone.utc)
    fresh = _add(tmp_store, meta_for, "乘数关新: " + "新" * 50,
                 written_at=now, weight=0.1)
    old = _add(tmp_store, meta_for, "乘数关旧: " + "旧" * 50,
               written_at=now - timedelta(days=30), weight=0.5)
    ms = meta_for("memory")
    assert _in_soft_residency(ms.get_entry(fresh), now, entry=fresh)
    assert _rule_rank(fresh, ms.get_entry(fresh), now) == 0.1
    assert _select_retirement_candidates(
        tmp_store, ms, "memory", 10 ** 6, {}) == [fresh, old]


# ---------------------------------------------------------------------------
# CACHE_POLICY_V2=0 legacy 对照 (排序主体冻结)
# ---------------------------------------------------------------------------

def test_cache_policy_v2_off_legacy_unchanged(tmp_store, meta_for,
                                              monkeypatch):
    monkeypatch.setattr(config_mod, "CACHE_POLICY_V2", False)
    monkeypatch.setattr(config_mod, "PROTECT_SKIP_LRU", True)
    now = datetime.now(timezone.utc)
    old = _add(tmp_store, meta_for, "回滚老规则: " + "老" * 60,
               written_at=now - timedelta(days=100),
               updated_at=now - timedelta(days=100), weight=1.0)
    fresh = _add(tmp_store, meta_for, "回滚新规则: " + "新" * 60,
                 written_at=now, updated_at=now, weight=0.1)
    prot = _add(tmp_store, meta_for, "回滚保护规则: " + "护" * 60,
                written_at=now - timedelta(days=100),
                updated_at=now - timedelta(days=100), weight=0.01,
                protected=True)

    ms = meta_for("memory")
    assert ov._cache_policy_v2() is False
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})

    # legacy 语义不变: 新鲜条目被年龄门挡 (age=0<7), protected 资格豁免,
    # 仅 100 天旧规则可退役。
    assert sel == [old], sel
    assert fresh not in sel and prot not in sel
