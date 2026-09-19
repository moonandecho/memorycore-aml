#!/usr/bin/env python3
"""E3 回归：AML_RERANK_LEXICAL 字面/关键词信号 opt-in 重排。

覆盖：
  1) 开关默认 off；关闭时 /search 顺序与现状一致；
  2) dense/keyword/fts 归一化加权在开关注入时能把字面命中从低 dense
     候选提到前面；
  3) 旧数据缺 keyword_score/fts_score 时回退为只按 dense 排序；
  4) 候选池放大开关 AML_RECALL_POOL_MULT 的边界。
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
        self.items = []
        self.calls = []

    def stats(self, all_sessions=False):
        return {"total": len(self.items)}

    def recall_results(self, query, top_k=5, author_id=None, **kwargs):
        self.calls.append({"query": query, "top_k": top_k,
                           "author_id": author_id})
        return [dict(x) for x in self.items][:top_k]


@pytest.fixture
def http_client(monkeypatch, tmp_path):
    fake = FakeCold()
    monkeypatch.setattr(aml, "_get_client", lambda: fake)
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    app = aml.mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, fake


def _cand(cid, dense, keyword=0.0, fts=0.0):
    return {"id": cid, "content": f"evidence for {cid}",
            "dense_score": dense, "keyword_score": keyword,
            "fts_score": fts, "importance": 0.9}


def test_e3_switch_default_off(monkeypatch):
    monkeypatch.delenv("AML_RERANK_LEXICAL", raising=False)
    assert getattr(aml, "_RERANK_LEXICAL", None) is False


def test_e3_scores_promote_literal_hit_with_low_dense():
    rows = [_cand("dense-hi", 0.90), _cand("literal-gold", 0.35, 0.99, 0.99)]
    scores = aml._lexical_weighted_scores(rows)
    by_id = {r["id"]: s for r, s in zip(rows, scores)}
    assert by_id["literal-gold"] > by_id["dense-hi"]


def test_e3_search_opt_in_changes_order(http_client, monkeypatch):
    c, fake = http_client
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", True)
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 0)
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", False)
    fake.items = [_cand("dense-hi", 0.90),
                  _cand("literal-gold", 0.35, 0.99, 0.99)]

    r_off = c.post("/search", json={"query": "literal question",
                                    "user_id": "u-e3:off", "top_k": 2})
    assert r_off.status_code == 200, r_off.text
    assert [d["id"] for d in r_off.json()["data"]] == ["dense-hi", "literal-gold"]

    monkeypatch.setattr(aml, "_RERANK_LEXICAL", True)
    r_on = c.post("/search", json={"query": "literal question",
                                   "user_id": "u-e3:on", "top_k": 2})
    assert r_on.status_code == 200, r_on.text
    assert [d["id"] for d in r_on.json()["data"]] == ["literal-gold", "dense-hi"]


def test_e3_missing_lexical_fields_falls_back_to_dense_order(http_client, monkeypatch):
    c, fake = http_client
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", True)
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 0)
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", True)
    fake.items = [
        {"id": "high", "content": "high", "dense_score": 0.90,
         "importance": 0.9},
        {"id": "low", "content": "low", "dense_score": 0.50,
         "importance": 0.9},
    ]
    r = c.post("/search", json={"query": "question",
                                "user_id": "u-e3:missing", "top_k": 2})
    assert r.status_code == 200, r.text
    assert [d["id"] for d in r.json()["data"]] == ["high", "low"]


def test_recall_pool_mult_bounds(monkeypatch):
    monkeypatch.setattr(aml, "_RECALL_POOL_MULT", 1)
    assert aml._candidate_pool_size(100) == 100
    monkeypatch.setattr(aml, "_RECALL_POOL_MULT", 5)
    monkeypatch.setattr(aml, "_RECALL_POOL_CAP", 500)
    assert aml._candidate_pool_size(100) == 500
    monkeypatch.setattr(aml, "_RECALL_POOL_CAP", 120)
    assert aml._candidate_pool_size(100) == 120

def test_e3_literal_token_expansion_adds_single_token_paths(
        http_client, monkeypatch):
    """E3: 多 token 查询应额外做单 token 召回（opt-in 扩路）。"""
    c, fake = http_client
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", True)
    monkeypatch.setattr(aml, "_RERANK_TOKEN_ROUTES", 2)
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 0)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", False)
    fake.items = [_cand("only", 0.9)]

    r = c.post("/search", json={"query": "What did Caroline research?",
                                "user_id": "u-e3:routes", "top_k": 1})
    assert r.status_code == 200, r.text
    assert [call["query"] for call in fake.calls] == [
        "What did Caroline research?", "caroline", "research"], fake.calls


def test_e3_auto_deep_pool_is_opt_in(monkeypatch):
    monkeypatch.setattr(aml, "_RECALL_POOL_MULT", 1)
    monkeypatch.setattr(aml, "_RECALL_POOL_CAP", 500)
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", False)
    assert aml._search_candidate_pool_size(100) == 100
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", True)
    assert aml._search_candidate_pool_size(100) == 200

def test_rrf_size_weights_favor_small_literal_path():
    """E3: 单 token 字面扩路候选少，RRF 贡献按 1/len(path) 放大。"""
    small = [_cand("rare", 0.5)]
    big = [_cand(f"common-{i}", 0.5) for i in range(10)]
    fused_flat = aml._rrf_fuse([small, big], k=60, min_dense=0.3)
    assert [c["id"] for c in fused_flat][:2] == ["rare", "common-0"]
    fused_size = aml._rrf_fuse([small, big], k=60, min_dense=0.3,
                               size_weights=True)
    score = {c["id"]: c["rrf_score"] for c in fused_size}
    assert score["rare"] > score["common-0"]
