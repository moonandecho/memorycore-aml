#!/usr/bin/env python3
"""tests/test_rule_stale.py — Phase 3 rule invalidation signals acceptance.

Pressure ladder semantics (baseline = measured usage):
  L0 (any):      existing logic + S3 same-topic clustering merge
  L1 (>= 60%):   S2 completion re-check (rule->state retype) + S5 cross-tier
                 redundancy dedup
  L2 (>= 80%):   S4 dormant B-class stub-sink (full text to cold tier first,
                 stub pointer left locally) + oldest-first stub GC

Tiered protection: A-class meta-rules, red-line rules and importance>=0.9
entries are never stubbed / retyped / cross-tier dedup'd (merge/compress only).
All tests use the isolated conftest fixtures (tmp dirs + mock cold tier).
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import overflow as ov
from memorycore.core import metadata as meta_mod
from memorycore.core.config import MAX_STUB_PER_RUN, STUB_MAX_CHARS, STUB_PREFIX
from memorycore.core.metadata import MetaStore
from memorycore.core.overflow import run_overflow
from conftest import MockMnemosyneClient, days_ago_str


# ---- helpers ----------------------------------------------------------------

def _fill_to(tmp_store, target, pct):
    """Pad with one big rule entry to push usage past the target percentage."""
    limit = 5000
    want = int(limit * pct) + 30
    need = max(0, want - tmp_store.char_count(target))
    if need:
        tmp_store.add(target, "填充条目" + "甲" * need)


def _stamp_rule(meta_for, entry, days, importance=0.8, target="memory"):
    meta_for(target).stamp(entry, "rule",
                           updated_at=datetime.now(timezone.utc) - timedelta(days=days),
                           importance=importance)


def _stamp_stub(meta_for, entry, days, target="memory"):
    meta_for(target).stamp(entry, "stub",
                           updated_at=datetime.now(timezone.utc) - timedelta(days=days),
                           origin="stub_sink")


def _setup_activity(monkeypatch, tmp_path, queries):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    for q in queries:
        meta_mod.log_activity_query(q)


def _judge_all_dormant(monkeypatch):
    monkeypatch.setattr(ov, "_llm_judge_dormant",
                        lambda entries, queries: {e: True for e in entries})


# ---- L0: low usage leaves rules untouched, zero cold calls ------------------

def test_l0_low_usage_rule_untouched_no_cold_calls(tmp_store, mock_client):
    entry = f"用户偏好({days_ago_str(100)}): 极简选型, Go/Rust 单二进制, 拒绝重依赖"
    tmp_store.add("memory", entry)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["aged_sunk"] == 0 and stat["stubbed"] == 0
    assert mock_client.stored == []
    assert mock_client.recall_queries == [], "low usage must not trigger S5 probes"


# ---- S3: same-topic merge (lexical + optional embedding channel) ------------

def test_s3_same_topic_merge_preserves_info(tmp_store, mock_client):
    base = "汇报系统状态必须结论先行, 判断标准是卡不卡崩不崩。"
    a = base + "给 available/swap 而非 used。"
    b = base + "用大白话分层结论, 不要术语堆砌。"
    tmp_store.add("memory", a)
    tmp_store.add("memory", b)
    _fill_to(tmp_store, "memory", 0.62)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert stat["merged"] >= 1, stat
    merged = [e for e in ents if "结论先行" in e]
    assert len(merged) == 1, "same-topic pair must merge into one entry"
    assert "available/swap" in merged[0] and "大白话分层结论" in merged[0], \
        "unique information must survive the merge"


def test_s3_embedding_channel_merges_lexical_distant(tmp_store, mock_client,
                                                     monkeypatch):
    """Embedding channel (mocked here; degrades to lexical when unavailable)
    finds same-topic pairs that lexical similarity misses."""
    a = "服务器内存参数记录: zram swap 8GB swappiness 60。"
    b = "服务器内存配置说明: 交换分区与压缩调优结论。"
    tmp_store.add("memory", a)
    tmp_store.add("memory", b)
    monkeypatch.setattr(ov, "_embed_batch",
                        lambda texts: {t: [1.0, 0.0] for t in texts})
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert stat["merged"] >= 1, stat
    merged = [e for e in ents if "zram" in e]
    assert len(merged) == 1 and "调优结论" in merged[0]


# ---- S2: completion re-check (rule -> state retype) -------------------------

def test_s2_retype_sinks_old_completed_rule(tmp_store, mock_client, meta_for):
    entry = f"{days_ago_str(65)} 安全审计已修两处漏洞, 已禁 rpcbind 服务, 归档完毕"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry not in tmp_store.entries("memory"), "retyped state must TTL-sink"
    assert stat["aged_sunk"] >= 1 and stat["retyped"] >= 1, stat
    assert entry in mock_client.stored, "cold tier must confirm the write first"
    m = meta_for("memory").get_entry(entry)
    assert m and m["type"] == "state" and m["origin"] == "retype_overflow"


@pytest.mark.parametrize("entry", [
    f"{days_ago_str(65)} 安全审计已修两处漏洞, 归档完毕",          # only 1 marker
    f"{days_ago_str(65)} 用户偏好: 已修两处漏洞, 已禁 rpcbind",    # behavior word
    f"{days_ago_str(20)} 安全审计已修漏洞, 已禁服务",              # date < 60d
])
def test_s2_retype_negative(tmp_store, mock_client, meta_for, entry):
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["retyped"] == 0
    assert meta_for("memory").get_entry(entry)["type"] == "rule"


def test_s2_retype_cold_fail_restores_rule(tmp_store, meta_for):
    """Cold failure -> entry stays + original rule stamp restored + errors>=1."""
    entry = f"{days_ago_str(65)} 安全审计已修两处漏洞, 已禁 rpcbind 服务"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    assert entry in tmp_store.entries("memory"), "cold failure must keep source"
    assert stat["errors"] >= 1 and stat["retyped"] == 0
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "rule" and m["origin"] == "legacy", "rule stamp restored"


# ---- S4: stub-sink ----------------------------------------------------------

def test_s4_stub_sink_dormant_b_rule(tmp_store, mock_client, meta_for,
                                     tmp_path, monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["帮我写一个 Python 脚本处理 Excel", "今天天气怎么样"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    stubs = [e for e in ents if e.startswith(STUB_PREFIX)]
    assert len(stubs) == 1, "entry must be replaced by a stub pointer"
    assert entry not in ents, "full text must leave the hot tier"
    assert len(stubs[0]) <= STUB_MAX_CHARS
    assert stat["stubbed"] == 1, stat
    assert entry in mock_client.stored, "full text must reach the cold tier first"
    m = meta_for("memory").get_entry(stubs[0])
    assert m and m["type"] == "stub" and m["origin"] == "stub_sink"


def test_s4_stub_cold_fail_keeps_original(tmp_store, meta_for, tmp_path,
                                          monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    assert entry in tmp_store.entries("memory"), "cold failure keeps original"
    assert stat["errors"] >= 1 and stat["stubbed"] == 0


# ---- S4 protection: A-class / red-line / importance -------------------------

@pytest.mark.parametrize("entry", [
    "行为准则: 汇报系统状态必须结论先行, 给 available/swap 而非 used。",
    "红线: 删除强制走回收站, 绝不能用 rm。",
])
def test_s4_protected_not_stubbed(tmp_store, mock_client, meta_for,
                                  tmp_path, monkeypatch, entry):
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=100)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert entry in ents, "A-class / red-line rules are never stubbed"
    assert stat["stubbed"] == 0 and stat["retyped"] == 0
    assert not any(e.startswith(STUB_PREFIX) for e in ents)


def test_s4_importance_protected_not_stubbed(tmp_store, mock_client, meta_for,
                                             tmp_path, monkeypatch):
    entry = "项目偏好: 团队沟通一律用邮件。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=100, importance=0.95)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["stubbed"] == 0


# ---- S4 degradation: log disabled / LLM failure / lexically active ----------

def test_s4_disabled_when_log_disabled(tmp_store, mock_client, meta_for,
                                       tmp_path, monkeypatch):
    monkeypatch.setattr(ov, "ACTIVITY_LOG_ENABLED", False)
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _judge_all_dormant(monkeypatch)  # even a dormant verdict must not fire
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["stubbed"] == 0


def test_s4_llm_fail_no_stub(tmp_store, mock_client, meta_for, tmp_path,
                             monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    monkeypatch.setattr(ov, "_llm_judge_dormant",
                        lambda entries, queries: {})  # failure -> all active
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["stubbed"] == 0


def test_s4_lexical_active_no_llm_call(tmp_store, mock_client, meta_for,
                                       tmp_path, monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["自托管选型偏好是什么"])
    called = []
    monkeypatch.setattr(
        ov, "_llm_judge_dormant",
        lambda entries, queries: called.append(1) or {e: True for e in entries})
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["stubbed"] == 0
    assert called == [], "lexically active entries skip the LLM judge entirely"


# ---- S5: cross-tier redundancy cleanup --------------------------------------

def test_s5_cross_layer_dedup_removes_hot_copy(tmp_store, meta_for):
    entry = "服务器事实: /tmp 是 tmpfs 重启即清空。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=40)
    _fill_to(tmp_store, "memory", 0.62)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    assert entry not in tmp_store.entries("memory"), "cold copy exists -> drop hot"
    assert stat["overflowed"] >= 1, stat
    assert cold.stored == [], "cold already has it -> no duplicate write"


def test_s5_skips_recent_rule(tmp_store, meta_for):
    """Idle < 30d never probes the cold tier (historical-redundancy oriented,
    keeps overflow cheap)."""
    entry = "服务器事实: /tmp 是 tmpfs 重启即清空。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=5)
    _fill_to(tmp_store, "memory", 0.62)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    assert entry in tmp_store.entries("memory")
    assert cold.recall_queries == [], "recent rules skip the cold probe"


# ---- ladder greedy stop -----------------------------------------------------

def test_l2_greedy_stops_below_hard(tmp_store, mock_client, meta_for,
                                    tmp_path, monkeypatch):
    e1 = ("自托管选型偏好: 极轻极简单二进制, 几十MB一行部署。"
          + "部署在服务器上的应用保持单二进制形态, 一行命令启动和维护, "
            "不引入额外依赖与守护进程。" * 3)
    e2 = ("购买偏好: 不追新只买需要的, 按需求短板升级。"
          + "只有实际遇到瓶颈时才考虑采购, 不为降价或囤货消费, "
            "硬件预算投向内存与硬盘。" * 3)
    e3 = ("硬件选型偏好: 中端芯片加够用内存就是黄金档。"
          + "不打游戏不跑本地大模型, 轻量模型跑轻量设备, "
            "旗舰性能属于浪费, 散热噪音也要考量。" * 3)
    for e in (e1, e2, e3):
        tmp_store.add("memory", e)
        _stamp_rule(meta_for, e, days=50)
    _fill_to(tmp_store, "memory", 0.81)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert 1 <= stat["stubbed"] <= MAX_STUB_PER_RUN, stat
    assert tmp_store.usage_pct("memory") < 80, "stubbing stops below the hard line"
    ents = tmp_store.entries("memory")
    unstubbed = [e for e in (e1, e2, e3) if e in ents]
    assert len(unstubbed) >= 1, "remaining candidates stay below the hard line"


# ---- stub GC: oldest first, zero cold calls ---------------------------------

def test_stub_gc_removes_oldest_pointers(tmp_store, mock_client, meta_for):
    topics = ["自托管选型", "服务器运维", "写作风格", "安全审计", "硬件采购"]
    stubs = []
    for i, kw in enumerate(topics):
        s = f"{STUB_PREFIX}{kw}→recall(\"{kw}\")"
        tmp_store.add("memory", s)
        _stamp_stub(meta_for, s, days=10 + i)  # ages 10..14 days
        stubs.append(s)
    _fill_to(tmp_store, "memory", 0.82)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert 1 <= stat["stub_gc"] <= MAX_STUB_PER_RUN, stat
    assert stubs[4] not in ents, "oldest pointer collected first"
    assert stubs[0] in ents, "youngest pointer kept"
    assert mock_client.forgotten == [], "stub GC is pointer-only, cold tier untouched"
    assert mock_client.stored == []


# ---- stub residency (low usage: no GC, no compress) --------------------------

def test_stub_kept_at_low_usage(tmp_store, mock_client, meta_for):
    s = f"{STUB_PREFIX}主题X→recall(\"主题X\")"
    tmp_store.add("memory", s)
    _stamp_stub(meta_for, s, days=30)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert s in tmp_store.entries("memory")
    assert stat["stub_gc"] == 0 and stat["stubbed"] == 0
    assert mock_client.stored == []


# ---- safety path + idempotency ----------------------------------------------

def test_rule_ladder_cold_fail_keeps_all(tmp_store, meta_for):
    """S2+S4 chain: any cold failure keeps the entry + errors>=1."""
    entry = f"{days_ago_str(65)} 安全审计已修两处漏洞, 已禁 rpcbind 服务"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["errors"] >= 1


def test_idempotent_second_run_no_side_effects(tmp_store, mock_client, meta_for,
                                               tmp_path, monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat1 = run_overflow(tmp_store, mock_client, "memory")
    assert stat1["stubbed"] == 1
    stat2 = run_overflow(tmp_store, mock_client, "memory")
    assert stat2["stubbed"] == 0 and stat2["stub_gc"] == 0, "second run: no side effects"


# ---- activity log (metadata unit) -------------------------------------------

def test_activity_log_roundtrip_and_truncate(tmp_path, monkeypatch):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    meta_mod.log_activity_query("  测试查询内容  ")
    qs = meta_mod.load_recent_queries(days=1)
    assert any("测试查询内容" in q for q in qs)
    meta_mod.log_activity_query("长" * 300)
    qs = meta_mod.load_recent_queries(days=1)
    assert max(len(q) for q in qs) <= 200, "log truncates to 200 chars"
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", False)
    meta_mod.log_activity_query("不应写入")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    qs = meta_mod.load_recent_queries(days=1)
    assert all("不应写入" not in q for q in qs), "disabled logging must not write"


def test_activity_log_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "act.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_MAX_BYTES", 4096)
    for i in range(300):
        meta_mod.log_activity_query(f"查询{i} " + "x" * 40)
    assert os.path.getsize(tmp_path / "act.jsonl") <= 4096, "rolling cap enforced"


def test_prefetch_logs_activity(tmp_path, monkeypatch):
    """Plugin smoke: prefetch writes the activity log every round."""
    import importlib.util
    plugin_path = (Path(__file__).resolve().parent.parent
                   / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")
    spec = importlib.util.spec_from_file_location("memorycore_prefetch_log_smoke",
                                                  plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "act.jsonl")
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_ENABLED", True)
    mod.ColdStoreClient = lambda **k: MockMnemosyneClient()
    provider = mod.MemoryCorePrefetchProvider()
    provider._recall_sync("自托管选型调查")
    qs = meta_mod.load_recent_queries(days=1)
    assert any("自托管选型调查" in q for q in qs)


# ---- server: store_fact importance passthrough + audit stub display ----------

def test_store_fact_stamps_importance(tmp_store, mock_client, meta_for):
    from memorycore import server
    server._store = tmp_store
    server._client = mock_client
    r = json.loads(server.memorycore_store_fact("用户偏好: 极简选型", importance=0.95))
    assert r["status"] == "stored", r
    m = meta_for("memory").get_entry("用户偏好: 极简选型")
    assert m and m["importance"] == 0.95


def test_audit_shows_stub_plan(tmp_store, mock_client):
    from memorycore import server
    server._store = tmp_store
    server._client = mock_client
    s = f"{STUB_PREFIX}主题Y→recall(\"主题Y\")"
    tmp_store.add("memory", s)
    MetaStore("memory", memory_path=tmp_store.memory_path,
              user_path=tmp_store.user_path).stamp(s, "stub", origin="stub_sink")
    r = json.loads(server.memorycore_memory_audit("memory"))
    row = r["memory"]["rows"][0]
    assert row["type"] == "stub"
    assert "stub" in row["plan"]
    assert row["keep"] is True
