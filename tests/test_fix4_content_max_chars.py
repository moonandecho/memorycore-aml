#!/usr/bin/env python3
"""fix-4: Search recall content max chars.

Regression coverage for the AML text track's long-evidence Search path:

* content longer than the old 500-char limit must be returned whole when it
  is not longer than the 2000-char default;
* content longer than 2000 chars must be cut to 2000, not 500;
* ``MEMORYCORE_CONTENT_MAX_CHARS`` may override the default;
* invalid env values fall back to 2000 and emit an explicit warning.

The tests use a tiny fake mnemosyne engine so they exercise exactly the
``LocalBackend.recall`` response-assembly boundary (where the old
``[:500]`` lived), without needing an embedding service.
"""
import logging
from typing import Any, Dict, List

import pytest

from memorycore.cold_store_client import LocalBackend, RemoteBackend

DEFAULT_MAX = 2000


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeConn:
    """SQLite-like connection exposing full stored rows by id."""

    def __init__(self, full_by_id):
        self._full_by_id = dict(full_by_id)

    def execute(self, sql, params=()):
        if "working_memory" in sql:
            rows = [
                (rid, self._full_by_id[rid])
                for rid in params
                if rid in self._full_by_id
            ]
            return _FakeCursor(rows)
        return _FakeCursor([])


class _FakeEngine:
    """Minimal engine double that mimics mnemosyne's internal [:500] cut.

    ``recall`` returns the same shape as the real engine, including the
    upstream hard cut.  ``conn`` lets LocalBackend hydrate the stored full
    content by id, which is what the real client must do.
    """

    def __init__(self, items: List[Dict[str, Any]]):
        self._items = [dict(item) for item in items]
        self.calls = []
        self.conn = _FakeConn({
            item.get("id"): item.get("content", "")
            for item in self._items
        })

    def recall(self, query: str, top_k: int = 5, **kwargs):
        self.calls.append({"query": query, "top_k": top_k, "kwargs": kwargs})
        rows = []
        for item in self._items:
            row = dict(item)
            if isinstance(row.get("content"), str):
                row["content"] = row["content"][:500]
            rows.append(row)
        return rows


def _make_local_backend(content: str) -> LocalBackend:
    backend = object.__new__(LocalBackend)
    backend._engine = _FakeEngine([{
        "id": "mem-1",
        "content": content,
        "dense_score": 0.5,
        "keyword_score": 0.4,
        "fts_score": 0.3,
        "importance": 0.8,
        "timestamp": "2026-09-19T00:00:00",
        "last_recalled": None,
    }])
    return backend


def test_recall_returns_full_content_above_old_500_limit(monkeypatch):
    """800-char evidence must round-trip in full (old code returned 500)."""
    monkeypatch.delenv("MEMORYCORE_CONTENT_MAX_CHARS", raising=False)
    content = ("FACT-800: 关键结论是 A 方案。" * 100)[:800]
    assert len(content) == 800

    result = _make_local_backend(content).recall("question", top_k=1)

    assert result["status"] == "ok"
    assert len(result["results"]) == 1
    got = result["results"][0]["content"]
    assert len(got) > 500, "old 500-char truncation still active"
    assert len(got) == len(content)
    assert got == content


def test_recall_truncates_at_default_2000_not_500(monkeypatch):
    """Long evidence is capped at 2000 by default, not at 500."""
    monkeypatch.delenv("MEMORYCORE_CONTENT_MAX_CHARS", raising=False)
    content = ("FACT-2500: " + "长证据" * 900)[:2500]
    assert len(content) == 2500

    result = _make_local_backend(content).recall("question", top_k=1)
    got = result["results"][0]["content"]

    assert len(got) == DEFAULT_MAX
    assert got == content[:DEFAULT_MAX]


def test_recall_results_delegates_and_uses_same_limit(monkeypatch):
    """The convenience API must see the same 2000-char response assembly."""
    monkeypatch.delenv("MEMORYCORE_CONTENT_MAX_CHARS", raising=False)
    content = "R" * 2500

    results = _make_local_backend(content).recall_results("question", top_k=1)

    assert len(results) == 1
    assert len(results[0]["content"]) == DEFAULT_MAX
    assert results[0]["content"] == content[:DEFAULT_MAX]


def test_env_override_caps_at_requested_value(monkeypatch):
    """MEMORYCORE_CONTENT_MAX_CHARS=100 overrides the default."""
    monkeypatch.setenv("MEMORYCORE_CONTENT_MAX_CHARS", "100")
    content = "E" * 300

    result = _make_local_backend(content).recall("question", top_k=1)
    got = result["results"][0]["content"]

    assert len(got) == 100
    assert got == content[:100]


@pytest.mark.parametrize("raw_value", ["abc", "-1", "0", ""])
def test_invalid_env_falls_back_to_2000_and_warns(monkeypatch, caplog,
                                                 raw_value):
    """Invalid env values are never silent: fallback + WARNING log."""
    monkeypatch.setenv("MEMORYCORE_CONTENT_MAX_CHARS", raw_value)
    content = "I" * 2500
    caplog.set_level(logging.WARNING, logger="memorycore.cold_store")

    result = _make_local_backend(content).recall("question", top_k=1)
    got = result["results"][0]["content"]

    assert len(got) == DEFAULT_MAX, (raw_value, len(got))
    assert got == content[:DEFAULT_MAX]
    assert any(
        "MEMORYCORE_CONTENT_MAX_CHARS" in rec.getMessage()
        for rec in caplog.records
    ), caplog.text


def test_remote_backend_does_not_truncate_in_this_client(monkeypatch):
    """Document the path conclusion: RemoteBackend has no client-side cut.

    The remote MCP server owns whatever payload it returns; this client
    passes it through unchanged (unlike LocalBackend's local response
    assembly).  This is deliberately not changed by fix-4.
    """
    monkeypatch.setenv("MEMORYCORE_CONTENT_MAX_CHARS", "100")
    content = "REMOTE" * 1000
    remote = object.__new__(RemoteBackend)
    remote._call_tool = lambda name, arguments: {
        "status": "ok",
        "results": [{"id": "r1", "content": content}],
    }

    raw = remote.recall("question", top_k=1)
    assert raw["results"][0]["content"] == content
    results = remote.recall_results("question", top_k=1)
    assert results[0]["content"] == content
