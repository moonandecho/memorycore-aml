#!/usr/bin/env python3
"""Cycle-2 round 3A contract regressions.

Covers the round-2 five-fix set plus review-round-3 B1/B2/G4/C1/C2/C4:
  * image-only Add persists a searchable placeholder instead of returning a
    zero-write 200;
  * recall candidate pool is wider than top_k and decay runs before truncation;
  * data directory is validated with a real write probe at startup;
  * role is strictly user/assistant;
  * media limits (10 MiB/image, 30 MiB/Add) and tolerant Base64 validation;
  * non-finite scores never turn into a 500 / invalid JSON.

The fixtures use a deterministic fake cold tier; no ollama / production data.
"""
import base64
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import memorycore.aml_server as aml  # noqa: E402


class FakeCold:
    def __init__(self):
        self.memories: List[Dict[str, Any]] = []
        self.recall_calls: List[Dict[str, Any]] = []
        self.search_fn = None

    def stats(self, all_sessions=False):
        return {"total": len(self.memories)}

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        mid = f"m{len(self.memories) + 1}"
        self.memories.append({
            "id": mid,
            "content": content,
            "author_id": author_id,
            "dense_score": 0.9,
            "importance": importance,
        })
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
        q = (query or "").lower()
        tokens = [t for t in q.split() if t]
        hits = []
        for m in self.memories:
            if author_id is not None and m.get("author_id") != author_id:
                continue
            if not tokens or any(t in (m.get("content") or "").lower()
                                 for t in tokens):
                hits.append(dict(m))
        return hits[:top_k]


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


def _add_body(rid, user_id, messages, session_id="s3a"):
    return {
        "request_id": rid,
        "messages": messages,
        "user_id": user_id,
        "session_id": session_id,
    }


def _img_url(decoded: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(decoded).decode()


# ---------------------------------------------------------------------------
# B1: image-only Add is a real write and is immediately searchable.
# ---------------------------------------------------------------------------

def test_b1_image_only_add_persists_searchable_placeholder(aml_http):
    c, fake = aml_http
    body = _add_body("r3a:b1:img", "r3a:b1", [{
        "role": "user",
        "content": [{"type": "image_url",
                     "image_url": {"url": _img_url(b"x" * 64)}}],
    }])
    r = c.post("/add", json=body)
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert len(fake.memories) == 1, "image-only Add must persist one record"
    text = fake.memories[0]["content"]
    assert "[image-only message]" in text
    assert "mime=image/png" in text
    assert "decoded_bytes=64" in text
    assert "session=s3a" in text

    s = c.post("/search", json={"query": "decoded_bytes=64",
                                "user_id": "r3a:b1", "top_k": 5})
    assert s.status_code == 200, s.text
    data = s.json()["data"]
    assert data, "placeholder must be retrievable by its metadata words"
    assert "decoded_bytes=64" in data[0]["content"]


def test_b1_image_only_replay_keeps_idempotency(aml_http):
    c, fake = aml_http
    body = _add_body("r3a:b1:replay", "r3a:b1:replay", [{
        "role": "user",
        "content": [{"type": "image_url",
                     "image_url": {"url": _img_url(b"y" * 64)}}],
    }])
    first = c.post("/add", json=body)
    assert first.status_code == 200, first.text
    rows = len(fake.memories)
    second = c.post("/add", json=body)
    assert second.status_code == 200, second.text
    assert second.json() == first.json()
    assert len(fake.memories) == rows


def test_b1_image_only_search_query_returns_empty_data(aml_http):
    c, _ = aml_http
    r = c.post("/search", json={
        "query": [{"type": "image_url",
                   "image_url": {"url": _img_url(b"z" * 64)}}],
        "user_id": "r3a:b1", "top_k": 5,
    })
    assert r.status_code == 200, r.text
    assert r.json() == {"data": []}


# ---------------------------------------------------------------------------
# B2: candidate pool > top_k; decay before truncation.
# ---------------------------------------------------------------------------

def _aged_candidates():
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    old = (now - timedelta(days=400)).isoformat()
    fresh = now.isoformat()
    return [
        {"id": "old-high-dense", "content": "old high dense memory",
         "dense_score": 0.9, "importance": 0.6, "last_recalled": old},
        {"id": "new-low-dense", "content": "new low dense memory",
         "dense_score": 0.8, "importance": 0.6, "last_recalled": fresh},
    ]


def _pool_search_fn(candidates):
    def search_fn(query, top_k=5, author_id=None):
        return [dict(x) for x in candidates[:top_k]]
    return search_fn


def test_b2_top1_is_final_score_winner_and_topk_prefix(aml_http):
    c, fake = aml_http
    fake.search_fn = _pool_search_fn(_aged_candidates())
    r1 = c.post("/search", json={"query": "memory", "user_id": "u",
                                 "top_k": 1})
    assert r1.status_code == 200, r1.text
    top1 = r1.json()["data"]
    assert top1 and top1[0]["id"] == "new-low-dense"
    assert all(call["top_k"] >= 30 for call in fake.recall_calls), \
        "primary recall must request a candidate pool wider than top_k"

    fake.recall_calls.clear()
    r2 = c.post("/search", json={"query": "memory", "user_id": "u",
                                 "top_k": 2})
    assert r2.status_code == 200, r2.text
    top2 = r2.json()["data"]
    assert [d["id"] for d in top2][:1] == [d["id"] for d in top1], \
        "top_k=k must be a prefix of a larger K"
    assert top2[0]["id"] == "new-low-dense"
    assert all(call["top_k"] >= 30 for call in fake.recall_calls)


def test_b2_options_fallback_competes_before_truncation(aml_http):
    c, fake = aml_http
    calls = []

    def search_fn(query, top_k=5, author_id=None):
        calls.append((query, top_k))
        if len(calls) == 1:
            return []
        return [{"id": "fallback", "content": "fallback evidence",
                 "dense_score": 0.9, "importance": 0.9}]

    fake.search_fn = search_fn
    r = c.post("/search", json={
        "query": "which option is correct?",
        "options": ["fallback evidence"],
        "user_id": "u",
        "top_k": 1,
    })
    assert r.status_code == 200, r.text
    assert r.json()["data"][0]["id"] == "fallback"
    assert all(k >= 30 for _, k in calls)


def test_b2_count_never_exceeds_top_k_and_empty_is_array(aml_http):
    c, fake = aml_http
    fake.search_fn = lambda query, top_k=5, author_id=None: [
        {"id": f"c{i}", "content": f"candidate {i}",
         "dense_score": 0.5, "importance": 0.9} for i in range(10)
    ]
    r = c.post("/search", json={"query": "x", "user_id": "u", "top_k": 3})
    assert r.status_code == 200, r.text
    assert len(r.json()["data"]) <= 3

    fake.search_fn = lambda query, top_k=5, author_id=None: []
    e = c.post("/search", json={"query": "x", "user_id": "u", "top_k": 3})
    assert e.status_code == 200, e.text
    assert e.json() == {"data": []}


# ---------------------------------------------------------------------------
# Round-2 carry-over: all-batch governance filtering still returns 200.
# ---------------------------------------------------------------------------

def test_all_batch_filtered_add_returns_200_without_write(aml_http, monkeypatch):
    c, fake = aml_http

    def filtered_plan(client, content, user_id):
        return {"action": "stale",
                "result": {"status": "stale", "detail": "filtered by test"}}

    monkeypatch.setattr(aml, "_plan_fragment", filtered_plan)
    r = c.post("/add", json=_add_body(
        "r3a:filtered", "u", [{"role": "user", "content": "stale fact"}]))
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True
    assert fake.memories == []


# ---------------------------------------------------------------------------
# C1: role is strictly user/assistant.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad_role", ["system", "tool", "", "User", None, 3])
def test_c1_role_not_user_or_assistant_is_422(aml_http, bad_role):
    c, fake = aml_http
    r = c.post("/add", json=_add_body(
        f"r3a:c1:{bad_role!r}", "u",
        [{"role": bad_role, "content": "role validation memory"}]))
    assert r.status_code == 422, r.text
    assert fake.memories == []


@pytest.mark.parametrize("ok_role", ["user", "assistant"])
def test_c1_user_and_assistant_are_written(aml_http, ok_role):
    c, fake = aml_http
    r = c.post("/add", json=_add_body(
        f"r3a:c1:ok:{ok_role}", "u",
        [{"role": ok_role, "content": "role validation memory"}]))
    assert r.status_code == 200, r.text
    assert len(fake.memories) == 1


# ---------------------------------------------------------------------------
# C2: per-image and per-request media limits + tolerant Base64 validation.
# ---------------------------------------------------------------------------

def test_c2_invalid_base64_payload_is_skipped_not_rejected():
    texts, image_count, skip_count, err = aml._extract_texts([
        {"type": "image_url", "image_url": {
            "url": "data:image/png;base64,!!!"}},
        {"type": "text", "text": "caption kept"},
    ], "content")
    assert err is None
    assert texts == ["caption kept"]
    assert image_count == 0
    assert skip_count == 1


def test_c2_four_scaled_images_total_limit_skips_fourth(monkeypatch):
    monkeypatch.setattr(aml, "_MAX_IMAGE_DECODED_BYTES", 10, raising=False)
    monkeypatch.setattr(aml, "_MAX_TOTAL_IMAGE_DECODED_BYTES", 30,
                        raising=False)
    ten = _img_url(b"a" * 10)
    parts = [{"type": "image_url", "image_url": {"url": ten}}
             for _ in range(4)]
    parts.append({"type": "text", "text": "caption kept"})
    texts, image_count, skip_count, err = aml._extract_texts(parts, "content")
    assert err is None
    assert texts == ["caption kept"]
    assert image_count == 3
    assert skip_count == 1


def test_c2_aggregate_overflow_http_200_text_persists(aml_http, monkeypatch):
    c, fake = aml_http
    monkeypatch.setattr(aml, "_MAX_IMAGE_DECODED_BYTES", 10, raising=False)
    monkeypatch.setattr(aml, "_MAX_TOTAL_IMAGE_DECODED_BYTES", 30,
                        raising=False)
    parts = [{"type": "image_url", "image_url": {"url": _img_url(b"a" * 10)}}
             for _ in range(4)]
    parts.append({"type": "text", "text": "text must survive media overflow"})
    r = c.post("/add", json=_add_body(
        "r3a:c2:http", "u", [{"role": "user", "content": parts}]))
    assert r.status_code == 200, r.text
    assert len(fake.memories) == 1
    assert fake.memories[0]["content"] == "text must survive media overflow"


def test_c2_invalid_base64_http_200_text_persists(aml_http):
    c, fake = aml_http
    r = c.post("/add", json=_add_body(
        "r3a:c2:invalid", "u",
        [{"role": "user", "content": [
            {"type": "image_url", "image_url": {
                "url": "data:image/png;base64,!!!"}},
            {"type": "text", "text": "text survives bad image"},
        ]}]))
    assert r.status_code == 200, r.text
    assert [m["content"] for m in fake.memories] == ["text survives bad image"]


# ---------------------------------------------------------------------------
# C4: non-finite scores must never 500 / emit invalid JSON.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_c4_nonfinite_dense_score_returns_legal_json(aml_http, bad):
    c, fake = aml_http
    fake.search_fn = lambda query, top_k=5, author_id=None: [{
        "id": "bad-score", "content": "bad score memory",
        "dense_score": bad, "importance": 0.6,
    }]
    r = c.post("/search", json={"query": "x", "user_id": "u", "top_k": 5})
    assert r.status_code == 200, r.text
    assert "NaN" not in r.text and "Infinity" not in r.text
    data = r.json()["data"]
    assert data and math.isfinite(float(data[0]["score"]))


def test_c4_nonfinite_final_score_returns_legal_json(aml_http, monkeypatch):
    c, fake = aml_http
    fake.search_fn = lambda query, top_k=5, author_id=None: [{
        "id": "final-bad", "content": "final bad score memory",
        "dense_score": 0.7, "importance": 0.6,
    }]
    monkeypatch.setattr(aml, "_apply_decay", lambda results: [
        {**r, "final_score": float("nan")} for r in results
    ])
    r = c.post("/search", json={"query": "x", "user_id": "u", "top_k": 5})
    assert r.status_code == 200, r.text
    assert "NaN" not in r.text and "Infinity" not in r.text
    assert math.isfinite(float(r.json()["data"][0]["score"]))


# ---------------------------------------------------------------------------
# top_k boundary behaviour carried over from round 2.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("top_k", [None, "5", 2.5, True, -1])
def test_top_k_invalid_values_are_422(aml_http, top_k):
    c, _ = aml_http
    body = {"query": "x", "user_id": "u"}
    if top_k is not None:
        body["top_k"] = top_k
    r = c.post("/search", json=body)
    assert r.status_code == 422, r.text


def test_top_k_zero_and_huge(aml_http):
    c, fake = aml_http
    r0 = c.post("/search", json={"query": "x", "user_id": "u", "top_k": 0})
    assert r0.status_code == 200 and r0.json() == {"data": []}
    assert fake.recall_calls == []

    rh = c.post("/search", json={"query": "x", "user_id": "u",
                                 "top_k": 10 ** 9})
    assert rh.status_code == 200, rh.text
    assert fake.recall_calls[-1]["top_k"] <= aml._MAX_TOP_K


# ---------------------------------------------------------------------------
# G4: data directory fail-closed at startup (subprocess-level).
# ---------------------------------------------------------------------------

def _run_startup(env, timeout=4.0):
    full = os.environ.copy()
    full.update({k: v for k, v in env.items() if v is not None})
    for k, v in env.items():
        if v is None:
            full.pop(k, None)
    full["TMPDIR"] = "/tmp"
    p = subprocess.Popen([
        str(REPO / ".venv" / "bin" / "python"),
        "-m", "memorycore.aml_server",
    ], cwd=str(REPO), env=full, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err, False
    except subprocess.TimeoutExpired:
        p.kill()
        out, err = p.communicate()
        return None, out, err, True


@pytest.mark.parametrize("case", ["unset", "empty", "proc_nonexistent",
                                  "readonly"])
def test_g4_data_dir_fail_closed_at_startup(case, tmp_path):
    env = {"AML_HOST": "127.0.0.1", "AML_PORT": "0"}
    if case == "unset":
        env["MNEMOSYNE_DATA_DIR"] = None
    elif case == "empty":
        env["MNEMOSYNE_DATA_DIR"] = ""
    elif case == "proc_nonexistent":
        env["MNEMOSYNE_DATA_DIR"] = "/proc/1/nonexistent"
    else:
        ro = tmp_path / "readonly"
        ro.mkdir()
        ro.chmod(0o555)
        env["MNEMOSYNE_DATA_DIR"] = str(ro)
    rc, out, err, timed_out = _run_startup(env)
    assert not timed_out, f"server started with bad data dir ({case})"
    assert rc not in (0, None), (case, out[-500:], err[-500:])
    assert "启动失败" in err, (case, out[-500:], err[-500:])
