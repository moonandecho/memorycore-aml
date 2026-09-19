#!/usr/bin/env python3
"""E1 回归：跨查询路由 RRF 融合（`_rrf_fuse` + AML_RECALL_FUSION=rrf）。

覆盖：
  1) RRF 按 id 聚合跨路名次分，k=60；
  2) 纯函数、不改输入、容忍缺 id / 非 dict / 同路重复 id；
  3) 单路内 dense_score < 门槛（默认 0.3）的候选不参与融合，
     且被滤候选不占用名次（通过门槛的候选按 1..n 重新排名）；
  4) 服务端开启融合时最终排序 = RRF 分 × 既有衰减因子（复用
     core.decay._apply_decay），并遵守 top_k 契约；
  5) AML_RECALL_FUSION 默认 off = 现状并集 + 衰减。
"""
import sys
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import memorycore.aml_server as aml  # noqa: E402


class FakeCold:
    def __init__(self):
        self.memories = []
        self.recall_calls = []
        self.search_fn = None

    def stats(self, all_sessions=False):
        return {"total": len(self.memories)}

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        mid = f"m{len(self.memories) + 1}"
        self.memories.append({"id": mid, "content": content,
                              "author_id": author_id, "dense_score": 0.9,
                              "importance": importance})
        return {"status": "stored", "memory_id": mid}

    def update(self, memory_id, content, author_id=None, **kwargs):
        for m in self.memories:
            if m.get("id") == memory_id:
                m["content"] = content
        return {"status": "updated", "memory_id": memory_id}

    def recall_results(self, query, top_k=5, author_id=None, **kwargs):
        self.recall_calls.append({
            "query": query, "top_k": top_k, "author_id": author_id,
        })
        if self.search_fn is not None:
            return self.search_fn(query, top_k=top_k, author_id=author_id)
        return [dict(m) for m in self.memories][:top_k]


def _fake_store_fragment(client, content, user_id, source="conversation"):
    r = client.remember(content, importance=0.6, scope="global",
                        author_id=user_id, source=source)
    return {"status": "stored", "memory_id": r.get("memory_id")}


@pytest.fixture
def aml_http(monkeypatch, tmp_path):
    fake = FakeCold()
    monkeypatch.setattr(aml, "_get_client", lambda: fake)
    monkeypatch.setattr(aml, "_store_fragment", _fake_store_fragment)
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    app = aml.mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, fake


def _cand(cid, dense):
    return {"id": cid, "content": f"evidence {cid}",
            "dense_score": dense, "importance": 0.9}


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

def test_rrf_fuse_orders_by_aggregated_reciprocal_rank():
    p1 = [_cand("a", 0.9), _cand("b", 0.9)]
    p2 = [_cand("b", 0.9), _cand("c", 0.9)]
    fused = aml._rrf_fuse([p1, p2], k=60)
    assert [c["id"] for c in fused] == ["b", "a", "c"]
    score = {c["id"]: c["rrf_score"] for c in fused}
    assert score["b"] == pytest.approx(1 / 61 + 1 / 62)
    assert score["a"] == pytest.approx(1 / 61)
    assert score["c"] == pytest.approx(1 / 62)


def test_rrf_fuse_is_pure_and_ignores_junk():
    p1 = [_cand("a", 0.9), None, 42, {"content": "no id"},
          _cand("a", 0.4)]  # 同一路内重复 id 只计一次
    p2 = [_cand("b", 0.9)]
    snapshot = [dict(p1[0]), dict(p2[0])]
    fused = aml._rrf_fuse([p1, p2], k=60)
    assert [c["id"] for c in fused] == ["a", "b"]
    assert p1[0] == snapshot[0] and p2[0] == snapshot[1]  # 不改输入
    assert all(isinstance(c, dict) for c in fused)


def test_rrf_fuse_min_dense_threshold_is_per_path():
    # p1 的 low 不占名次，a 应补位为 rank 1；p2 的 low2 不占名次，c 为 rank 2。
    # C8: low/low2 不再从融合结果消失，而是 rrf_score=0 排在正分候选之后。
    p1 = [_cand("low", 0.10), _cand("a", 0.90), _cand("b", 0.90)]
    p2 = [_cand("b", 0.90), _cand("c", 0.90), _cand("low2", 0.20)]
    fused = aml._rrf_fuse([p1, p2], k=60, min_dense=0.3)
    assert [c["id"] for c in fused] == ["b", "a", "c", "low", "low2"]
    score = {c["id"]: c["rrf_score"] for c in fused}
    assert score["a"] == pytest.approx(1 / 61)          # 没有因 low 而被压到 1/62
    assert score["b"] == pytest.approx(1 / 62 + 1 / 61)
    assert score["c"] == pytest.approx(1 / 62)
    assert score["low"] == 0.0 and score["low2"] == 0.0


def test_rrf_fuse_empty_paths():
    assert aml._rrf_fuse([], k=60) == []
    assert aml._rrf_fuse([[], []], k=60) == []
    assert aml._rrf_fuse([[None, {}, 7]], k=60) == []


# ---------------------------------------------------------------------------
# 服务端端到端
# ---------------------------------------------------------------------------

def test_fusion_enabled_uses_rrf_order_and_decay(aml_http, monkeypatch):
    c, fake = aml_http
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", True)
    monkeypatch.setattr(aml, "_RECALL_FUSION_MIN_DENSE", 0.3)

    calls = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append(query)
        idx = len(calls) - 1
        if idx == 0:
            return [_cand("a", 0.9), _cand("b", 0.9)]
        return [_cand("b", 0.9), _cand("c", 0.9)]

    fake.search_fn = search_fn
    r = c.post("/search", json={
        "query": "What did Alice discuss?",
        "user_id": "u-e1:fusion",
        "top_k": 3,
    })
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    # b 出现在两路 → RRF 第一；importance=0.9（>=0.8）不走衰减。
    assert [d["id"] for d in data] == ["b", "a", "c"], data
    assert all(d["score"] > 0.0 for d in data)
    assert len(data) <= 3


def test_fusion_top_k_contract_is_respected(aml_http, monkeypatch):
    c, fake = aml_http
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", True)
    fake.search_fn = lambda query, top_k=5, author_id=None: [
        _cand(f"c{i}", 0.9) for i in range(40)]
    r = c.post("/search", json={"query": "x y z", "user_id": "u-e1:topk",
                                "top_k": 5})
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) <= 5


def test_fusion_off_by_default_keeps_union_behavior(aml_http, monkeypatch):
    """默认 off：多路仍按并集 + dense 衰减排序，不走 RRF。"""
    c, fake = aml_http
    assert aml._RECALL_FUSION_MODE == "off"
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", False)

    calls = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append(query)
        idx = len(calls) - 1
        if idx == 0:
            return [_cand("b", 0.9), _cand("a", 0.4)]
        return [_cand("a", 0.8), _cand("b", 0.2)]

    fake.search_fn = search_fn
    r = c.post("/search", json={
        "query": "What did Alice discuss?",
        "user_id": "u-e1:off",
        "top_k": 2,
    })
    assert r.status_code == 200, r.text
    # 并集去重后 a 保留 primary 版（dense 0.4），排序为 b,a。
    assert [d["id"] for d in r.json()["data"]] == ["b", "a"]


def test_fusion_gate_falls_back_to_union_when_all_below_threshold(
        aml_http, monkeypatch):
    """所有候选都低于门槛时退回并集，避免门控把结果清零（默认 off 同口径）。"""
    c, fake = aml_http
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", True)
    monkeypatch.setattr(aml, "_RECALL_FUSION_MIN_DENSE", 0.3)
    calls = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append(query)
        return [_cand("low-a", 0.1)] if len(calls) == 1 else [_cand("low-b", 0.2)]

    fake.search_fn = search_fn
    r = c.post("/search", json={"query": "What about alpha beta?", "user_id": "u-e1:gate",
                                "top_k": 5})
    assert r.status_code == 200, r.text
    assert {d["id"] for d in r.json()["data"]} == {"low-a", "low-b"}

def test_fusion_keeps_below_threshold_gold_from_other_path(
        aml_http, monkeypatch):
    """C8: 一路高分时，另一路低于门槛的 gold 不得整段消失。

    改动前：只要任一路有 dense>=门槛，_rrf_fuse 会把所有低于门槛的候选
    从融合结果删除，gold 不在最终 top_k 内 -> 必红。
    改动后：低于门槛候选保留候选身份、融合分记 0，排在正分候选之后。
    """
    c, fake = aml_http
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", True)
    monkeypatch.setattr(aml, "_RECALL_FUSION_MIN_DENSE", 0.3)

    calls = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append(query)
        if len(calls) == 1:
            return [_cand("high-1", 0.9), _cand("high-2", 0.9),
                    _cand("high-3", 0.9)]
        return [_cand("gold-low", 0.1)]

    fake.search_fn = search_fn
    r = c.post("/search", json={
        "query": "What did Alice discuss with Bob about the project?",
        "user_id": "u-e1:c8",
        "top_k": 4,
    })
    assert r.status_code == 200, r.text
    ids = [d["id"] for d in r.json()["data"]]
    assert "gold-low" in ids, ids
