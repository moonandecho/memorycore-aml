#!/usr/bin/env python3
"""Round-3B regression tests: merge fidelity, duplicate threshold, read-only Search.

Each test is intentionally written against the round-3B acceptance criteria so
that it fails on the pre-fix code (see evidence/probe_round3b.py old-code run).
"""
import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from memorycore.core.overflow import _merge_two_entries  # noqa: E402
from memorycore.aml_server import (  # noqa: E402
    _aml_match_level, _split_fragments, _store_fragment,
)
import memorycore.aml_server as aml  # noqa: E402


# ---------------------------------------------------------------------------
# Shared helpers / fake cold tiers
# ---------------------------------------------------------------------------

class _MemoryClient:
    """Minimal in-memory cold tier with substring recall (no ollama)."""

    def __init__(self):
        self.memories = []

    def recall_results(self, query, top_k=5, author_id=None, bump=True,
                       **kwargs):
        return [dict(m) for m in self.memories[:top_k]]

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        mid = f"m{len(self.memories) + 1}"
        self.memories.append({
            "id": mid,
            "content": content,
            "author_id": author_id,
            "dense_score": 0.95,
            "importance": importance,
        })
        return {"status": "stored", "memory_id": mid}

    def update(self, memory_id, content, author_id=None, **kwargs):
        for m in self.memories:
            if m["id"] == memory_id:
                m["content"] = content
        return {"status": "updated", "memory_id": memory_id}


def _fact(i: int) -> str:
    return (f"用户档案记录编号 {i:04d}：该用户的配送地址为北京市朝阳区示例路"
            f"{i}号，联系电话为 1380000{i:04d}。")


# ---------------------------------------------------------------------------
# ① merge fidelity: preserve newline/semicolon; no Chinese full-stop rewrite
# ---------------------------------------------------------------------------

def test_merge_two_functions_stays_parseable():
    local = "def add(a, b):\n    return a + b"
    cold = "def sub(a, b):\n    return a - b"
    merged = _merge_two_entries(local, cold)
    assert "def add" in merged and "def sub" in merged
    ast.parse(merged)


def test_merge_two_semicolon_statements_stays_parseable():
    merged = _merge_two_entries("a = 1; b = 2", "c = 3; d = 4")
    assert "a = 1" in merged and "c = 3" in merged
    ast.parse(merged)


def test_merge_768_char_code_split_three_pieces_ast_parse():
    # Three legal Python lines, exactly 256 chars each including the newline.
    lines = [
        f"x{i} = 1  # " + "a" * 245 + "\n"
        for i in range(3)
    ]
    code = "".join(lines)
    assert len(code) == 768
    parts = _split_fragments(code, max_chars=256)
    assert len(parts) == 3, [len(p) for p in parts]
    assert "".join(parts) == code, "split must be lossless"
    rebuilt = parts[0]
    for part in parts[1:]:
        rebuilt = _merge_two_entries(rebuilt, part)
    assert rebuilt == code
    ast.parse(rebuilt)


def test_chinese_merge_has_no_double_idle_period():
    merged = _merge_two_entries("第一段。", "第二段。第三段。")
    assert "第一段" in merged and "第二段" in merged and "第三段" in merged
    assert "。。" not in merged


def test_plan_a_to_b_merge_keeps_both_values_in_one_entry():
    old_plan = "缓存方案A：使用 Redis 做会话缓存"
    new_plan = "缓存方案A改为B：使用内存缓存替代 Redis"
    merged = _merge_two_entries(new_plan, old_plan)
    assert "方案A改为B" in merged
    assert "内存缓存" in merged and "Redis" in merged


# ---------------------------------------------------------------------------
# ② duplicate threshold
# ---------------------------------------------------------------------------

def test_twenty_same_template_unique_markers_all_recallable():
    client = _MemoryClient()
    facts = [_fact(i) for i in range(1, 21)]
    statuses = [_store_fragment(client, f, "u", source="user") for f in facts]
    assert all(s.get("status") == "stored" for s in statuses), statuses
    assert len(client.memories) == 20, len(client.memories)
    for i in range(1, 21):
        marker = f"编号 {i:04d}"
        hits = client.recall_results(marker, top_k=100, author_id="u")
        assert any(marker in (h.get("content") or "") for h in hits), marker


def test_true_duplicate_still_drops_to_one_row():
    client = _MemoryClient()
    fact = "部署方案：冷层使用进程内 SQLite 引擎存储"
    first = _store_fragment(client, fact, "u", source="user")
    second = _store_fragment(client, fact, "u", source="user")
    assert first["status"] == "stored"
    assert second["status"] == "duplicate", second
    assert len(client.memories) == 1


def test_update_pair_merges_not_duplicates():
    client = _MemoryClient()
    old_plan = "缓存方案A：使用 Redis 做会话缓存"
    new_plan = "缓存方案A改为B：使用内存缓存替代 Redis"
    first = _store_fragment(client, old_plan, "u", source="user")
    second = _store_fragment(client, new_plan, "u", source="user")
    assert first["status"] == "stored"
    assert second["status"] == "updated", second
    assert len(client.memories) == 1
    content = client.memories[0]["content"]
    assert "方案A改为B" in content and "内存缓存" in content and "Redis" in content


# ---------------------------------------------------------------------------
# ③ Search must not refresh last_recalled / must be repeatable
# ---------------------------------------------------------------------------

class _DecayClient:
    """Models the observed bug: bump=True refreshes the DB after the snapshot."""

    def __init__(self):
        now = datetime.now(timezone.utc)
        old = (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ")
        fresh = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        self._rows = [
            {"id": "old", "content": "old memory", "dense_score": 0.9,
             "importance": 0.6, "last_recalled": old, "timestamp": old},
            {"id": "new", "content": "new memory", "dense_score": 0.8,
             "importance": 0.6, "last_recalled": fresh,
             "timestamp": fresh},
        ]

    def recall_results(self, query, top_k=5, author_id=None, bump=True,
                       **kwargs):
        snapshot = [dict(r) for r in self._rows]
        if bump:
            touched = datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ")
            for row in self._rows:
                row["last_recalled"] = touched
        return snapshot


def test_search_same_query_five_times_is_byte_identical(monkeypatch):
    client = _DecayClient()
    monkeypatch.setattr(aml, "_get_client", lambda: client)
    app = aml.mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        runs = []
        for _ in range(5):
            r = c.post("/search", json={
                "query": "memory evidence",
                "user_id": "round3b:repeat",
                "top_k": 2,
            })
            assert r.status_code == 200, r.text
            data = r.json()["data"]
            runs.append([(d["id"], float(d["score"])) for d in data])
    assert all(run == runs[0] for run in runs), runs
    assert [rid for rid, _ in runs[0]] == ["new", "old"]
