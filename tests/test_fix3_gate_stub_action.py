#!/usr/bin/env python3
"""FIX3 F1/F2/F3/F4 回归测试 (2026-09-13)。

覆盖独立复核:
  F1 低信息/噪声闸门: 15 条"不该注入"查询逐条 0 注入;
  F2 指针 GC 优先级: stub 不先于全文被删, 新建 stub 年龄保护,
      protected 标记继承, 指针总量<=预算时全保留;
  F3 stub 文本碰撞: 25 条快照 25 唯一, 写回按 cold_id 精确映射;
  F4 动作触发面: assistant 文本不得触发, 弱词不触发,
     纯 S+action 只注入不写回, 低信息 H 通道不写回。
"""
import importlib.util
import os
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    STUB_PREFIX, _handle_rule_stub_sink, _make_stub,
    _select_retirement_candidates, _stub_gc, enforce_rule_budget,
    restore_stubs_from_results,
)
from conftest import MockMnemosyneClient  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402

# FIX4 P3: 仓库内相对路径, 不依赖 Hermes 安装目录 symlink。
def _resolve_plugin_path() -> Path:
    """Release layout: hermes-plugin/memorycore-prefetch/__init__.py
    (override via MEMORYCORE_PLUGIN_PATH; no ~/.hermes dependency)."""
    env_path = os.environ.get("MEMORYCORE_PLUGIN_PATH")
    if env_path:
        return Path(env_path)
    return ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py"


PLUGIN_PATH = _resolve_plugin_path()


def _load_plugin():
    spec = importlib.util.spec_from_file_location("fix3_plugin", PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRecall:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.queries = []

    def recall_results(self, query, top_k=5, bump=True):
        self.queries.append((query, top_k, bump))
        return [dict(r) for r in self.results]


def _patch_plugin_store(mod, tmp_store, meta_for):
    mod.LocalStore = lambda *a, **k: tmp_store

    def _meta(target, **kwargs):
        return meta_for(target)
    mod.MetaStore = _meta


F1_NO_INJECT_CASES = [
    ("闲聊", "今天天气真不错，晚上吃什么好呢"),
    ("确认", "好的，收到，明白了"),
    ("确认2", "嗯嗯，继续"),
    ("低信息", "在吗"),
    ("低信息2", "谢谢"),
    ("无关技术", "Python 的 GIL 是什么"),
    ("无关技术2", "怎么把 Excel 转成 CSV"),
    ("系统前缀噪声",
     "[IMPORTANT: You have 1 unread message from system] 请继续"),
    ("系统前缀噪声2", "[ASYNC DELEGATION] background task done"),
    ("闲聊带动作词1", "帮我写一首关于春天的诗"),
    ("闲聊带动作词2", "你刚才回复得不错"),
    ("闲聊带动作词3", "我想更新一下头像"),
    ("闲聊带动作词4", "周末打算去爬山，顺便拍点照片"),
    ("确认带动作词", "好的，那就按你说的写吧"),
    ("无关技术带动作词", "怎么安装 Python 包"),
]


def test_f1_15_low_info_noise_queries_zero_injection(tmp_store, meta_for,
                                                     monkeypatch):
    """F1 验收: 15 条查询逐条 0 全文注入 (仅目录)。"""
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    provider = mod.MemoryCorePrefetchProvider()
    fake = _FakeRecall([{
        "id": "cA",
        "content": "冷层记忆内容：用户要求每次交付后必须通知。",
        "dense_score": 0.95,
        "keyword_score": 0,
        "fts_score": 0,
    }])
    mod.ColdStoreClient = lambda *a, **k: fake
    rows = []
    for tag, q in F1_NO_INJECT_CASES:
        assert provider._is_low_information_query(q), (tag, q)
        provider._injected_ids = set()
        provider._hot_norm = ""
        rows.append({
            "tag": tag,
            "query": q,
            "gate": True,
            "injected": "冷层记忆内容" in provider.prefetch(q),
        })
    assert all(not r["injected"] for r in rows), rows
    # 低信息闸门在直调 _recall_sync 时同样生效 (目录仍由 system_prompt_block 提供)
    for _tag, q in F1_NO_INJECT_CASES:
        assert provider._recall_sync(q) == ""


def test_f1_noise_prefixes_direct_and_case_variants(tmp_store, meta_for):
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    provider = mod.MemoryCorePrefetchProvider()
    for q in ["[IMPORTANT: x", "[ASYNC DELEGATION] x",
              "[BACKGROUND] x", "[system] x", "[System] 请继续"]:
        assert provider._is_low_information_query(q), q


def _collision_client():
    class C:
        def __init__(self):
            self.contents = []

        def recall_results(self, query, top_k=5, bump=True):
            return []

        def remember(self, content, importance=0.6, scope="global"):
            self.contents.append(content)
            return {"status": "stored", "memory_id": f"cold-{len(self.contents)}"}

        def update(self, memory_id, content, importance=None):
            return {"status": "updated"}

        def forget(self, memory_id):
            return {"status": "ok"}
    return C()


def test_f3_25_snapshot_stubs_unique(tmp_path):
    """F3 回归: 合成快照 25 条规则 → 25 个唯一 stub。"""
    import re

    rules_path = ROOT / "tests/fixtures/snapshot_20260912/MEMORY.md"
    rules = [x.strip() for x in rules_path.read_text(
        encoding="utf-8").split("\n§\n") if x.strip()]
    stubs = [_make_stub(r) for r in rules]
    assert len(rules) == 25
    assert len(set(stubs)) == 25, [
        s for s in stubs if stubs.count(s) > 1]
    assert all(len(s) <= ov.STUB_MAX_CHARS for s in stubs)
    # 合成快照刻意保留同前缀规则, 用内容指纹保证 stub 不碰撞。
    def _topic(rule: str) -> str:
        return re.sub(r"\s+", "", rule.split("。")[0][:10])

    topics = [_topic(r) for r in rules]
    colliding = {t for t in set(topics) if topics.count(t) > 1}
    assert colliding, "合成快照应保留至少一组同前缀规则"
    for topic in sorted(colliding):
        coll = [r for r in rules if _topic(r) == topic]
        assert len(coll) >= 2
        assert len({_make_stub(r) for r in coll}) == len(coll)


def test_f3_cold_id_writeback_mapping_exact(tmp_store, meta_for):
    """F3: 前 10 字碰撞的两条规则 → 写回按 cold_id 精确映射, 不串条目。"""
    ms = meta_for("memory")
    e1 = ("MemoryCore 实现范围边界: 只改 memorycore 与 plugin 目录, "
          "内容甲" * 8)
    e2 = ("MemoryCore 实现范围边界: 另一条同前缀规则, 内容乙" * 8)
    tmp_store.add("memory", e1)
    tmp_store.add("memory", e2)
    for e in (e1, e2):
        ms.stamp(e, "rule", weight=1.0,
                 updated_at=datetime.now(timezone.utc) - timedelta(days=10))
    client = _collision_client()
    stat = {}
    _handle_rule_stub_sink(tmp_store, client, "memory", e1, ms, stat, [])
    _handle_rule_stub_sink(tmp_store, client, "memory", e2, ms, stat, [])
    s1, s2 = _make_stub(e1), _make_stub(e2)
    assert s1 != s2
    assert s1 in tmp_store.entries("memory")
    assert s2 in tmp_store.entries("memory")
    m1, m2 = ms.get_entry(s1), ms.get_entry(s2)
    assert m1.get("cold_id") == "cold-1"
    assert m2.get("cold_id") == "cold-2"
    # 两条不同 source 内容都真实落冷层 (不是共享幂等写)
    assert e1 in client.contents and e2 in client.contents

    store = LocalStore(tmp_store.memory_path, tmp_store.user_path)
    metastores = {"memory": ms}
    remaining = restore_stubs_from_results(store, metastores, [
        {"id": "cold-2", "content": e2},
        {"id": "cold-1", "content": e1},
    ])
    ents = store.entries("memory")
    assert e1 in ents and e2 in ents, "两条碰撞规则必须按 cold_id 各自写回"
    assert s1 not in ents and s2 not in ents
    assert remaining == []
    assert (ms.get_entry(e1) or {}).get("cold_id") == "cold-1"
    assert (ms.get_entry(e2) or {}).get("cold_id") == "cold-2"


def test_f2_stub_age_protected_and_full_candidates_first(tmp_store, meta_for):
    """F2: 本轮新建 stub 不参 GC; 全文候选优先于指针。"""
    ms = meta_for("memory")
    now = datetime.now(timezone.utc)
    # 新建 stub: last_evicted_at=now, rank 故意最低
    fresh_stub = f"{STUB_PREFIX}新指针→recall(\"新指针-aaaa\")"
    tmp_store.add("memory", fresh_stub)
    ms.stamp(fresh_stub, "stub", origin="stub_sink", cold_id="c-fresh",
             weight=0.01, updated_at=now - timedelta(days=999),
             last_evicted_at=now, retire_count=1, last_active_at=now)
    # 旧 stub: 年龄足够, 可回收
    old_stub = f"{STUB_PREFIX}旧指针→recall(\"旧指针-bbbb\")"
    tmp_store.add("memory", old_stub)
    ms.stamp(old_stub, "stub", origin="stub_sink", cold_id="c-old",
             weight=0.01,
             updated_at=now - timedelta(days=30),
             last_evicted_at=now - timedelta(days=30),
             last_active_at=now - timedelta(days=30))
    stat = {"stub_gc": 0}
    _stub_gc(tmp_store, ms, "memory", stat, force=True)
    assert fresh_stub in tmp_store.entries("memory"), "本轮新建 stub 不得被 GC"
    assert old_stub not in tmp_store.entries("memory"), "旧 stub 应按需回收"

    # 两阶段候选: include_stubs=False 不返回 stub
    sel_full = _select_retirement_candidates(
        tmp_store, ms, "memory", 10 ** 6, {}, include_stubs=False)
    assert fresh_stub not in sel_full and old_stub not in sel_full


def test_f2_protected_marker_inherited_to_stub(tmp_store, meta_for):
    ms = meta_for("memory")
    e = ("用户明确要求: 交付完成后必须通知我并附上回执。"
         + "内容甲" * 10)
    tmp_store.add("memory", e)
    ms.stamp(e, "rule", weight=1.0,
             updated_at=datetime.now(timezone.utc) - timedelta(days=10))
    assert ov._is_protected_rule(e, ms.get_entry(e)) is True
    stat = {}
    _handle_rule_stub_sink(tmp_store, _collision_client(), "memory", e, ms,
                           stat, [])
    sm = ms.get_entry(_make_stub(e))
    assert sm.get("protected") is True
    assert ov._is_protected_rule(_make_stub(e), sm) is True


def test_f2_enforce_does_not_gc_pointer_to_preserve_full_text(
        tmp_store, meta_for, monkeypatch):
    """F2 核心: 全文超预算时先换全文, 不得先删指针。"""
    ms = meta_for("memory")
    old = datetime.now(timezone.utc) - timedelta(days=40)
    # 预置: 一条低 rank 旧 pointer + 一条高 rank 全文; full 超预算。
    stub = f"{STUB_PREFIX}预置指针→recall(\"预置指针-cccc\")"
    tmp_store.add("memory", stub)
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="c_pre",
             weight=0.01, last_active_at=old,
             updated_at=old, last_evicted_at=old)
    full = "普通规则: " + "内容甲" * 200
    tmp_store.add("memory", full)
    ms.stamp(full, "rule", weight=5.0, updated_at=old, last_active_at=old)
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", 10)
    stat = {}
    enforce_rule_budget(tmp_store, _collision_client(), "memory", ms, stat)
    hot = tmp_store.entries("memory")
    # 全文先被换成指针 (冷写成功), 预置指针保留 — 无"删指针保全文"。
    assert full not in hot
    assert _make_stub(full) in hot
    assert stub in hot, "内容仍超预算时不得先 GC 已有指针"
    assert stat.get("stub_gc", 0) == 0, stat


def test_f4_action_trigger_scope_and_pure_s_no_writeback(tmp_store, meta_for):
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    p = mod.MemoryCorePrefetchProvider()
    # 弱词不单独触发; 泛动词需带动作对象; 如何式提问不触发。
    for weak in ("帮我写一首诗", "你刚才回复得不错", "请发送文件",
                 "我想更新一下头像", "怎么安装 Python 包", "修改头像"):
        assert p._action_trigger_hit(weak) is False, weak
    for strong in ("准备发布并通知用户", "删除旧配置", "更新系统配置",
                   "配置服务器端口"):
        assert p._action_trigger_hit(strong) is True, strong
    # 强词只在最近用户意图上触发
    p.sync_turn("准备发布并通知用户", "好的")
    p.on_turn_start(1, "assistant 再次说发布/通知", tool_count=0)
    assert p._action_trigger_hit(p._last_user_intent) is True
    assert p._pending_action_query.startswith("准备发布并通知用户")
    # assistant 文本不得进入合成 query / 触发动作
    p2 = mod.MemoryCorePrefetchProvider()
    p2.sync_turn("帮我写一首诗", "好的，我这就来写")
    p2.on_turn_start(1, "好的，我这就来写", tool_count=0)
    assert p2._pending_action_query == ""
    # tool_count>0 可触发, 但 query 仍用最近用户意图, 不含 assistant 文本
    p3 = mod.MemoryCorePrefetchProvider()
    p3.sync_turn("继续", "assistant 文本不应进入")
    p3.on_turn_start(1, "assistant 文本不应进入", tool_count=1)
    assert "assistant 文本不应进入" not in p3._pending_action_query


def test_f4_pure_s_plus_action_injects_not_writeback(tmp_store, meta_for):
    """F4: 纯 S+action 只注入, 不得写回全文 (action 不是写回共识)。"""
    ms = meta_for("memory")
    full = "不相关规则: 对外发布前必须通知用户。"
    stub = _make_stub("不相关规则: 对外发布前必须通知用户。")
    tmp_store.add("memory", stub)
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="c_s",
             handle="#h_s", weight=0.5)
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    fake = _FakeRecall([{
        "id": "c_s", "content": full, "dense_score": 0.95,
        "keyword_score": 0, "fts_score": 0,
    }])
    mod.ColdStoreClient = lambda *a, **k: fake
    p = mod.MemoryCorePrefetchProvider()
    out = p._recall_sync("帮我处理一件不相干的事情", action_trigger=True)
    assert "不相关规则" in out, out  # S 通道可注入上下文
    assert stub in tmp_store.entries("memory"), "纯 S+action 不得写回"
    assert full not in tmp_store.entries("memory")


def test_f4_low_info_h_channel_no_writeback(tmp_store, meta_for):
    """F4 验收: query='系统' 低信息, 即使 H 句柄主题匹配也不写回。"""
    ms = meta_for("memory")
    full = "系统配置必须使用国内镜像源, 发布前通知用户。"
    stub = _make_stub("系统配置必须使用国内镜像源。")
    tmp_store.add("memory", stub)
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="c_h",
             handle="#h_h", weight=0.5)
    mod = _load_plugin()
    _patch_plugin_store(mod, tmp_store, meta_for)
    fake = _FakeRecall([{
        "id": "c_h", "content": full, "dense_score": 0.30,
        "keyword_score": 0, "fts_score": 0,
    }])
    mod.ColdStoreClient = lambda *a, **k: fake
    p = mod.MemoryCorePrefetchProvider()
    assert p._recall_sync("系统") == ""
    assert fake.queries == [], "低信息闸门必须零冷层 RPC"
    assert stub in tmp_store.entries("memory")
    assert full not in tmp_store.entries("memory")


def test_f5_replay_reads_frozen_fixture_and_passes():
    """F5: replay 只读固化 fixture (200/226, dense 已落盘), 缺页率≤10%。"""
    import importlib.util
    fixture = json.loads((ROOT / "tests/fixtures/fault_replay_silver.json")
                         .read_text(encoding="utf-8"))
    assert len(fixture["queries"]) == 200
    assert len(fixture["pairs"]) == 226
    assert len(fixture["rules"]) == 25
    assert all(len(q["dense"]) == len(fixture["rules"])
               for q in fixture["queries"]), "dense 向量必须固化在 fixture"
    assert "one-shot" in fixture["provenance"]["generator"]
    # fixture 自描述策略计数与 EVIDENCE 一致
    assert fixture["provenance"]["policy_counts_reconstructed"] == {
        "k3_048": 117, "k8_042": 172,
        "k8_union_lex": 188, "k20": 198,
    }
    spec = importlib.util.spec_from_file_location(
        "fix3_replay", ROOT / "tools/replay_fault_rate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    result = mod.evaluate_fixture(fixture)
    imp = result["implementation"]
    assert imp["fault_rate"] <= 0.10, result
    assert imp["relative_drop_vs_baseline"] >= 0.70, result
    assert result["silver"]["baseline_k3_048_fault_rate"] == 0.415
