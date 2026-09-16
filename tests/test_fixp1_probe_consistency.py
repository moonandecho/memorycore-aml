#!/usr/bin/env python3
"""tests/test_fixp1_probe_consistency.py — FIX-P1 观测字段自洽回归。

四个验收点 (只改事件构造层, 不改注入/排序/写回/MCP 返回):
  * F1-server:  handle 直查 H 行 + kw/fts → channel="H" 且 k_source="";
  * F1-prefetch: H 句柄候选 + kw/fts → channel="H" 且 k_source="";
  * F2:          重复 id 候选 → injected 只对真正留在 selected 的那一行 True;
  * F3:          K 共识候选被写回移除 → event/candidates.channel 仍为 "K"。

全部使用内存 mock, 不访问网络/生产冷层, 不写真实数据。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore import server  # noqa: E402
from memorycore.core import recall_probe  # noqa: E402


def _resolve_plugin_path() -> Path:
    """Release layout: <repo-root>/hermes-plugin/memorycore-prefetch/__init__.py."""
    try:
        from conftest import PLUGIN_PATH  # release layout via conftest
        return PLUGIN_PATH
    except Exception:
        return (ROOT / "hermes-plugin" / "memorycore-prefetch"
                / "__init__.py")


def _load_plugin(name: str):
    spec = importlib.util.spec_from_file_location(name, _resolve_plugin_path())
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _RecallClient:
    """server 侧固定顺序候选 mock。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]

    def recall_results(self, query, top_k=5, bump=True):
        return [dict(it) for it in self.items]


class _FakeClient:
    """prefetch 侧固定冷层候选 mock。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]
        self.queries = []

    def recall_results(self, query, top_k=5, bump=True):
        self.queries.append((query, top_k, bump))
        return [dict(it) for it in self.items]


def _probe_env(monkeypatch, tmp_path):
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    return target


def _make_provider(mod, items, records, tmp_path, restore=None):
    provider = mod.MemoryCorePrefetchProvider()
    provider._load_directory = lambda: list(records)
    provider._injected_ids = set()
    provider._hot_norm = ""
    provider._record_baseline = lambda results: None
    provider._mark_injected_audit = lambda results: None
    fake = _FakeClient(items)

    class _FakeStore:
        memory_path = tmp_path / "MEMORY.md"
        user_path = tmp_path / "USER.md"

    mod.ColdStoreClient = lambda *a, **k: fake
    mod.LocalStore = lambda *a, **k: _FakeStore()
    mod.MetaStore = lambda *a, **k: _FakeStore()
    mod.log_activity_query = lambda q: None
    if restore is None:
        mod.restore_stubs_from_results = lambda store, metas, results: list(results)
    else:
        mod.restore_stubs_from_results = restore
    return provider, fake


# ---------------------- F1 server -------------------------------------------

def test_f1_server_handle_h_row_k_source_empty(
        tmp_store, meta_for, tmp_path, monkeypatch):
    """H 行 + cold kw/fts → channel=H, k_source="" (不能矛盾成 cold_kw_fts)。"""
    stub = "[规则指针]主题丁→recall:0123456789abcdef"
    tmp_store.add("memory", stub)
    meta_for("memory").stamp(stub, "stub", origin="stub_sink",
                             cold_id="cold-h3", handle="#h3")
    server._store = tmp_store
    server._client = _RecallClient([
        {"id": "cold-h3", "keyword_score": 0.7, "fts_score": 0.7,
         "dense_score": 0.9, "importance": 0.9},
    ])
    target = _probe_env(monkeypatch, tmp_path)
    response = json.loads(server.memorycore_recall(
        "主题丁", top_k=1, handle="#h3"))
    assert response["results"][0]["channel"] == "H"
    event = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert event["channel"] == ["H"]
    assert event["k_source"] == [""]


# ---------------------- F1 prefetch -----------------------------------------

def test_f1_prefetch_handle_h_row_k_source_empty(tmp_path, monkeypatch):
    """prefetch H 句柄候选 + cold kw/fts → channel=H 且 k_source 为空串。"""
    target = _probe_env(monkeypatch, tmp_path)
    mod = _load_plugin("fixp1_probe_f1_prefetch")
    items = [{"id": "cold-h3", "content": "", "dense_score": 0.9,
              "keyword_score": 0.7, "fts_score": 0.7, "importance": 0.9}]
    records = [{"handle": "#h3", "topic": "主题丁", "cold_id": "cold-h3"}]
    provider, fake = _make_provider(mod, items, records, tmp_path)
    out = provider.prefetch("主题丁")
    assert "MemoryCore Recall" in out
    assert fake.queries and fake.queries[0][2] is False
    event = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert event["source"] == "prefetch"
    assert event["selected"] == ["cold-h3"]
    assert event["channel"] == ["H"]
    assert event["k_source"] == [""]
    assert event["injected"] == [True]


# ---------------------- F2 duplicate id -------------------------------------

def test_f2_duplicate_ids_injected_matches_real_injection(tmp_path, monkeypatch):
    """["dup","dup","c-s"]: 第二个 dup 被 _dedupe_injected 丢弃 → 不得 True。"""
    target = _probe_env(monkeypatch, tmp_path)
    mod = _load_plugin("fixp1_probe_f2_dup")
    items = [
        {"id": "dup", "content": "发布前复核部署", "dense_score": 0.80,
         "keyword_score": 0.9, "importance": 0.9},
        {"id": "dup", "content": "另一个部署复核", "dense_score": 0.80,
         "keyword_score": 0.9, "importance": 0.9},
        {"id": "c-s", "content": "语义内容", "dense_score": 0.70,
         "importance": 0.9},
    ]
    provider, _fake = _make_provider(mod, items, [], tmp_path)
    out = provider.prefetch("发布前复核部署")
    assert out.count("发布前复核部署") == 1
    assert "另一个部署复核" not in out
    event = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert event["returned_ids"] == ["dup", "dup", "c-s"]
    assert event["selected"] == ["dup", "c-s"]
    assert event["injected"] == [True, False, True]


# ---------------------- F3 removed K candidate ------------------------------

def test_f3_writeback_removed_k_candidate_keeps_channel(tmp_path, monkeypatch):
    """K 共识候选被写回移除后, 其原本 H/K/S 语义仍可从 event 读出。"""
    target = _probe_env(monkeypatch, tmp_path)
    mod = _load_plugin("fixp1_probe_f3_removed_k")
    items = [
        {"id": "c-kw", "content": "无关内容A", "dense_score": 0.95,
         "keyword_score": 0.8, "importance": 0.9},
        {"id": "c-s", "content": "语义内容", "dense_score": 0.90,
         "importance": 0.9},
    ]
    # 写回 mock: 所有 WB 候选都被"恢复"掉 (返回空), 等价于 selected 后移除。
    provider, _fake = _make_provider(
        mod, items, [], tmp_path,
        restore=lambda store, metas, results: [])
    out = provider.prefetch("查询内容")
    assert "语义内容" in out
    assert "无关内容A" not in out
    event = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert event["returned_ids"] == ["c-kw", "c-s"]
    assert event["selected"] == ["c-s"]
    assert event["injected"] == [False, True]
    assert event["channel"] == ["K", "S"]
    assert event["candidate_channels"] == ["K", "S"]
    assert event["candidates"][0]["id"] == "c-kw"
    assert event["candidates"][0]["channel"] == "K"
