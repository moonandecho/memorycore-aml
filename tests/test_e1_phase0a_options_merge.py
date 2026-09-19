#!/usr/bin/env python3
"""Phase 0a 回归（改动前必红）：options 分支不得覆盖关键词多路召回结果。

背景：`_aml_search_sync_locked` 中 keyword 路用 `extra_results.extend(...)`，
随后 options 路误用 `extra_results = client.recall_results(...)` 赋值，把 keyword
路整段覆盖。测试用 FakeCold 记录 recall 调用，锁死「options 请求必须同时包含
关键词路与 options 路的结果」。
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


def test_options_does_not_overwrite_keyword_multi_query_path(aml_http, monkeypatch):
    c, fake = aml_http
    monkeypatch.setattr(aml, "_RECALL_MULTI_QUERY", 1)

    calls = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append(query)
        idx = len(calls) - 1
        if idx == 0:
            return [_cand("primary", 0.9)]
        if idx == 1:
            return [_cand("keyword", 0.8)]
        return [_cand("options", 0.7)]

    fake.search_fn = search_fn
    r = c.post("/search", json={
        "query": "What did Alice discuss on Friday?",
        "options": ["engineer", "doctor"],
        "user_id": "u-e1:0a",
        "top_k": 3,
    })
    assert r.status_code == 200, r.text
    ids = [d["id"] for d in r.json()["data"]]

    # 三条路都必须被调用，且 keyword 路结果不得被 options 赋值覆盖。
    assert len(calls) == 3, calls
    assert "alice discuss friday" in calls[1], calls
    assert "engineer" in calls[2] and calls[2].startswith(calls[0]), calls
    assert ids == ["primary", "keyword", "options"], ids
