#!/usr/bin/env python3
"""FIX4 P0/P1/P2 回归测试 (2026-09-13)。

覆盖:
  P0 F1: A 组 31 条真实规则自然提问 + B 组 3 条快照规则提问 → 不被闸门误挡;
         原 15 条噪声 → 0 注入; C 组 5 条强动作+共识 → 不被 gate 短路。
  P1 F2: --budget 0 同时零内容/指针预算 → T3 cold-only 明确语义;
         120 条 20 字短全文不因 5000 硬顶卡死/每轮 errors。
  P2 F3: 旧 4-hex 碰撞 pair 在新格式下唯一且各自写回;
         stub 文本碰撞检测 salted 兜底不丢 cold_id 映射。
"""
import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.config import MAX_EVICT_PER_RUN  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    _handle_rule_stub_sink, _make_stub, enforce_rule_budget,
    restore_stubs_from_results,
)
from conftest import MockMnemosyneClient  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402

def _resolve_plugin_path() -> Path:
    """Release layout: hermes-plugin/memorycore-prefetch/__init__.py
    (override via MEMORYCORE_PLUGIN_PATH; no ~/.hermes dependency)."""
    env_path = os.environ.get("MEMORYCORE_PLUGIN_PATH")
    if env_path:
        return Path(env_path)
    return ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py"


PLUGIN_PATH = _resolve_plugin_path()

# A 组: 题面词/主题词自然指向真实规则 (FIX3-REVIEW §2.2 逐条输入)。
A_QUERIES = [
    "天气服务配置改了必须先通知我",
    "周末批量发布任务必须提前通知团队",
    "放假前必须冻结发布并通知值班人",
    "旅游保险服务更新前要通知我",
    "爬山俱乐部活动报名服务上线前必须通知我",
    "拍照检测任务部署前要经过审批",
    "头像上传服务停机维护要通知用户",
    "壁纸网站采集任务上线前必须通知我",
    "办公全家桶安装脚本更新前必须通知我",
    "游戏机回收流程变更前要通知我",
    "电影票接口服务发布前必须通知我",
    "奶茶店订单服务配置修改前必须通知我",
    "咖啡机固件更新前必须通知我",
    "健身数据服务配置修改前先通知我",
    "散步路径推荐服务上线前必须通知我",
    "逛街比价插件发布前必须通知我",
    "Python 服务更新前必须先通知我",
    "Excel 导出任务失败必须通知我",
    "CSV 导入规则修改前先确认回滚",
    "把 GIL 相关服务升级前必须通知我",
    "一首诗生成服务发布前必须通知用户",
    "吃什么推荐服务配置修改前必须通知我",
    "回复得不错自动回复模板更新前必须通知我",
    "主播怎么样看板发布前必须通知我",
    "好的，那就按对外发布必须通知用户的规则来",
    "收到，就按系统配置改动必须通知我的规则执行",
    "可以，那就按继续执行前确认回滚点的规则办",
    "明白，按你说的发送前必须通知审核人的规则来",
    "嗯，就按回复前检查红线规则的流程处理",
    "好的，继续按写周报要附带服务状态的规则执行",
    "[IMPORTANT: 运维] 对外发布必须通知用户",
]

# B 组: 真实 snapshot 规则自然提问 (FIX3-REVIEW §2.3)。
B_QUERIES = [
    "做 PPT 前用哪个 skill 必须问用户吗，还是直接走 python-pptx",
    "demo-host 游戏机定位和性能优先偏好是否覆盖通用省电策略",
    "MemoryCore 周治理是否已经用 python 脚本编排并降低 cron 耦合",
]

# C 组: 强动作 + H/K 共识 (FIX3-REVIEW §2.4)。
C_QUERIES = [
    "更新 Python 服务配置并通知我",
    "周末对外发布上线前通知用户",
    "电影服务发布前必须通知用户",
    "头像服务停机维护前通知用户",
    "准备对外发布通知用户",
]

NOISE_QUERIES = [
    "今天天气真不错，晚上吃什么好呢",
    "好的，收到，明白了",
    "嗯嗯，继续",
    "在吗",
    "谢谢",
    "Python 的 GIL 是什么",
    "怎么把 Excel 转成 CSV",
    "[IMPORTANT: You have 1 unread message from system] 请继续",
    "[ASYNC DELEGATION] background task done",
    "帮我写一首关于春天的诗",
    "你刚才回复得不错",
    "我想更新一下头像",
    "周末打算去爬山，顺便拍点照片",
    "好的，那就按你说的写吧",
    "怎么安装 Python 包",
]

# FIX3-REVIEW R5 实测的旧 4-hex 碰撞 pair。
LEGACY_H4_COLLISION = (
    "系统配置修改前必须通知我并确认回滚点eGPva2A0Fg",
    "系统配置修改前必须通知我并确认回滚点820DfQnPOM",
)


class _FakeRecall:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.queries = []

    def recall_results(self, query, top_k=5, bump=True):
        self.queries.append((query, top_k, bump))
        return [dict(r) for r in self.results]


class _SeqCold:
    """冷写 mock: recall 空, remember 递增返回 cold-id。"""

    def __init__(self):
        self.stored = []
        self.seq = 0

    def recall_results(self, query, top_k=5, bump=True):
        return []

    def remember(self, content, importance=0.6, scope="global"):
        self.seq += 1
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"cold-{self.seq}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}

    def forget(self, memory_id):
        return {"status": "ok"}


def _load_plugin():
    spec = importlib.util.spec_from_file_location("fix4_plugin", PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log_activity_query = lambda q: None
    return mod


def _patch_plugin_store(mod, store, meta_for):
    mod.LocalStore = lambda *a, **k: store
    mod.MetaStore = lambda target, **kw: meta_for(target)


def _provider_for(mod, store, meta_for, results):
    _patch_plugin_store(mod, store, meta_for)
    fake = _FakeRecall(results)
    mod.ColdStoreClient = lambda *a, **k: fake
    provider = mod.MemoryCorePrefetchProvider()
    provider._hot_norm = ""
    provider._injected_ids = set()
    return provider, fake


def test_p0_f1_a_b_legit_queries_not_gated_and_injected(
        tmp_store, meta_for):
    """P0 双向验收: A/B 组 34 条全部 gate=false 且真实规则可注入。"""
    mod = _load_plugin()
    provider, fake = _provider_for(
        mod, tmp_store, meta_for,
        [{"id": "c_placeholder", "content": "placeholder",
          "dense_score": 0.95, "keyword_score": 0, "fts_score": 0}])
    rows = []
    for i, q in enumerate(A_QUERIES + B_QUERIES):
        content = f"FIX4-A/B 规则{i}: {q[:16]} 必须通知用户。"
        fake.results = [{
            "id": f"c{i}", "content": content,
            "dense_score": 0.95, "keyword_score": 0, "fts_score": 0}]
        provider._injected_ids = set()
        provider._hot_norm = ""
        gate = provider._is_low_information_query(q)
        out = provider.prefetch(q)
        rows.append({"query": q, "gate": gate,
                     "injected": content in out,
                     "cold_rpc": len(fake.queries)})
    assert not any(r["gate"] for r in rows), rows
    assert all(r["injected"] for r in rows), rows
    assert all(r["cold_rpc"] > 0 for r in rows), rows


def test_p0_topic_words_never_block_alone():
    """P0: 主题/内容词不得单独作为拦截依据 (裸词 + 长主题句均 gate=false)。"""
    mod = _load_plugin()
    bare_topics = [
        "游戏机", "Python", "天气", "周末", "头像", "电影",
        "全家桶", "excel", "csv", "拍照", "吃什么", "喝什么", "回复",
    ]
    long_topic_only = "周末准备去爬山顺便拍点照片但还没决定去哪"
    for q in bare_topics + [long_topic_only]:
        assert mod.MemoryCorePrefetchProvider._is_low_information_query(q) is False, q


def test_p0_f1_original_15_noise_zero_injection(tmp_store, meta_for):
    """P0 双向验收: 原 15 条噪声查询仍逐条 0 注入。"""
    mod = _load_plugin()
    provider, fake = _provider_for(
        mod, tmp_store, meta_for,
        [{"id": "cA", "content": "冷层记忆内容：用户要求每次交付后必须通知。",
          "dense_score": 0.95, "keyword_score": 0, "fts_score": 0}])
    rows = []
    for q in NOISE_QUERIES:
        provider._injected_ids = set()
        provider._hot_norm = ""
        out = provider.prefetch(q)
        rows.append({"query": q, "gate": provider._is_low_information_query(q),
                     "injected": "冷层记忆内容" in out})
    assert all(r["gate"] for r in rows), rows
    assert not any(r["injected"] for r in rows), rows


def test_p0_f1_action_hk_group_yields_and_writebacks(tmp_path, meta_for):
    """P0/F4: C 组 5 条强动作+H/K 共识不被 gate 短路, 全文写回。"""
    full = "对外发布前必须通知用户并确认回滚点。"
    rows = []
    for i, q in enumerate(C_QUERIES):
        store = LocalStore(tmp_path / f"C{i}_M.md", tmp_path / f"C{i}_U.md")
        ms = MetaStore("memory", memory_path=store.memory_path,
                       user_path=store.user_path)
        stub = _make_stub(full)
        store.add("memory", stub)
        ms.stamp(stub, "stub", origin="stub_sink", cold_id="c1",
                 handle="#h1", weight=0.5)
        mod = _load_plugin()
        provider, fake = _provider_for(
            mod, store,
            lambda target, _ms=ms, **kw: _ms,
            [{"id": "c1", "content": full, "dense_score": 0.95,
              "keyword_score": 1, "fts_score": 0}])
        provider.sync_turn(q, "assistant")
        provider.on_turn_start(1, "assistant only", tool_count=0)
        gate = provider._is_low_information_query(
            provider._pending_action_query or "")
        pressed = provider._pending_action_query
        rows.append({
            "query": q,
            "trigger": provider._action_trigger_hit(q),
            "pending_gate": gate,
            "cold_rpc": len(fake.queries),
            "writeback": full in store.entries("memory"),
            "stub_gone": stub not in store.entries("memory"),
            "pending": pressed,
        })
    assert all(r["trigger"] for r in rows), rows
    assert all(not r["pending_gate"] for r in rows), rows
    assert all(r["cold_rpc"] > 0 for r in rows), rows
    assert all(r["writeback"] for r in rows), rows


def test_p2_legacy_4hex_collision_pair_unique_and_both_writeback(
        tmp_store, meta_for):
    """P2: 旧 4-hex 碰撞 pair 在新 16-hex 指纹下唯一且各自按 cold_id 写回。"""
    e1, e2 = LEGACY_H4_COLLISION
    s1, s2 = _make_stub(e1), _make_stub(e2)
    assert s1 != s2, (s1, s2)
    ms = meta_for("memory")
    old = datetime.now(timezone.utc) - timedelta(days=10)
    for e in (e1, e2):
        tmp_store.add("memory", e)
        ms.stamp(e, "rule", weight=1.0, updated_at=old,
                 last_active_at=old)
    client = _SeqCold()
    stat = {}
    _handle_rule_stub_sink(tmp_store, client, "memory", e1, ms, stat, [])
    _handle_rule_stub_sink(tmp_store, client, "memory", e2, ms, stat, [])
    stubs = {e for e in tmp_store.entries("memory")
             if (ms.get_entry(e) or {}).get("type") == "stub"}
    assert stubs == {s1, s2}, stubs
    m1, m2 = ms.get_entry(s1) or {}, ms.get_entry(s2) or {}
    assert m1.get("cold_id") and m2.get("cold_id")
    assert m1["cold_id"] != m2["cold_id"]
    remaining = restore_stubs_from_results(
        tmp_store, {"memory": ms}, [
            {"id": m2["cold_id"], "content": e2},
            {"id": m1["cold_id"], "content": e1},
        ])
    ents = tmp_store.entries("memory")
    assert e1 in ents and e2 in ents, "碰撞 pair 必须各自恢复全文"
    assert not (stubs & set(ents)), ents
    assert remaining == []
    assert (ms.get_entry(e1) or {}).get("cold_id") == m1["cold_id"]
    assert (ms.get_entry(e2) or {}).get("cold_id") == m2["cold_id"]


def test_p2_salted_collision_fallback_preserves_mapping(
        tmp_store, meta_for, monkeypatch):
    """P2: 同 stub 文本碰撞检测触发 salted 兜底, 不覆盖 cold_id 映射。"""
    base_stub = '[规则指针]同前缀→recall("force-same")'

    def _forced_collision_stub(entry, *, salt=""):
        if not salt:
            return base_stub
        return f'[规则指针]同前缀→recall("{str(salt)[-10:]}")'
    monkeypatch.setattr(ov, "_make_stub", _forced_collision_stub)

    e1 = "同前缀规则甲: " + "内容甲" * 12
    e2 = "同前缀规则乙: " + "内容乙" * 12
    ms = meta_for("memory")
    old = datetime.now(timezone.utc) - timedelta(days=10)
    for e in (e1, e2):
        tmp_store.add("memory", e)
        ms.stamp(e, "rule", weight=1.0, updated_at=old,
                 last_active_at=old)
    client = _SeqCold()
    stat = {}
    _handle_rule_stub_sink(tmp_store, client, "memory", e1, ms, stat, [])
    _handle_rule_stub_sink(tmp_store, client, "memory", e2, ms, stat, [])
    hot = [e for e in tmp_store.entries("memory")
           if (ms.get_entry(e) or {}).get("type") == "stub"]
    assert len(hot) == 2, hot
    assert stat.get("stub_collision_fallback", 0) == 1, stat
    cids = [(ms.get_entry(s) or {}).get("cold_id") for s in hot]
    assert all(cids) and cids[0] != cids[1], cids
    remaining = restore_stubs_from_results(
        tmp_store, {"memory": ms},
        [{"id": cids[1], "content": e2}, {"id": cids[0], "content": e1}])
    ents = tmp_store.entries("memory")
    assert e1 in ents and e2 in ents, "salted 兜底后两条规则仍须各自写回"
    assert remaining == []


def test_p1_true_zero_budgets_simultaneous_and_t3_semantics(
        tmp_store, meta_for, monkeypatch):
    """P1: 内容预算与 stub 预算同时置 0 → cold-only, 语义显式。"""
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", 0)
    monkeypatch.setattr(config_mod, "RULE_BUDGET_CHARS", 0)
    ms = meta_for("memory")
    old = datetime.now(timezone.utc) - timedelta(days=40)
    originals = []
    for i in range(5):
        e = f"{i}号短规则: " + "内容" * 10
        tmp_store.add("memory", e)
        ms.stamp(e, "rule", weight=1.0, updated_at=old,
                 last_active_at=old)
        originals.append(e)
    client = MockMnemosyneClient()
    client.recall_results = lambda *a, **k: []
    # FIX7 I1: 单次调用只允许换出 MAX_EVICT_PER_RUN 条; 预算 0 的收敛
    # 必须由多轮调用完成 (不能像 FIX6 那样一次清空 5 条)。
    total_cold_only = 0
    per_call_caps = []
    for _ in range(10):
        stat = {}
        enforce_rule_budget(tmp_store, client, "memory", ms, stat)
        per_call_caps.append(stat.get("lru_evicted", 0))
        total_cold_only += stat.get("cold_only", 0)
        assert stat.get("errors", 0) == 0, stat
        assert stat.get("lru_evicted", 0) <= MAX_EVICT_PER_RUN, stat
        if not tmp_store.entries("memory"):
            break
    assert tmp_store.entries("memory") == [], tmp_store.entries("memory")
    assert all(e in client.stored for e in originals), client.stored
    assert total_cold_only == len(originals), (total_cold_only, per_call_caps)
    assert max(per_call_caps) <= MAX_EVICT_PER_RUN, per_call_caps
    assert per_call_caps and sum(per_call_caps) == len(originals), per_call_caps
    assert stat.get("t3_cold_only_mode") is True, stat
    assert stat.get("budget_semantics") == \
        "T3_cold_only_zero_pointer_budget", stat


def test_p1_short_120_no_hard_cap_stall(meta_for, monkeypatch):
    """P1: 120 条 20 字短全文 + 真 0 预算, 不得每轮 errors/卡死。"""
    import tempfile
    monkeypatch.setattr(ov, "RULE_BUDGET_CHARS", 0)
    monkeypatch.setattr(config_mod, "RULE_BUDGET_CHARS", 0)
    tmp = Path(tempfile.mkdtemp(prefix="fix4_p1_test_"))
    store = LocalStore(tmp / "MEMORY.md", tmp / "USER.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    old = datetime.now(timezone.utc) - timedelta(days=90)
    entries = []
    for i in range(120):
        head = f"{i:04d}号规则:"
        body = ("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未" * 4)
        e = (head + body)[:20]
        store.add("memory", e)
        ms.stamp(e, "rule", weight=1.0, updated_at=old,
                 last_active_at=old)
        entries.append(e)
    client = MockMnemosyneClient()
    client.recall_results = lambda *a, **k: []
    for r in range(100):
        stat = {}
        enforce_rule_budget(store, client, "memory", ms, stat)
        assert stat.get("errors", 0) == 0, (r, stat)
        assert stat.get("lru_evicted", 0) <= MAX_EVICT_PER_RUN, (r, stat)
        if not store.entries("memory"):
            break
    assert store.entries("memory") == [], "短全文应全部冷层-only/离开热层"
    assert all(e in client.stored for e in entries), len(client.stored)
