#!/usr/bin/env python3
"""E3 close-out regression: A prefetch撤回, B byte guard, C Q1/Q2边界。

覆盖：
  A. Search 不再调用 ``prefetch_embeddings``（撤回 3a30479 的批量预热）；
  B. Search 响应组装硬护栏：100 × 400 KB 内容也必须 <= byte budget；
  C① 局部缺 keyword/fts 时按实际存在的信号回退，不把缺失当 0 惩罚；
  C② 极端有限分 min-max 溢出不得静默丢信号，须保留单调顺序；
  C③ 多条单 token 路叠加不得压过主路高分证据（token 路权重总上限）。
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
        self.prefetch_calls = []

    def stats(self, all_sessions=False):
        return {"total": len(self.items)}

    def prefetch_embeddings(self, kind, texts):
        self.prefetch_calls.append((kind, list(texts)))
        return [0.0] * len(texts)

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


# ---------------------------------------------------------------------------
# A: revert 3a30479 — no batch prefetch on the search path
# ---------------------------------------------------------------------------

def test_a_search_does_not_prefetch_embeddings(http_client, monkeypatch):
    c, fake = http_client
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", True)
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)
    monkeypatch.setattr(aml, "_RERANK_TOKEN_ROUTES", 2)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", False)
    fake.items = [_cand("only", 0.9)]

    r = c.post("/search", json={"query": "What did Caroline research?",
                                "user_id": "u-e3close:no-prefetch",
                                "top_k": 1})
    assert r.status_code == 200, r.text
    assert fake.prefetch_calls == [], fake.prefetch_calls
    assert fake.calls, "search should still recall normally"


# ---------------------------------------------------------------------------
# B: response byte hard guard
# ---------------------------------------------------------------------------

def test_b_search_response_byte_guard_100x400kb(http_client, monkeypatch):
    c, fake = http_client
    limit = 28 * 1024 * 1024
    monkeypatch.setattr(aml, "_SEARCH_RESPONSE_MAX_BYTES", limit)
    monkeypatch.setattr(aml, "_RERANK_LEXICAL", False)
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 0)
    monkeypatch.setattr(aml, "_RECALL_FUSION_ON", False)

    one_size = 400 * 1024
    fake.items = []
    for i in range(100):
        fake.items.append({
            "id": f"huge-{i:03d}",
            # 400 KB payload (with a small stable prefix)
            "content": f"item-{i:03d}:" + "x" * (one_size - 10),
            "dense_score": 0.9 - i * 1e-4,
            "importance": 0.9,
            "timestamp": "2026-01-01T00:00:00Z",
        })

    r = c.post("/search", json={"query": "huge response guard",
                                "user_id": "u-e3close:byte-guard",
                                "top_k": 100})
    assert r.status_code == 200, r.text
    # 改动前：100 条全量返回约 39 MiB，必红；改动后必须 <= limit。
    assert len(r.content) <= limit, (len(r.content), limit)
    data = r.json()["data"]
    assert 0 < len(data) < 100, len(data)
    # 尾部截断必须保留最高分的前缀（id 顺序仍为 huge-000...）。
    assert data[0]["id"] == "huge-000"
    assert [d["id"] for d in data] == [
        f"huge-{i:03d}" for i in range(len(data))]


# ---------------------------------------------------------------------------
# C①: partial missing keyword/fts fallback
# ---------------------------------------------------------------------------

def test_c1_partial_missing_keyword_does_not_penalize_legacy_row():
    rows = [
        {"id": "legacy_missing_kw", "dense_score": 0.90,
         "fts_score": 0.0, "importance": 0.9},
        {"id": "new_with_kw", "dense_score": 0.50,
         "keyword_score": 1.0, "fts_score": 0.0, "importance": 0.9},
    ]
    scores = aml._lexical_weighted_scores(rows)
    by_id = {r["id"]: s for r, s in zip(rows, scores)}
    # 局部缺失回退为 dense-only；不得把缺 keyword 的旧行当作 0 分惩罚。
    assert by_id["legacy_missing_kw"] > by_id["new_with_kw"], by_id


# ---------------------------------------------------------------------------
# C②: extreme finite min-max overflow must not silently drop the signal
# ---------------------------------------------------------------------------

def test_c2_extreme_finite_scores_preserve_signal_order():
    assert aml._unit_minmax([-1e308, 1e308]) == [0.0, 1.0]
    rows = [
        {"id": "lo", "dense_score": -1e308,
         "keyword_score": 0.0, "fts_score": 0.0, "importance": 0.9},
        {"id": "hi", "dense_score": 1e308,
         "keyword_score": 0.0, "fts_score": 0.0, "importance": 0.9},
    ]
    scores = aml._lexical_weighted_scores(rows)
    assert scores[1] > scores[0], scores
    assert [c["id"] for c in aml._rerank_path_lexical(rows)] == ["hi", "lo"]


# ---------------------------------------------------------------------------
# C③: token expansion routes cannot stack over primary high-dense evidence
# ---------------------------------------------------------------------------

def test_c3_multi_token_route_stack_cannot_outrank_primary_high_dense():
    primary = [_cand("good", 0.95)]
    token1 = [_cand("multi-token-distractor", 0.35)]
    token2 = [_cand("multi-token-distractor", 0.35)]
    weights = aml._search_path_weights([primary, token1, token2],
                                       main_path_count=1)
    assert sum(weights[1:]) <= weights[0] + 1e-12
    fused = aml._rrf_fuse([primary, token1, token2], k=60,
                          min_dense=0.3, path_weights=weights)
    ids = [c["id"] for c in fused]
    assert ids[0] == "good", fused
    by_id = {c["id"]: c.get("rrf_score", 0.0) for c in fused}
    assert by_id["good"] > by_id["multi-token-distractor"]


def test_c3_token_weight_total_is_capped():
    primary = [_cand(f"p{i}", 0.9) for i in range(20)]
    token1 = [_cand("t", 0.9)]
    token2 = [_cand("t", 0.9)]
    weights = aml._search_path_weights([primary, token1, token2],
                                       main_path_count=1)
    token_total = sum(weights[1:])
    assert token_total <= weights[0] * aml._RERANK_TOKEN_RRF_TOTAL_MULT + 1e-12
