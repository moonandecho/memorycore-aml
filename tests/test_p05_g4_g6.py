#!/usr/bin/env python3
"""P0.5 G4/G6 regressions.

Every test here is written against the review's counterexamples and must be red
on the pre-P0.5 tree/implementation:

* Phase A dedup recall must pass bump=False (probe_phase_a_real_bump).
* resume-all-done must not call _get_client/Phase A (probe_resume_all_done).
* Phase B must reuse Phase A plans (probe_duplicate_planning).
* prefetch/query-LRU must not poison None and must key by model fingerprint.
* body admission must happen before request.json() (review §2.6).
* LocalBackend must detect DB inode split.
* a committed fragment before its ledger checkpoint must replay idempotently
  (probe_ledger_crash).
"""
import asyncio
import json
import os
import sqlite3
import threading
import time
from typing import Any, Dict, List

import numpy as np
import pytest

import memorycore.aml_server as aml
from memorycore.cold_store_client import LocalBackend
from mnemosyne.core import embeddings as emb


# ---------------------------------------------------------------------------
# Test doubles / helpers
# ---------------------------------------------------------------------------

class _BumpProbe:
    def __init__(self):
        self.bumps: List[bool] = []

    def recall_results(self, query, top_k=5, author_id=None,
                       bump=True, **kwargs):
        self.bumps.append(bump)
        return []


class _PlanReuseProbe:
    def __init__(self):
        self.recall_calls = 0
        self.remember_calls: List[str] = []

    def recall_results(self, query, top_k=5, author_id=None,
                       bump=True, **kwargs):
        self.recall_calls += 1
        return []

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        self.remember_calls.append(content)
        return {"status": "stored", "memory_id": f"m{len(self.remember_calls)}"}

    def update(self, memory_id, content, author_id=None, **kwargs):
        return {"status": "updated", "memory_id": memory_id}


class _CrashReplayProbe:
    """Models BeamMemory exact-dedup-update and a recall false negative."""

    def __init__(self, content: str, user_id: str):
        self.rows = [{
            "id": "m1", "content": content, "author_id": user_id,
            "timestamp": 1000.0, "source": "user",
        }]
        self.dedup_updates = 0
        self.remember_calls = 0

    def find_exact(self, content, author_id=None, **kwargs):
        for row in self.rows:
            if row["content"] == content and row["author_id"] == author_id:
                return row["id"]
        return None

    def recall_results(self, query, top_k=5, author_id=None,
                       bump=True, **kwargs):
        # Deliberate counterexample: recall gates miss the committed row.
        return []

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        self.remember_calls += 1
        for row in self.rows:
            if row["content"] == content and row["author_id"] == author_id:
                row["timestamp"] = 2000.0
                self.dedup_updates += 1
                return {"status": "stored", "memory_id": row["id"]}
        self.rows.append({"id": f"m{len(self.rows) + 1}",
                          "content": content, "author_id": author_id,
                          "timestamp": 2000.0, "source": source})
        return {"status": "stored", "memory_id": self.rows[-1]["id"]}

    def update(self, memory_id, content, author_id=None, **kwargs):
        return {"status": "updated", "memory_id": memory_id}


def _body(rid: str = "p05-1", user_id: str = "u-p05",
          content: str = "P0.5 crash replay fact") -> Dict[str, Any]:
    return {
        "request_id": rid,
        "user_id": user_id,
        "session_id": f"{user_id}:s",
        "messages": [{"role": "user", "content": content}],
    }


def _set_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    return str(tmp_path)


def _seed_ledger(body: Dict[str, Any], tmp_path: str, *,
                 phase: str, completed: int, total: int) -> str:
    monkeypatch_dir = os.environ.get("MNEMOSYNE_DATA_DIR", str(tmp_path))
    conn = aml._ledger_connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO aml_request_ledger "
            "(request_id, body_hash, status_code, response_json, updated_at, "
            "phase, checkpoint) VALUES (?, ?, 0, '', ?, ?, ?)",
            (body["request_id"], aml._body_hash(body), time.time() - 400,
             phase, json.dumps({"total": total, "completed": completed})),
        )
        conn.commit()
    finally:
        conn.close()
    return monkeypatch_dir


class _AdmissionOrderRequest:
    def __init__(self):
        self.headers = {"content-length": "32"}
        self.json_called = False
        self.stream_called = False

    async def json(self):
        self.json_called = True
        return _body("p05-body")

    async def stream(self):
        self.stream_called = True
        yield json.dumps(_body("p05-body")).encode()


# ---------------------------------------------------------------------------
# 1. Phase A must be read-only (bump=False)
# ---------------------------------------------------------------------------

def test_phase_a_dedup_recall_passes_bump_false():
    probe = _BumpProbe()
    aml._dedup_recall(probe, "some content", "u")
    assert probe.bumps == [False], (
        "Phase A dedup recall must be read-only; got bumps=%r" % probe.bumps)


# ---------------------------------------------------------------------------
# 2. resume-all-done short-circuits Phase A/B and replay/finalize 200
# ---------------------------------------------------------------------------

def test_resume_all_done_does_not_touch_client(monkeypatch, tmp_path):
    _set_data_dir(monkeypatch, tmp_path)
    body = _body("p05-resume-all")
    _seed_ledger(body, str(tmp_path), phase="retryable_partial",
                 completed=1, total=1)

    def fail_get_client():
        raise AssertionError("resume-all-done must not call _get_client()")

    monkeypatch.setattr(aml, "_get_client", fail_get_client)
    resp = aml._aml_add_sync_locked(body, time.monotonic() + 10)
    assert resp.status_code == 200, resp.body
    payload = json.loads(resp.body)
    assert payload["success"] is True

    conn = aml._ledger_connect()
    try:
        row = conn.execute(
            "SELECT phase, status_code FROM aml_request_ledger "
            "WHERE request_id = ?", (body["request_id"],)).fetchone()
    finally:
        conn.close()
    assert row[0] == "done" and int(row[1]) == 200


# ---------------------------------------------------------------------------
# 3. Phase B must reuse the Phase A plan (one plan, one recall, one write)
# ---------------------------------------------------------------------------

def test_phase_b_reuses_phase_a_plan(monkeypatch, tmp_path):
    _set_data_dir(monkeypatch, tmp_path)
    probe = _PlanReuseProbe()
    monkeypatch.setattr(aml, "_get_client", lambda: probe)

    plan_calls = {"n": 0}
    original_plan = aml._plan_fragment

    def counting_plan(client, content, user_id):
        plan_calls["n"] += 1
        return original_plan(client, content, user_id)

    monkeypatch.setattr(aml, "_plan_fragment", counting_plan)
    body = _body("p05-plan")
    resp = aml._aml_add_sync_locked(body, time.monotonic() + 10)
    assert resp.status_code == 200, resp.body
    assert plan_calls["n"] == 1, (
        "write phase must not replan; _plan_fragment calls=%d"
        % plan_calls["n"])
    assert probe.recall_calls == 1, (
        "write phase must not re-run dedup recall; calls=%d"
        % probe.recall_calls)
    assert probe.remember_calls == ["P0.5 crash replay fact"]


# ---------------------------------------------------------------------------
# 4. Explicit prefetch survives MNEMOSYNE_EMBED_CACHE_SIZE=0
# ---------------------------------------------------------------------------

def test_prefetch_survives_disabled_normal_cache(monkeypatch):
    monkeypatch.setattr(emb, "_is_api_model", lambda _model: True)
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_API_URL", "http://p05-prefetch.test/v1")
    monkeypatch.setenv("MNEMOSYNE_EMBED_CACHE_SIZE", "0")
    calls: List[List[str]] = []

    def fake_api(texts):
        calls.append(list(texts))
        return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(emb, "_embed_api", fake_api)
    emb.cache_clear()

    vecs = emb.prefetch_embeddings(["prefetched text"], kind="doc")
    assert vecs is not None
    assert emb.embeddings_cached(["prefetched text"], kind="doc") is True
    again = emb.embed(["prefetched text"])
    assert again is not None
    assert calls == [["prefetched text"]], (
        "write-phase embed must reuse the prefetched vector, not call the "
        "API again; calls=%r" % calls)


# ---------------------------------------------------------------------------
# 5. query LRU must not poison None / fingerprint must include digest
# ---------------------------------------------------------------------------

def test_embed_query_does_not_cache_none(monkeypatch):
    monkeypatch.setattr(emb, "_is_api_model", lambda _model: True)
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_API_URL", "http://p05-query.test/v1")
    calls: List[List[str]] = []

    def flaky_api(texts):
        calls.append(list(texts))
        if len(calls) == 1:
            return None
        return np.ones((len(texts), 3), dtype=np.float32)

    monkeypatch.setattr(emb, "_embed_api", flaky_api)
    emb.cache_clear()
    assert emb.embed_query("transient failure query") is None
    got = emb.embed_query("transient failure query")
    assert got is not None, "None was poison-cached by embed_query lru"
    assert len(calls) == 2, calls


def test_model_fingerprint_cache_key_includes_digest(monkeypatch):
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_API_URL", "http://p05-fp.test/v1")
    emb._MODEL_FINGERPRINT_CACHE = None
    emb._MODEL_FINGERPRINT_KEY = None
    monkeypatch.setenv("MNEMOSYNE_EMBED_MODEL_DIGEST", "digest-one")
    first = emb._model_fingerprint()
    monkeypatch.setenv("MNEMOSYNE_EMBED_MODEL_DIGEST", "digest-two")
    second = emb._model_fingerprint()
    assert first["digest"] == "digest-one"
    assert second["digest"] == "digest-two"
    assert first != second, "fingerprint cache ignored digest change"


# ---------------------------------------------------------------------------
# 6. Body cap/admission must happen before request.json()
# ---------------------------------------------------------------------------

def test_body_admission_before_json_parse(monkeypatch):
    request = _AdmissionOrderRequest()

    async def reject_acquire(_nbytes, _deadline):
        return False, "overload"

    monkeypatch.setattr(aml._ADMISSION, "acquire", reject_acquire)
    resp = asyncio.run(aml.aml_add(request))
    assert resp.status_code == 503
    assert request.json_called is False, (
        "request.json() ran before admission; body could be materialised "
        "outside the byte budget")
    assert request.stream_called is False


# ---------------------------------------------------------------------------
# 7. DB identity guard detects replacement and exposes the marker
# ---------------------------------------------------------------------------

def test_local_backend_identity_guard_detects_replacement(monkeypatch, tmp_path):
    db_path = tmp_path / "mnemosyne.db"
    db_path.write_bytes(b"old-db")

    class _FakeEngine:
        def __init__(self):
            self.db_path = str(db_path)
            self.conn = None

    backend = object.__new__(LocalBackend)
    backend._engine = _FakeEngine()
    backend._db_path = str(db_path)
    backend._db_identity = {"path": str(db_path), "st_dev": -1, "st_ino": -1}
    backend._identity_mismatch_count = 0
    backend._identity_last_mismatch_at = None
    backend._identity_last_before = None
    backend._aml_engines = {}
    backend._aml_engine_locks = {}
    backend._aml_lock = threading.Lock()
    backend._engine_lock = threading.RLock()

    created = []

    class _NewEngine(_FakeEngine):
        def __init__(self, session_id=None):
            super().__init__()
            created.append(session_id)

    import mnemosyne
    monkeypatch.setattr(mnemosyne, "Mnemosyne", _NewEngine)

    info = backend.check_data_identity()
    assert info["identity_mismatch"] is True
    assert info["identity_mismatch_count"] == 1
    assert created == ["memorycore"]


# ---------------------------------------------------------------------------
# 8. commit-before-checkpoint replay must be byte-identical
# ---------------------------------------------------------------------------

def test_commit_before_checkpoint_replay_is_idempotent(monkeypatch, tmp_path):
    _set_data_dir(monkeypatch, tmp_path)
    body = _body("p05-crash-replay", content="Crash window content 12345.")
    probe = _CrashReplayProbe(body["messages"][0]["content"], body["user_id"])
    monkeypatch.setattr(aml, "_get_client", lambda: probe)
    _seed_ledger(body, str(tmp_path), phase="writing",
                 completed=0, total=1)

    resp = aml._aml_add_sync_locked(body, time.monotonic() + 10)
    assert resp.status_code == 200, resp.body
    assert probe.dedup_updates == 0, (
        "replay called remember() on an already committed fragment and "
        "refreshed its timestamp")
    assert probe.rows[0]["timestamp"] == 1000.0
    assert probe.remember_calls == 0


# ---------------------------------------------------------------------------
# 9. stats.embeddings must count both vector stores
# ---------------------------------------------------------------------------

class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return list(self._rows)


class _StatsConn:
    def execute(self, sql, params=()):
        compact = " ".join(sql.split())
        if compact.startswith("SELECT COUNT(*) FROM working_memory"):
            return _Cursor([(1,)])
        if "SELECT id, rowid FROM working_memory" in compact:
            return _Cursor([("m1", 1)])
        if "SELECT memory_id FROM memory_embeddings" in compact:
            return _Cursor([("m1",)])
        if "SELECT rowid FROM vec_working_rowids" in compact:
            return _Cursor([])  # vec store empty; legacy has the vector
        if compact.startswith("SELECT COUNT(*) FROM episodic_memory"):
            return _Cursor([(0,)])
        return _Cursor([])


class _StatsEngine:
    def __init__(self):
        self.conn = _StatsConn()
        self.db_path = "/tmp/p05-stats/mnemosyne.db"


def test_stats_embeddings_counts_legacy_and_vec_stores():
    backend = object.__new__(LocalBackend)
    backend._engine = _StatsEngine()
    stats = backend.stats(all_sessions=True)
    assert stats["memory_embeddings"] == 1
    assert stats["vec_working"] == 0
    assert stats["embeddings"] == 1, (
        "stats.embeddings ignored memory_embeddings when vec_working was "
        "empty: %r" % stats)
