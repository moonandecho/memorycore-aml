#!/usr/bin/env python3
"""tests/test_lru_budget.py — Phase 4 热层规则预算制 (LRU 挤权) 单元测试。

覆盖 Pi 设计阶段 1 验收点:
  ① 超预算 → 最低权重规则被 stub、占用回落、冷层有全文
  ② 全驻留期内 → 挤 0 条
  ③ 冷层故障 → 零删除、errors 计数
  ④ 同权重 tie-break 顺序确定 (幂等)
  ⑤ 召回恢复: stub cold_id 命中 → 全文回热层, 新键高权重复活
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import overflow as ov
from memorycore.core.config import (RULE_BUDGET_CHARS, RULE_MIN_RESIDENCY_DAYS,
                         WEIGHT_INIT, WEIGHT_MAX, STUB_PREFIX)
from memorycore.core.overflow import (_rule_weight_eff, _bump_weight, _lex_evidence,
                           _select_retirement_candidates, enforce_rule_budget,
                           restore_stubs_from_results)
from conftest import MockMnemosyneClient


def _add_rules(tmp_store, meta_for, n, chars_each=200, days=100, weight=None,
               prefix="规则"):
    """加 n 条超驻留期规则 (同权重默认), 返回条目列表。"""
    entries = []
    for i in range(n):
        e = f"{prefix}{i}: " + "细节" * (chars_each // 2)
        tmp_store.add("memory", e)
        m = meta_for("memory")
        kw = {"updated_at": datetime.now(timezone.utc) - timedelta(days=days)}
        if weight is not None:
            kw["weight"] = weight
        m.stamp(e, "rule", **kw)
        entries.append(e)
    return entries


def test_weight_eff_decay():
    """惰性折现: 30 天减半, 0 天原值, 缺失字段回退。"""
    now = datetime.now(timezone.utc)
    m30 = {"weight": 1.0, "last_active_at": (now - timedelta(days=30)).isoformat()}
    assert abs(_rule_weight_eff(m30, now) - 0.5) < 1e-6
    m0 = {"weight": 1.0, "last_active_at": now.isoformat()}
    assert abs(_rule_weight_eff(m0, now) - 1.0) < 1e-6
    m_missing = {"type": "rule"}  # 零迁移: 无 weight/last_active
    assert _rule_weight_eff(m_missing, now) == WEIGHT_INIT


def test_bump_weight_refresh_anchor():
    """强命中刷新锚点并封顶; 弱命中不刷新锚点。"""
    now = datetime.now(timezone.utc)
    meta = {"weight": WEIGHT_INIT, "last_active_at": (now - timedelta(days=90)).isoformat()}
    eff_before = _rule_weight_eff(meta, now)
    _bump_weight(meta, 1.0, refresh_anchor=True, now=now)
    assert meta["weight"] <= WEIGHT_MAX
    assert meta["last_active_at"] == now.isoformat()  # 锚点刷新
    # 封顶
    meta2 = {"weight": WEIGHT_MAX, "last_active_at": now.isoformat()}
    _bump_weight(meta2, 1.0, refresh_anchor=True, now=now)
    assert meta2["weight"] == WEIGHT_MAX


def test_lex_evidence():
    """词法弱命中: sb≥2 命中, 无关不命中 (实测标定)。"""
    assert _lex_evidence("打印机怎么设双面打印", "小米激光打印机 K100")
    assert not _lex_evidence("今晚吃什么", "用户偏好回答结论先行")


def test_budget_overflow_stubs_lowest_weight(tmp_store, mock_client, meta_for):
    """① 超预算 → 最低权重规则被 stub, 冷层有全文。"""
    # 权重 0.2 和 2.0 各一条 (0.2 更低 → 先挤)
    e_low = _add_rules(tmp_store, meta_for, 1, chars_each=250, days=100, weight=0.2, prefix="低权")[0]
    e_high = _add_rules(tmp_store, meta_for, 1, chars_each=250, days=100, weight=2.0, prefix="高权")[0]
    # 加超预算填充 (rule 型, 超驻留期)
    tmp_store.add("memory", "填充甲" + "乙" * (RULE_BUDGET_CHARS))
    meta_for("memory").stamp(tmp_store.entries("memory")[-1], "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=100))
    stat = {}
    enforce_rule_budget(tmp_store, mock_client, "memory", meta_for("memory"), stat)
    ents = tmp_store.entries("memory")
    assert e_low not in ents, "最低权重规则应被 stub"
    assert any(e.startswith(STUB_PREFIX) for e in ents), "应产生 stub 指针"
    assert stat.get("lru_evicted", 0) >= 1
    assert mock_client.stored, "冷层应有全文"


def test_budget_no_age_gate_evicts_lowest_weight_fresh(tmp_store, mock_client, meta_for):
    """CACHE-POLICY-V2: 无年龄门, 刚写入的低权重规则也可被预算挤权。"""
    e_low = _add_rules(tmp_store, meta_for, 1, chars_each=250, days=0,
                       weight=0.1, prefix="低权新鲜")[0]
    e_high = _add_rules(tmp_store, meta_for, 1, chars_each=250, days=0,
                        weight=5.0, prefix="高权新鲜")[0]
    tmp_store.add("memory", "填充甲" + "乙" * (RULE_BUDGET_CHARS))
    meta_for("memory").stamp(tmp_store.entries("memory")[-1], "rule",
                             weight=5.0,
                             updated_at=datetime.now(timezone.utc))
    stat = {}
    enforce_rule_budget(tmp_store, mock_client, "memory",
                        meta_for("memory"), stat)
    ents = tmp_store.entries("memory")
    assert e_low not in ents, "无年龄门: 低权重新鲜条目也应先换出"
    assert stat.get("lru_evicted", 0) >= 1 and stat.get("errors", 0) == 0
    assert mock_client.stored, "冷层先写成功才换出"
    assert e_low in mock_client.stored
    assert any(x.startswith(STUB_PREFIX) for x in ents)


def test_budget_cold_failure_keeps_local(tmp_store, meta_for):
    """③ 冷层故障 → 零删除、errors 计数。"""
    e1 = _add_rules(tmp_store, meta_for, 1, chars_each=250, days=100)[0]
    tmp_store.add("memory", "填充甲" + "乙" * (RULE_BUDGET_CHARS))
    meta_for("memory").stamp(tmp_store.entries("memory")[-1], "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=100))
    bad = MockMnemosyneClient(fail_remember=True)
    stat = {}
    enforce_rule_budget(tmp_store, bad, "memory", meta_for("memory"), stat)
    assert e1 in tmp_store.entries("memory"), "冷层失败保留本地"
    assert stat.get("errors", 0) >= 1
    assert stat.get("lru_evicted", 0) == 0


def test_retirement_candidates_deterministic(tmp_store, meta_for):
    """④ 同权重 tie-break 确定: 更老优先, 同老更短优先, sha256 兜底。"""
    now = datetime.now(timezone.utc)
    e_old = _add_rules(tmp_store, meta_for, 1, chars_each=100, days=100, prefix="老")[0]
    e_new = _add_rules(tmp_store, meta_for, 1, chars_each=100, days=10, prefix="新")[0]
    sel1 = _select_retirement_candidates(tmp_store, meta_for("memory"), "memory", 1, {})
    assert e_old in sel1 and e_new not in sel1, "更老先挤"
    sel2 = _select_retirement_candidates(tmp_store, meta_for("memory"), "memory", 1, {})
    assert sel1 == sel2, "幂等确定"


def test_restore_stub_from_results(tmp_store, meta_for):
    """⑤ 召回恢复: stub cold_id 命中 → 全文回热层, 新键高权重 + importance 透传。"""
    e = "规则甲: 关键偏好内容。" 
    tmp_store.add("memory", e)
    cold_id = "m-cold-1"
    # 手工造 stub + cold_id meta (importance 0.95 模拟受保护规则)
    stub = "[规则指针]规则甲→recall(\"规则甲\")"
    tmp_store.replace("memory", e, stub)
    meta_for("memory").stamp(stub, "stub", origin="stub_sink", cold_id=cold_id,
                             importance=0.95)
    results = [{"id": cold_id, "content": e, "dense_score": 0.9}]
    remaining = restore_stubs_from_results(
        tmp_store, {"memory": meta_for("memory")}, results)
    ents = tmp_store.entries("memory")
    assert e in ents, "stub 应恢复为全文"
    assert stub not in ents
    assert remaining == [], "已恢复条目应从注入剔除"
    m = meta_for("memory").get_entry(e)
    assert m and m.get("type") == "rule" and m.get("origin") == "stub_restore"
    assert m.get("weight", 0) > WEIGHT_INIT, "恢复后高权重"
    assert m.get("importance") == 0.95, "L3: importance 保护线透传"


# ---- 修复回归测试 (2026-08-26 Pi 检查发现 S1/S2/S3) ------------------------

def _mk_meta(tmp_store, meta_for, entry, days=100):
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=days))
    return entry


def test_fresh_queries_take_recent_not_oldest(tmp_store, meta_for, tmp_path,
                                              monkeypatch):
    """S2 回归: fresh 查询取最近而非最老 (追加式日志尾部=最新)。"""
    from memorycore.core import config as config_mod
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # 写 60 条查询 (时间递增)
    for i in range(60):
        meta_mod.log_activity_query(f"查询内容{i}")
    from memorycore.core.overflow import _load_fresh_queries
    # last_scan_at 为空 → 下界 = now-30d → 全部 fresh, 但 cap=50 → 取最近 50
    fresh = _load_fresh_queries({"memory": meta_for("memory")})
    assert len(fresh) == 50, f"cap 应取 50 条, got {len(fresh)}"
    assert "查询内容59" in fresh, "应包含最新查询"
    assert "查询内容0" not in fresh, "最老查询应被 cap 挤掉"


def test_last_scan_at_persists_anchor(tmp_store, meta_for, tmp_path,
                                      monkeypatch):
    """S3 回归: last_scan_at 落盘 — 第二轮同批查询不再加分。"""
    from memorycore.core import config as config_mod
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    entry = _mk_meta(tmp_store, meta_for, "规则乙: 打印机配置细节。")
    meta_mod.log_activity_query("打印机怎么设置")
    from memorycore.core.overflow import apply_activity_hits
    from memorycore.core.config import HIT_STRONG_COS
    # mock embed: 返回固定向量, "打印机怎么设置" 与规则余弦 ≥ 0.48
    class FakeClient:
        def embed_texts(self, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)  # 全 1 维向量 → cos=1
    stat = {}
    apply_activity_hits({"memory": meta_for("memory")},
                        {"memory": [entry]}, FakeClient(), stat)
    m1 = meta_for("memory").get_entry(entry)
    assert m1.get("last_scan_at"), "last_scan_at 应落盘"
    w1 = m1.get("weight")
    # 第二轮: 同批查询 (ts 未变) → last_scan_at 锚点 → 无新 fresh → 不加分
    stat2 = {}
    apply_activity_hits({"memory": meta_for("memory")},
                        {"memory": [entry]}, FakeClient(), stat2)
    m2 = meta_for("memory").get_entry(entry)
    assert m2.get("weight") == w1, f"锚点防重复: weight 不应变 ({w1} -> {m2.get('weight')})"


def test_candidate_selection_lexical_without_llm(tmp_store, meta_for, tmp_path,
                                                 monkeypatch):
    """S1 回归 (v2 口径, 2026-09-12): 候选筛选纯词法零 LLM; 词法活跃不再一票豁免。

    旧语义 "词法活跃 → 不挤" 已废弃 (DESIGN §Q3): 活跃只抬高年龄门槛
    (warm 14 天) 与排序, 不再给资格否决 — 100 天的 warm 条目照常进候选。
    """
    from memorycore.core import config as config_mod
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    from memorycore.core.overflow import _select_retirement_candidates
    # 词法活跃的规则 (7 天内有相关查询) → warm 级, 年龄 100 天 ≥ 14 → 可进候选
    e_active = _mk_meta(tmp_store, meta_for, "规则丙: 打印机型号与耗材。")
    meta_mod.log_activity_query("打印机怎么设置双面打印")
    # 非活跃规则 (7 天无相关查询) → idle 级, 年龄 100 天 ≥ 7 → 可进候选
    e_idle = _mk_meta(tmp_store, meta_for, "规则丁: 远古技术方案记录。")
    need = sum(len(e) for e in tmp_store.entries("memory")) + 1
    sel = _select_retirement_candidates(tmp_store, meta_for("memory"), "memory", need, {})
    assert e_active in sel, "词法活跃不再一票豁免 (100 天 warm 条目可进候选)"
    assert e_idle in sel, "非活跃规则应可被挤 (无 LLM 依赖)"


def test_retirement_candidates_no_age_gate_audit_only(tmp_store, meta_for):
    """CACHE-POLICY-V2: 年龄不再参与候选资格; activity_tier 仅审计解释。"""
    now = datetime.now(timezone.utc)
    fresh = _add_rules(tmp_store, meta_for, 1, chars_each=120, days=0,
                       prefix="零天")[0]
    old = _add_rules(tmp_store, meta_for, 1, chars_each=120, days=200,
                     prefix="二百天")[0]
    sel = _select_retirement_candidates(tmp_store, meta_for("memory"),
                                        "memory", 10 ** 6, {})
    assert fresh in sel and old in sel, "年龄不再阻断资格, 同池可挤"
    tier, min_age = ov._rule_activity_tier(
        meta_for("memory").get_entry(fresh) or {}, fresh, [], now)
    assert tier in ("idle", "warm", "active") and min_age >= 0
    # 缺失/未来时间字段也不得让候选池"永不"命中
    sel_all = _select_retirement_candidates(
        tmp_store, meta_for("memory"), "memory", 10 ** 6, {})
    assert fresh in sel_all and old in sel_all


def test_protected_in_pool_with_multiplier(tmp_store, meta_for):
    """CACHE-POLICY-V2 Q5: protected 只乘 3.0, 不再排除; 同池按 rank 排序。"""
    from memorycore.core.config import WEIGHT_PROTECT_MULT
    from memorycore.core.overflow import _rule_rank
    now = datetime.now(timezone.utc)
    ms = meta_for("memory")
    # FIX5: 本用例比 protected 乘数下的 rank 顺序, 两条都必须是
    # 非宽限旧条目 (同时盖 written_at); 否则会被新软驻留按宽限时间排序。
    _old_ts = now - timedelta(days=20)
    p = "行为准则: 汇报系统状态必须结论先行。"
    tmp_store.add("memory", p)
    ms.stamp(p, "rule", written_at=_old_ts, updated_at=_old_ts, weight=1.0)
    o = "普通规则: 某技术方案选型记录。"
    tmp_store.add("memory", o)
    ms.stamp(o, "rule", written_at=_old_ts, updated_at=_old_ts, weight=1.0)
    mp = ms.get_entry(p)
    assert abs(_rule_rank(p, mp, now) - _rule_weight_eff(mp, now) * WEIGHT_PROTECT_MULT) < 1e-9
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})
    assert p in sel and o in sel, "protected 必须进候选池"
    assert sel.index(o) < sel.index(p), "同权重下 protected 靠后 (更难挤, 非豁免)"


def test_protect_skip_lru_rollback(tmp_store, meta_for, monkeypatch):
    """PROTECT_SKIP_LRU=0 回滚: protected 参与排序 (×WEIGHT_PROTECT_MULT 旧语义)。"""
    from datetime import datetime, timedelta, timezone
    from memorycore.core import config as config_mod
    from memorycore.core.overflow import _select_retirement_candidates, _rule_rank
    monkeypatch.setattr(config_mod, "PROTECT_SKIP_LRU", False)
    now = datetime.now(timezone.utc)
    ms = meta_for("memory")
    p = "行为准则: 汇报系统状态必须结论先行。"
    tmp_store.add("memory", p)
    ms.stamp(p, "rule", updated_at=now - timedelta(days=200), weight=1.0)
    m = ms.get_entry(p)
    assert _rule_rank(p, m, now) > _rule_weight_eff(m, now), \
        "回滚时 protected 恢复 ×WEIGHT_PROTECT_MULT 排序乘数"
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})
    assert p in sel, "PROTECT_SKIP_LRU=0 → protected 重新参与候选"


def test_apply_activity_hits_strong_and_cap(tmp_store, meta_for, tmp_path,
                                            monkeypatch):
    """S1 补充: apply_activity_hits 语义强命中 +1.0, 每轮封顶 1 次。"""
    from memorycore.core import config as config_mod
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    entry = _mk_meta(tmp_store, meta_for, "规则戊: Samba 配置说明。")
    meta_mod.log_activity_query("怎么在服务器上共享文件夹")
    meta_mod.log_activity_query("Samba 怎么设置")
    from memorycore.core.overflow import apply_activity_hits
    class FakeClient:
        def embed_texts(self, texts):
            return [[1.0, 0.0, 0.0]] * len(texts)
    stat = {}
    apply_activity_hits({"memory": meta_for("memory")},
                        {"memory": [entry]}, FakeClient(), stat)
    m = meta_for("memory").get_entry(entry)
    assert abs(m.get("weight") - (WEIGHT_INIT + 1.0)) < 1e-6, \
        f"两条查询同轮命中应只 +1.0 (封顶), got {m.get('weight')}"
    assert stat.get("hits_strong", 0) == 1, "同轮多条查询命中计 1 次强命中"
    assert m.get("last_strong_hit_at"), "v2: 强命中落盘 last_strong_hit_at (Q2 active 级输入)"
    assert not m.get("last_weak_hit_at"), "强命中不写弱命中戳"


# ---- SAFE-JUDGE v3 ambiguous 分级门槛 (Q-C / F-3) ---------------------------

def _stamp_ambiguous(meta_for, entry, *, days=100, review_count=0,
                     review_days=1, resolution=None, resolved_days=None):
    kw = {
        "updated_at": datetime.now(timezone.utc) - timedelta(days=days),
        "last_active_at": datetime.now(timezone.utc) - timedelta(days=days),
        "judge_decision": "ambiguous",
        "judge_band": "ambiguous",
        "judge_confidence": 0.5,
        "judge_signals": {"has_date": False},
        "judge_reason": "test",
        "judge_review_at": datetime.now(timezone.utc) + timedelta(days=review_days),
        "judge_review_count": review_count,
        "judge_policy": "v3",
    }
    if resolution:
        kw["judge_resolution"] = resolution
    if resolved_days is not None:
        kw["judge_resolved_at"] = (datetime.now(timezone.utc)
                                   - timedelta(days=resolved_days))
    meta_for("memory").stamp(entry, "rule", **kw)


def test_ambiguous_audit_prior_not_candidate_exemption(tmp_store, meta_for):
    """CACHE-POLICY-V2: ambiguous 不再有 A0 资格豁免; 低先验也进统一候选池。"""
    e = "条目标记A: 正文内容。"
    tmp_store.add("memory", e)
    _stamp_ambiguous(meta_for, e, days=10, review_count=1)
    sel = _select_retirement_candidates(tmp_store, meta_for("memory"),
                                        "memory", 10 ** 9, {})
    assert e in sel, "ambiguous 资格不再阻止预算换出"


def test_a1_ambiguous_is_stub_only_candidate(tmp_store, mock_client, meta_for):
    """A1: 达 21d/2 次审 → 只允许 stub-sink (全文先冷层 + 指针), 不全文冷迁。"""
    e = "条目标记B: " + "正文" * 120
    tmp_store.add("memory", e)
    _stamp_ambiguous(meta_for, e, days=100, review_count=2)
    # 增加不满足驻留期的 rule 填充, 把规则生态推过预算 (trigger 为 e)
    filler = "填充" + "乙" * (RULE_BUDGET_CHARS + 100)
    tmp_store.add("memory", filler)
    meta_for("memory").stamp(filler, "rule", updated_at=datetime.now(timezone.utc))
    sel = _select_retirement_candidates(tmp_store, meta_for("memory"),
                                        "memory", 10 ** 9, {})
    assert e in sel
    stat = {}
    enforce_rule_budget(tmp_store, mock_client, "memory",
                        meta_for("memory"), stat)
    ents = tmp_store.entries("memory")
    assert e not in ents, "A1 ambiguous 全文必须离开热层"
    assert any(x.startswith(STUB_PREFIX) for x in ents), "必须只留 stub 指针"
    assert e in mock_client.stored, "全文必须先写冷层确认"


def test_resolved_rule_grace_no_longer_skips_lru(tmp_store, meta_for):
    """CACHE-POLICY-V2: judge 字段只审计; R-grace 不再阻止预算换出。"""
    e = "条目标记C: 正文内容。"
    tmp_store.add("memory", e)
    _stamp_ambiguous(meta_for, e, days=100, review_count=1,
                     resolution="rule", resolved_days=1)
    sel = _select_retirement_candidates(tmp_store, meta_for("memory"),
                                        "memory", 10 ** 9, {})
    assert e in sel, "grace 字段不再是资格门; 统一活性+预算决定"


def test_ambiguous_evicted_by_budget_no_ambiguous_hold(tmp_store, mock_client,
                                                      meta_for):
    """CACHE-POLICY-V2: ambiguous 超预算走统一 stub-sink; 无 ambiguous_hold 平台。"""
    from memorycore.core.overflow import run_overflow
    e = "条目标记D: " + "甲" * 2100
    tmp_store.add("memory", e)
    _stamp_ambiguous(meta_for, e, days=10, review_count=0, review_days=7)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert e not in ents, "ambiguous 全文必须离开热层 (预算压力)"
    assert any(x.startswith(STUB_PREFIX) for x in ents), "缺页地址应保留"
    assert e in mock_client.stored, "冷层先写成功才换出"
    assert stat.get("plateau_reason") != "ambiguous_hold"
    assert stat.get("errors", 0) == 0
