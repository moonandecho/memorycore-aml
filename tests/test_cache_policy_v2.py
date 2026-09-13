#!/usr/bin/env python3
"""tests/test_cache_policy_v2.py — CACHE-POLICY-V2 验收 (2026-09-13)。

覆盖:
  ① 无永久驻留: 全 protected + 红线/importance/protect_override 也必可换出;
  ② stub 继承活性/判型字段, 指针不再被 stamp 成刚活跃;
  ③ rule/stub/state 统一候选池 + protected ×3 排序;
  ④ 冷层不可达 cold_backstop: 不换出/不删本地/errors 可见;
  ⑤ MEMORYCORE_CACHE_POLICY_V2=0 旧资格语义回滚;
  ⑥ plugin: 常驻目录 ≤800、句柄行 ≤20、动作工具 schema、0.449/K 通道。
"""
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    _handle_rule_stub_sink, _rule_rank, _select_retirement_candidates,
    enforce_rule_budget,
)
from conftest import MockMnemosyneClient  # noqa: E402


def _cold_has_text(client, text):
    if text in getattr(client, "stored", []):
        return True
    for _mid, merged in getattr(client, "updated", []):
        if text in merged:
            return True
    return False


def _stamp_protected(ms, entry, *, weight=0.01, days=500):
    # FIX5: 软驻留认 written_at; 夹具的 days=500 旧条目需同时盖 written_at,
    # 否则默认 now 会被误当新写入 (用例意图是旧 protected 必可换出)。
    anchor = datetime.now(timezone.utc) - timedelta(days=days)
    ms.stamp(entry, "rule", weight=weight,
             importance=0.95,
             protect_override=True,
             written_at=anchor,
             last_active_at=anchor,
             updated_at=anchor)


def test_no_permanent_residency_all_protected(tmp_store, meta_for, monkeypatch):
    """全 protected (A 类/红线/importance=0.9/protect_override) 预算压到 0 全可 stub。"""
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", 0)
    ms = meta_for("memory")
    client = MockMnemosyneClient()
    # 只验证冷写铁律; 禁用查重合并, 保证每个原文都被真实 remember。
    client.recall_results = lambda q, top_k=5, bump=True: []
    originals = []
    for i in range(6):
        e = (f"红线规则{i}: 行为准则 必须 绝不 禁止 零容忍, 用户明确要求, "
             + "内容甲" * 16)
        tmp_store.add("memory", e)
        _stamp_protected(ms, e)
        originals.append(e)
    stat = {}
    for _ in range(20):
        enforce_rule_budget(tmp_store, client, "memory", ms, stat)
        full_left = [e for e in tmp_store.entries("memory")
                     if (ms.get_entry(e) or {}).get("type") == "rule"]
        if not full_left:
            break
    ents = tmp_store.entries("memory")
    # F2 修复后语义: 无永久驻留 = 无全文 rule 常驻; ≤40 字 stub 指针
    # 是常驻页表 (指针总量 6×~33≤2000 时必须全部保留, 不得 GC)。
    assert not [e for e in ents
                if (ms.get_entry(e) or {}).get("type") == "rule"], \
        "全 protected 场景不得出现全文永久驻留"
    stubs = [e for e in ents
             if (ms.get_entry(e) or {}).get("type") == "stub"]
    assert len(stubs) == len(originals), (
        f"指针总量≤2000 必须全部保留: {len(stubs)}/{len(originals)}")
    for e in originals:
        assert e not in ents, "原全文必须离开热层 (stub/T3)"
        assert e in client.stored, "换出前必须先冷写成功"
    for st in stubs:
        assert (ms.get_entry(st) or {}).get("cold_id"), \
            "每个 stub 必须有 cold_id 精确映射"
    assert stat.get("errors", 0) == 0, stat
    assert stat.get("protected_evicted", 0) == len(originals), stat
    assert stat.get("stub_gc", 0) == 0, \
        "全文预算为 0 时也不得用删指针来保全文/满足内容预算"


def test_stub_inherits_activity_and_judge_fields(tmp_store, meta_for):
    """stub 不是新条目: 继承 weight/锚点/judge/importance/type_override。"""
    ms = meta_for("memory")
    e = "用户明确要求: 交付完成后必须通知我。"
    tmp_store.add("memory", e)
    old = datetime.now(timezone.utc) - timedelta(days=45)
    strong = datetime.now(timezone.utc) - timedelta(days=3)
    ms.stamp(e, "rule", weight=3.25, importance=0.95,
             last_active_at=old, updated_at=old,
             last_strong_hit_at=strong,
             judge_decision="rule", judge_band="strong",
             judge_confidence=0.91, judge_signals={"x": 1},
             judge_reason="test", judge_policy="v3",
             type_override="state", protect_override=True)
    stat = {}
    _handle_rule_stub_sink(tmp_store, MockMnemosyneClient(), "memory", e,
                           ms, stat, [])
    stub = ov._make_stub(e)
    assert stub in tmp_store.entries("memory")
    sm = ms.get_entry(stub)
    assert sm is not None and sm.get("type") == "stub"
    assert sm.get("weight") == 3.25
    assert sm.get("last_active_at") == old.isoformat()
    assert sm.get("last_strong_hit_at") == strong.isoformat()
    assert sm.get("importance") == 0.95
    assert sm.get("judge_decision") == "rule"
    assert sm.get("type_override") == "state"
    assert sm.get("protect_override") is True
    assert sm.get("cold_id")
    assert sm.get("retire_count") == 1
    assert sm.get("last_evicted_at")
    assert stat.get("stubbed") == 1 and stat.get("errors", 0) == 0


def test_rule_stub_state_unified_pool_and_protected_mult(tmp_store, meta_for):
    """rule/stub/state 同池; protected 只乘 ×3, 无资格豁免。"""
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    old = now - timedelta(days=100)
    rule = "普通规则: 技术方案选型记录。"
    state = "2026-08-01 已停: 历史状态记录。"
    stub = "[规则指针]主题词→recall(\"主题词\")"
    for e, typ in ((rule, "rule"), (state, "state"), (stub, "stub")):
        tmp_store.add("memory", e)
        ms.stamp(e, typ, weight=1.0, last_active_at=old)
    protected = "红线: 绝不向用户隐藏事实。"
    tmp_store.add("memory", protected)
    ms.stamp(protected, "rule", weight=1.0, last_active_at=old)
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})
    assert rule in sel and state in sel and stub in sel, "三类必须同池参选"
    plain_m = ms.get_entry(rule)
    prot_m = ms.get_entry(protected)
    plain_rank = _rule_rank(rule, plain_m, now)
    prot_rank = _rule_rank(protected, prot_m, now)
    assert abs(prot_rank - plain_rank * config_mod.WEIGHT_PROTECT_MULT) < 1e-9


def test_cold_backstop_keeps_local(tmp_store, meta_for, monkeypatch):
    """冷层不可达: 统一换出零删除、errors/cold_backstop 可见。"""
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", 0)
    ms = meta_for("memory")
    e = "普通规则: " + "内容" * 80
    tmp_store.add("memory", e)
    ms.stamp(e, "rule", weight=0.5,
             updated_at=datetime.now(timezone.utc) - timedelta(days=100))
    bad = MockMnemosyneClient(fail_remember=True)
    stat = {}
    enforce_rule_budget(tmp_store, bad, "memory", ms, stat)
    assert e in tmp_store.entries("memory"), "冷层失败绝不能动本地"
    assert stat.get("errors", 0) >= 1
    assert stat.get("cold_errors", 0) >= 1
    assert stat.get("lru_evicted", 0) == 0
    stat2 = ov.run_overflow(tmp_store, bad, "memory")
    assert stat2.get("plateau_reason") == "cold_backstop", stat2
    assert e in tmp_store.entries("memory")


def test_cache_policy_v2_rollback_selects_legacy(tmp_store, meta_for,
                                                 monkeypatch):
    """MEMORYCORE_CACHE_POLICY_V2=0 → protected skip + 旧资格路径可回滚。"""
    monkeypatch.setattr(config_mod, "CACHE_POLICY_V2", False)
    monkeypatch.setattr(config_mod, "PROTECT_SKIP_LRU", True)
    assert ov._cache_policy_v2() is False
    ms = meta_for("memory")
    p = "行为准则: 汇报结论先行。"
    tmp_store.add("memory", p)
    ms.stamp(p, "rule", weight=0.001,
             updated_at=datetime.now(timezone.utc) - timedelta(days=999))
    sel = _select_retirement_candidates(tmp_store, ms, "memory", 10 ** 6, {})
    assert p not in sel, "回滚路径恢复 protected 资格豁免"




def test_old_sidecar_reads_without_rejudge(tmp_store, meta_for):
    """旧 sidecar 缺新字段: 读取默认值, reconcile 不重判。"""
    import hashlib
    e = "旧规则: 历史写入的准则。"
    tmp_store.add("memory", e)
    ms = meta_for("memory")
    old_meta = {
        "type": "rule", "written_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00", "origin": "legacy",
        "importance": 0.8, "weight": 1.0,
        "last_active_at": "2026-01-01T00:00:00+00:00",
    }
    ms.meta_path.write_text(json.dumps({hashlib.sha256(
        e.strip().encode()).hexdigest(): old_meta}), encoding="utf-8")
    rec = ms.reconcile([e])
    assert rec["stamped"] == 0 and rec["gc"] == 0, rec
    loaded = ms.get_entry(e)
    assert loaded.get("type") == "rule" and "last_recall_hit_at" not in loaded
    assert loaded.get("weight") == 1.0
    assert loaded.get("last_active_at") == "2026-01-01T00:00:00+00:00"


def test_stub_total_budget_gc(tmp_store, meta_for):
    """stub 合计超规则预算 → 统一池按 LRU 回收指针 (≤2000)。"""
    ms = meta_for("memory")
    old = datetime.now(timezone.utc) - timedelta(days=30)
    for i in range(90):
        stub = f"[规则指针]主题{i:03d}→recall(\"主题{i:03d}\")"
        tmp_store.add("memory", stub)
        ms.stamp(stub, "stub", origin="stub_sink", cold_id=f"c{i}",
                 updated_at=old, last_active_at=old)
    stat = {}
    for _ in range(15):
        enforce_rule_budget(tmp_store, MockMnemosyneClient(), "memory", ms, stat)
        stub_chars = sum(len(e) for e in tmp_store.entries("memory")
                         if (ms.get_entry(e) or {}).get("type") == "stub")
        if stub_chars <= config_mod.RULE_BUDGET_CHARS:
            break
    stub_chars = sum(len(e) for e in tmp_store.entries("memory")
                     if (ms.get_entry(e) or {}).get("type") == "stub")
    assert stub_chars <= config_mod.RULE_BUDGET_CHARS, stub_chars
    assert stat.get("errors", 0) == 0, stat

# ---- plugin 烟测 -------------------------------------------------------------

def _load_plugin():
    # FIX4 P3: 仓库内相对路径, 不依赖 Hermes 安装目录 symlink。
    from conftest import PLUGIN_PATH as plugin_path  # release layout: hermes-plugin/memorycore-prefetch
    spec = importlib.util.spec_from_file_location("memorycore_prefetch_v2",
                                                  plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _patch_plugin_store(mod, tmp_store, meta_for):
    mod.LocalStore = lambda *a, **k: tmp_store

    def _meta(target, **kwargs):
        return meta_for(target)
    mod.MetaStore = _meta


def test_plugin_action_tool_schema_and_rollback(tmp_store, meta_for,
                                                monkeypatch):
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    provider = mod.MemoryCorePrefetchProvider()
    monkeypatch.setenv("MEMORYCORE_ACTION_RECALL", "1")
    schemas = provider.get_tool_schemas()
    assert [s["name"] for s in schemas] == ["memorycore_action_recall"]
    monkeypatch.setenv("MEMORYCORE_ACTION_RECALL", "0")
    assert provider.get_tool_schemas() == []
    called = []
    provider._recall_sync = lambda *a, **k: called.append((a, k)) or "CTX"
    provider.on_turn_start(1, "准备发布", tool_count=1)
    assert called == [], "ACTION_RECALL=0 时 on_turn_start 不得召回"


def test_plugin_directory_budget_and_line_limit(tmp_store, meta_for,
                                                monkeypatch):
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    provider = mod.MemoryCorePrefetchProvider()
    now = datetime.now(timezone.utc)
    for i in range(45):
        stub = f'[规则指针]主题{i:02d}→recall("主题{i:02d}")'
        tmp_store.add("memory", stub)
        meta_for("memory").stamp(
            stub, "stub", origin="stub_sink", cold_id=f"c{i}",
            handle=f"#h{i:07x}", last_active_at=now - timedelta(days=i))
    text = provider._format_directory()
    assert text
    assert len(text) <= config_mod.INDEX_BUDGET_CHARS, len(text)
    for line in text.splitlines()[1:]:
        assert len(line) <= 20, line
    assert text.count("[#") == len(text.splitlines()) - 1


def test_plugin_recall_keeps_0449_candidate(tmp_store, meta_for, monkeypatch):
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    provider = mod.MemoryCorePrefetchProvider()

    class FakeClient:
        def __init__(self, *a, **k):
            self.recall_queries = []
            self.recall_bumps = []

        def recall_results(self, query, top_k=5, bump=True):
            self.recall_queries.append(query)
            self.recall_bumps.append(bump)
            return [
                {"id": "c-1", "content": "交付完成后必须通知我 行为规则",
                 "dense_score": 0.449, "keyword_score": 0.0, "fts_score": 0.0},
                {"id": "c-2", "content": "今晚吃什么",
                 "dense_score": 0.90, "keyword_score": 0.0, "fts_score": 0.0},
            ]

    fake = FakeClient()
    mod.ColdStoreClient = lambda *a, **k: fake
    provider._hot_norm = ""
    provider._injected_ids = set()
    out = provider._recall_sync("交付完成后必须通知我 行为规则")
    assert "交付完成后必须通知我" in out, "0.449 边缘命中必须保留在候选/注入"
    assert "交付完成后必须通知我 行为规则" in fake.recall_queries
    assert fake.recall_bumps and all(b is False for b in fake.recall_bumps), \
        "冷层召回必须只读 bump=False"


def test_plugin_action_hooks_and_sync_turn(tmp_store, meta_for, monkeypatch):
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    provider = mod.MemoryCorePrefetchProvider()
    provider._recall_sync = lambda q, action_trigger=False: (
        f"CTX:{q}:{action_trigger}")
    provider.sync_turn("把交付通知我", "准备发布并通知用户")
    assert provider._last_user_intent == "把交付通知我"
    provider.on_turn_start(1, "准备发布", tool_count=1)
    # F4: 动作触发/合成 query 只来自最近用户意图, assistant 文本不得触发。
    assert "准备发布" not in provider._pending_action_query
    assert provider._last_user_intent in provider._pending_action_query
    assert provider._last_action_context.startswith("CTX:")
    result = json.loads(provider.handle_tool_call("memorycore_action_recall",
                                                  {"intent": "删除文件"}))
    assert result["intent"] == "删除文件"
    assert result["context"].startswith("CTX:")
    assert result["read_only_cold"] is True
    monkeypatch.setenv("MEMORYCORE_ACTION_RECALL", "0")
    with pytest.raises(NotImplementedError):
        provider.handle_tool_call("memorycore_action_recall", {"intent": "x"})
