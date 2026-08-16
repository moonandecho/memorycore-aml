#!/usr/bin/env python3
"""tests/conftest.py — isolated test infrastructure (tempfile + mock, never
touches real memory files).

Run: cd <repo> && .venv/bin/python -m pytest tests/ -v
"""
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Plugin smoke tests load hermes-plugin/memorycore-prefetch which depends on
# the Hermes host runtime (agent.memory_provider). This host has no Hermes,
# so install a minimal mock module — keeps every import chain working with
# zero host dependencies.
if "agent.memory_provider" not in sys.modules:
    agent_pkg = types.ModuleType("agent")
    mp_mod = types.ModuleType("agent.memory_provider")

    class MemoryProvider:
        pass

    mp_mod.MemoryProvider = MemoryProvider
    agent_pkg.memory_provider = mp_mod
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.memory_provider"] = mp_mod

from memorycore.local_store import LocalStore  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402


def days_ago_str(n: int) -> str:
    """Date string n days ago (test anchor for embedded dates)."""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


class MockMnemosyneClient:
    """Mock cold tier: records calls; supports fault injection."""

    def __init__(self, cold_items=None, fail_remember=False, fail_recall=False):
        self._cold = list(cold_items or [])
        self._fail_remember = fail_remember
        self._fail_recall = fail_recall
        self.recall_queries = []
        self.stored = []
        self.updated = []
        self.forgotten = []
        self._next_id = 0

    def recall_results(self, query, top_k=5):
        if self._fail_recall:
            raise RuntimeError("cold tier unreachable (mock)")
        self.recall_queries.append(query)
        return [{"id": f"c{i}", "content": c.get("content"),
                 "dense_score": c.get("dense_score", 0.0)}
                for i, c in enumerate(self._cold)]

    def remember(self, content, importance=0.6, scope="global"):
        if self._fail_remember:
            return {"status": "error", "error": "cold write failed (mock)"}
        self._next_id += 1
        self.stored.append(content)
        self._cold.append({"content": content, "dense_score": 0.5})
        return {"status": "stored", "memory_id": f"m{self._next_id}"}

    def update(self, memory_id, content, importance=None):
        self.updated.append((memory_id, content))
        return {"status": "updated"}

    def forget(self, memory_id):
        self.forgotten.append(memory_id)
        return {"status": "ok"}

    def stats(self):
        return {"total": len(self._cold)}


@pytest.fixture
def tmp_store(tmp_path):
    """Temp-dir LocalStore (MEMORY.md / USER.md), isolated from real files."""
    return LocalStore(tmp_path / "MEMORY.md", tmp_path / "USER.md")


@pytest.fixture
def meta_for(tmp_store):
    """Build a MetaStore that follows the tmp_store paths."""
    def _make(target):
        return MetaStore(target, memory_path=tmp_store.memory_path,
                         user_path=tmp_store.user_path)
    return _make


@pytest.fixture
def mock_client():
    return MockMnemosyneClient()
