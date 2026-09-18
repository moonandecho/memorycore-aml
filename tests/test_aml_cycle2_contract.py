#!/usr/bin/env python3
"""Cycle-2 AML contract regression tests (A/B/C/D/E, fix-round1).

These tests use a small in-memory fake for the cold tier so they exercise the
HTTP contract deterministically and do not need ollama.  Integration smoke is
available in tests/test_aml.py.
"""
import os
from typing import Any, Dict, List

import pytest
from starlette.testclient import TestClient

import memorycore.aml_server as aml


class _FakeColdClient:
    def __init__(self):
        self.memories: List[Dict[str, Any]] = []
        self.search_fn = None

    def stats(self, all_sessions=False):
        return {"total": len(self.memories)}

    def recall_results(self, query, top_k=5, author_id=None, **kwargs):
        if self.search_fn is not None:
            return self.search_fn(query, top_k=top_k, author_id=author_id)
        q = (query or "").lower()
        tokens = [t for t in q.split() if t]
        hits = []
        for m in self.memories:
            if author_id is not None and m.get("author_id") != author_id:
                continue
            content = (m.get("content") or "").lower()
            if not tokens or any(t in content for t in tokens):
                hits.append(dict(m))
        return hits[:top_k]


@pytest.fixture
def aml_http(monkeypatch, tmp_path):
    fake = _FakeColdClient()

    def fake_store(client, content, user_id, source="conversation", **_kwargs):
        memory_id = f"m{len(fake.memories) + 1}"
        fake.memories.append({
            "id": memory_id,
            "content": content,
            "author_id": user_id,
            "dense_score": 0.9,
            "importance": 0.6,
        })
        return {"status": "stored", "memory_id": memory_id}

    monkeypatch.setattr(aml, "_get_client", lambda: fake)
    monkeypatch.setattr(aml, "_store_fragment", fake_store)
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    app = aml.mcp.streamable_http_app()
    with TestClient(app) as c:
        yield c, fake


def test_round1_a_content_array_add_and_search_query(aml_http):
    c, fake = aml_http
    uid = "contract:a"
    text = "The ordered caption says the bicycle is red."
    body = {
        "request_id": "contract:a:1",
        "messages": [{
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,AAAA"}},
            ],
        }],
        "user_id": uid,
        "session_id": "contract:a:s",
    }
    r = c.post("/add", json=body)
    assert r.status_code == 200, r.text
    assert any(text in m["content"] for m in fake.memories)

    # Search accepts the same ordered text/image content-array shape.
    s = c.post("/search", json={
        "query": [
            {"type": "text", "text": text},
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64,AAAA"}},
        ],
        "user_id": uid,
        "top_k": 5,
    })
    assert s.status_code == 200, s.text
    data = s.json()["data"]
    assert any(text in d["content"] for d in data)


def test_round1_a_oversize_image_is_skipped_text_kept(monkeypatch):
    # Patch the threshold down so the test does not allocate a real 14 MiB
    # request body; the endpoint probe covers the real >10 MiB case.
    monkeypatch.setattr(aml, "_MAX_IMAGE_DECODED_BYTES", 100)
    payload = "data:image/png;base64," + ("A" * 400)
    texts, image_count, oversize_count, err = aml._extract_texts([
        {"type": "image_url", "image_url": {"url": payload}},
        {"type": "text", "text": "keep this caption"},
    ], "content")
    assert err is None
    assert texts == ["keep this caption"]
    assert image_count == 0
    assert oversize_count == 1


def test_round1_b_options_fallback_respects_top_k(aml_http):
    c, fake = aml_http
    calls: List[str] = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append(query)
        if len(calls) == 1:
            return [{"id": "primary", "content": "primary evidence",
                     "dense_score": 1.0, "importance": 0.6}]
        return [{"id": "extra", "content": "extra evidence",
                 "dense_score": 0.9, "importance": 0.6}]

    fake.search_fn = search_fn
    s = c.post("/search", json={
        "query": "primary evidence",
        "options": ["extra evidence", "extra evidence"],
        "user_id": "contract:b",
        "top_k": 1,
    })
    assert s.status_code == 200, s.text
    data = s.json()["data"]
    assert len(data) <= 1
    assert data and data[0]["id"] == "primary"


@pytest.mark.parametrize("messages", [
    [{"content": "missing role"}],
    [{"role": "user", "content": "   "}],
    [{"role": "user", "content": 123}],
    ["not-an-object"],
    [],
])
def test_round1_c_invalid_messages_are_422_and_do_not_write(aml_http, messages):
    c, fake = aml_http
    r = c.post("/add", json={
        "request_id": f"contract:c:{id(messages)}",
        "messages": messages,
        "user_id": "contract:c",
        "session_id": "contract:c:s",
    })
    assert r.status_code == 422, r.text
    assert isinstance(r.json().get("detail"), dict)
    assert r.json()["detail"].get("reason")
    assert fake.memories == []


def test_round1_d_request_id_ledger_is_persistent_and_conflicts(aml_http):
    c, fake = aml_http
    rid = "contract:d:replay"
    first = {
        "request_id": rid,
        "messages": [{"role": "user", "content": "ledger body one"}],
        "user_id": "contract:d",
        "session_id": "contract:d:s",
    }
    r1 = c.post("/add", json=first)
    assert r1.status_code == 200, r1.text
    count_after_first = len(fake.memories)

    r2 = c.post("/add", json=first)
    assert r2.status_code == 200, r2.text
    assert r2.json() == r1.json()
    assert len(fake.memories) == count_after_first

    second = dict(first)
    second["messages"] = [{"role": "user", "content": "ledger body two MUST_NOT_WRITE"}]
    r3 = c.post("/add", json=second)
    assert r3.status_code == 409, r3.text
    assert len(fake.memories) == count_after_first
    assert all("MUST_NOT_WRITE" not in m["content"] for m in fake.memories)


def test_round1_e_health_reports_version_and_commit(aml_http):
    c, _ = aml_http
    r = c.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert isinstance(body.get("version"), str) and body["version"]
    assert isinstance(body.get("commit"), str) and body["commit"]
