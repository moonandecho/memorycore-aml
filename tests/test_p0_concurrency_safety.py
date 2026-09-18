#!/usr/bin/env python3
"""P0 regression tests: health isolation, budget/no-half-write, backpressure,
batch/cache consistency, and 64-concurrent Add offloading.

These are intentionally written to fail on the pre-P0 tree:
  * old /health synchronously calls _get_client(); the first test would sleep.
  * old Add writes fragment-by-fragment before any budget check; the second
    test would leave one row behind after the injected budget error.
  * _AdmissionController and embedding.cache_stats did not exist.
  * old route handlers do synchronous work on the event loop; the concurrent
    test would starve /health while 64 Adds are in flight.
"""
import asyncio
import threading
import time
from typing import Any, Dict, List

import pytest
from starlette.testclient import TestClient

import memorycore.aml_server as aml


class _CountingCold:
    """Minimal thread-safe fake cold tier (no ollama, no real embeddings)."""

    def __init__(self, recall_sleep: float = 0.0, write_sleep: float = 0.0,
                 stats_sleep: float = 0.0):
        self.memories: List[Dict[str, Any]] = []
        self.recall_sleep = recall_sleep
        self.write_sleep = write_sleep
        self.stats_sleep = stats_sleep
        self._lock = threading.Lock()

    def stats(self, all_sessions=False):
        if self.stats_sleep:
            time.sleep(self.stats_sleep)
        with self._lock:
            return {"total": len(self.memories)}

    def recall_results(self, query, top_k=5, author_id=None, bump=True, **kwargs):
        if self.recall_sleep:
            time.sleep(self.recall_sleep)
        with self._lock:
            rows = [dict(m) for m in self.memories]
        # Keep the fake deterministic: return nothing unless the caller asks
        # for a top_k larger than a normal dedup probe.
        return rows[:top_k] if top_k and len(rows) else []

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        if self.write_sleep:
            time.sleep(self.write_sleep)
        with self._lock:
            mid = f"m{len(self.memories) + 1}"
            self.memories.append({
                "id": mid, "content": content, "author_id": author_id,
                "dense_score": 0.9, "importance": importance,
            })
        return {"status": "stored", "memory_id": mid}

    def update(self, memory_id, content, author_id=None, **kwargs):
        with self._lock:
            for m in self.memories:
                if m["id"] == memory_id:
                    m["content"] = content
        return {"status": "updated", "memory_id": memory_id}


def _body(rid: str, content: str = "P0 regression fact") -> Dict[str, Any]:
    return {
        "request_id": rid,
        "messages": [{"role": "user", "content": content}],
        "user_id": f"p0:{rid}",
        "session_id": f"p0:{rid}:s",
    }


def _client():
    from starlette.testclient import TestClient
    return TestClient(aml.mcp.streamable_http_app(), raise_server_exceptions=False)


def test_p0_health_is_memory_snapshot_only(monkeypatch):
    """Old /health called _get_client(); this must now be a fast snapshot."""
    called = []
    monkeypatch.setattr(aml, "_get_client",
                        lambda: (called.append(1), time.sleep(5), None)[-1])
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", "/tmp/p0-health-snapshot")
    aml._health_set(status="ok", storage={"total": 0})
    with _client() as c:
        t0 = time.monotonic()
        r = c.get("/health")
        elapsed = time.monotonic() - t0
    assert r.status_code == 200, r.text
    assert elapsed < 0.8, f"/health took {elapsed:.3f}s; touched backend?"
    assert called == [], "health must not call _get_client"


def test_p0_budget_aborts_before_write_and_retry_writes_once(monkeypatch, tmp_path):
    fake = _CountingCold()
    monkeypatch.setattr(aml, "_get_client", lambda: fake)
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    original = aml._precompute_add_plans
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise aml._BudgetExceeded("injected budget error")
        return original(*args, **kwargs)

    monkeypatch.setattr(aml, "_precompute_add_plans", flaky)
    body = _body("p0-budget", "budget-safe unique content")
    with _client() as c:
        r1 = c.post("/add", json=body)
        assert r1.status_code == 503, r1.text
        assert fake.memories == [], "budget failure must not half-write"
        r2 = c.post("/add", json=body)
        assert r2.status_code == 200, r2.text
    assert len(fake.memories) == 1, "retry after safe budget abort writes once"


def test_p0_admission_controller_overload_is_explicit():
    async def scenario():
        ctrl = aml._AdmissionController(
            max_inflight=1, max_queue=0, max_bytes=1024)
        deadline = time.monotonic() + 5
        ok, _ = await ctrl.acquire(10, deadline)
        assert ok
        ok2, reason = await ctrl.acquire(10, deadline)
        assert not ok2 and reason == "overload"
        await ctrl.release(10)

    asyncio.run(scenario())


def test_p0_embedding_cache_batch_matches_single(monkeypatch):
    from mnemosyne.core import embeddings as emb
    import numpy as np

    monkeypatch.setattr(emb, "_is_api_model", lambda _model: True)
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_API_URL", "http://p0-cache.test/v1")

    def fake_api(texts):
        return np.array([[float(sum(map(ord, t))), float(len(t) % 17), 1.0]
                         for t in texts], dtype=np.float32)

    monkeypatch.setattr(emb, "_embed_api", fake_api)
    texts = ["p0 cache alpha", "p0 cache beta", "p0 cache gamma"]

    monkeypatch.setenv("MNEMOSYNE_EMBED_CACHE_SIZE", "0")
    emb.cache_clear()
    uncached = emb.embed(texts)

    monkeypatch.setenv("MNEMOSYNE_EMBED_CACHE_SIZE", "64")
    emb.cache_clear()
    singles = np.stack([emb.embed([t])[0] for t in texts])
    stats_before = emb.cache_stats()
    batch = emb.embed(texts)
    assert batch is not None
    assert float(np.max(np.abs(batch - singles))) == 0.0
    assert uncached is not None
    assert float(np.max(np.abs(batch - uncached))) == 0.0, \
        "cache hit must return the same vector as an uncached call"
    singles_again = np.stack([emb.embed([t])[0] for t in texts])
    assert float(np.max(np.abs(singles_again - batch))) == 0.0
    stats = emb.cache_stats()
    assert stats["enabled"] is True
    assert stats["entries"] >= len(texts)
    assert stats["hits"] > stats_before["hits"], "second pass must hit cache"


def test_p0_concurrent_64_does_not_wedge_health(monkeypatch, tmp_path):
    """64 concurrent Adds must not starve /health or leave requests hanging."""
    import httpx2

    fake = _CountingCold(stats_sleep=1.5)
    monkeypatch.setattr(aml, "_get_client", lambda: fake)
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    aml._health_set(status="ok", storage={"total": 0})

    async def scenario():
        app = aml.mcp.streamable_http_app()
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
                transport=transport, base_url="http://p0-test") as client:
            tasks = [
                asyncio.create_task(
                    client.post("/add", json=_body(f"p0-conc-{i}",
                                                   f"concurrent fact {i}")))
                for i in range(64)
            ]
            await asyncio.sleep(0.2)
            t0 = time.monotonic()
            health = await asyncio.wait_for(client.get("/health"), timeout=1.0)
            health_elapsed = time.monotonic() - t0
            responses = await asyncio.gather(*tasks)
            return health, health_elapsed, responses

    health, health_elapsed, responses = asyncio.run(scenario())
    assert health.status_code == 200, health.text
    assert health_elapsed < 1.0, f"health starved for {health_elapsed:.3f}s"
    assert len(responses) == 64
    assert all(r.status_code == 200 for r in responses), [
        (r.status_code, r.text[:120]) for r in responses if r.status_code != 200
    ][:3]
