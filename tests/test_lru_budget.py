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


def test_budget_all_in_residency_no_evict(tmp_store, mock_client, meta_for):
    """② 全驻留期内 → 挤 0 条。"""
    e1 = _add_rules(tmp_store, meta_for, 1, chars_each=250, days=0)[0]
    tmp_store.add("memory", "填充甲" + "乙" * (RULE_BUDGET_CHARS))
    meta_for("memory").stamp(tmp_store.entries("memory")[-1], "rule",
                             updated_at=datetime.now(timezone.utc))
    stat = {}
    enforce_rule_budget(tmp_store, mock_client, "memory", meta_for("memory"), stat)
    assert e1 in tmp_store.entries("memory"), "驻留期内不挤"
    assert stat.get("lru_evicted", 0) == 0


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
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
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
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
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
    """S1 回归: 候选筛选纯词法 (sb≥2), 无 LLM key 时非活跃规则可被选中。"""
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    from memorycore.core.overflow import _select_retirement_candidates
    # 词法活跃的规则 (7 天内有相关查询) → 不挤
    e_active = _mk_meta(tmp_store, meta_for, "规则丙: 打印机型号与耗材。")
    meta_mod.log_activity_query("打印机怎么设置双面打印")
    # 非活跃规则 (7 天无相关查询) → 可挤
    e_idle = _mk_meta(tmp_store, meta_for, "规则丁: 远古技术方案记录。")
    sel = _select_retirement_candidates(tmp_store, meta_for("memory"), "memory", 1, {})
    assert e_active not in sel, "词法活跃规则不应被挤"
    assert e_idle in sel, "非活跃规则应可被挤 (无 LLM 依赖)"


def test_apply_activity_hits_strong_and_cap(tmp_store, meta_for, tmp_path,
                                            monkeypatch):
    """S1 补充: apply_activity_hits 语义强命中 +1.0, 每轮封顶 1 次。"""
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
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
