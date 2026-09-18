#!/usr/bin/env python3
"""fix-3c: AML Search must be a read-only recall.

Two regression tests:

1. POST /search must not change ``working_memory.last_recalled`` or
   ``working_memory.recall_count`` (byte-for-byte DB comparison).
2. Repeated /search calls for the same query must return identical ids,
   order, and scores, even when a matching memory is in the decayed state
   (old timestamp, ``last_recalled IS NULL``).

The real local mnemosyne engine is used.  A tiny local OpenAI-compatible
embedding server makes the test deterministic and independent of ollama.
The tests drop the ``wm_ai`` trigger before seeding: SQLite 3.45 in this
environment raises SQLITE_CANTOPEN for the trigger's FTS5 INSERT on a fresh
BEAM schema (an independent FTS5/index issue, not the recall-readonly
behavior under test).  Recall and the tracking-column UPDATE path do not
depend on that insert trigger.
"""
import json
import math
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from memorycore import aml_server
from memorycore.cold_store_client import ColdStoreClient


USER_ID = "fix3c-user"
QUERY = "ancient oak winter"
_VECTOR_DIM = 1024
_OLD_TS = "2020-01-01T00:00:00"


def _unit_vector(text: str):
    """Return a deterministic, dense, non-negative vector.

    A constant query/doc vector is enough for this regression: it ensures
    the row is always a vector candidate, so the test is about the DB
    tracking columns and score stability, not ranking quality.
    """
    del text  # intentionally content-independent; ranking quality is not under test
    value = 1.0 / math.sqrt(_VECTOR_DIM)
    return [value] * _VECTOR_DIM


class _EmbeddingHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802 - stdlib handler API
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
        except Exception:
            body = {}
        inputs = body.get("input")
        if isinstance(inputs, str):
            inputs = [inputs]
        vectors = [_unit_vector(str(text)) for text in (inputs or [""])]
        payload = json.dumps({
            "data": [
                {"embedding": vec, "index": idx}
                for idx, vec in enumerate(vectors)
            ]
        }).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence the test server
        pass


@pytest.fixture
def embedding_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _EmbeddingHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def _make_local_client(monkeypatch, tmp_path: Path, embedding_port: int):
    """Create a real local ColdStoreClient pointed at *tmp_path*.

    The per-user engine is created before dropping ``wm_ai`` so later
    ``client.remember`` calls reuse the same engine and do not run
    ``init_beam`` again (which would recreate the trigger).
    """
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv(
        "MNEMOSYNE_EMBEDDING_API_URL",
        f"http://127.0.0.1:{embedding_port}/v1",
    )
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
    monkeypatch.setenv("MNEMOSYNE_EMBEDDING_DIM", str(_VECTOR_DIM))
    monkeypatch.setenv("MNEMOSYNE_ENHANCED_RECALL", "0")
    monkeypatch.setenv("MNEMOSYNE_POLYPHONIC_RECALL", "0")

    client = ColdStoreClient()
    user_engine = client._backend._engine_for(USER_ID)
    # Work around an unrelated SQLite/FTS5 trigger failure on a fresh
    # schema; the written row is manually copied into fts_working below.
    user_engine.beam.conn.execute("DROP TRIGGER IF EXISTS wm_ai")
    user_engine.beam.conn.commit()
    return client, user_engine


def _sync_fts(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM fts_working")
    conn.execute(
        "INSERT INTO fts_working(id, content) "
        "SELECT id, content FROM working_memory"
    )
    conn.commit()


def _read_tracking(conn: sqlite3.Connection):
    return [
        (row[0], row[1], row[2])
        for row in conn.execute(
            "SELECT id, last_recalled, recall_count "
            "FROM working_memory ORDER BY id"
        ).fetchall()
    ]


def _search(client, query=QUERY, top_k=5):
    app = aml_server.mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as http:
        return http.post("/search", json={
            "user_id": USER_ID,
            "query": query,
            "top_k": top_k,
        })


@pytest.fixture
def local_aml_client(monkeypatch, tmp_path, embedding_server):
    client, user_engine = _make_local_client(monkeypatch, tmp_path, embedding_server)
    monkeypatch.setattr(aml_server, "_get_client", lambda: client)
    db_path = Path(user_engine.db_path)
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = None
    try:
        yield client, conn
    finally:
        conn.close()


def test_search_does_not_change_last_recalled_or_recall_count(
        local_aml_client, monkeypatch):
    client, conn = local_aml_client
    stored = client.remember(
        "The ancient oak tree remembers every winter.",
        importance=0.6,
        author_id=USER_ID,
    )
    assert stored.get("status") == "stored", stored
    _sync_fts(conn)
    conn.execute(
        "UPDATE working_memory SET timestamp = ?, last_recalled = NULL, "
        "recall_count = 0",
        (_OLD_TS,),
    )
    conn.commit()

    before = _read_tracking(conn)
    assert before == [(stored["memory_id"], None, 0)]

    response = _search(client)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload.get("data"), payload

    after = _read_tracking(conn)
    assert after == before, (before, after)


def test_search_is_byte_stable_across_five_calls(local_aml_client,
                                                monkeypatch):
    client, conn = local_aml_client
    rows = [
        ("ancient oak winter memory alpha", 0.6, "2020-01-01T00:00:00"),
        ("ancient oak winter memory beta", 0.6, "2024-01-01T00:00:00"),
    ]
    stored_ids = []
    for content, importance, _ts in rows:
        result = client.remember(
            content, importance=importance, author_id=USER_ID)
        assert result.get("status") == "stored", result
        stored_ids.append(result["memory_id"])
    _sync_fts(conn)
    for memory_id, (_content, _importance, ts) in zip(stored_ids, rows):
        conn.execute(
            "UPDATE working_memory SET timestamp = ?, last_recalled = NULL, "
            "recall_count = 0 WHERE id = ?",
            (ts, memory_id),
        )
    conn.commit()

    before = _read_tracking(conn)
    assert before == [(mid, None, 0) for mid in sorted(stored_ids)]

    payloads = []
    for _ in range(5):
        response = _search(client)
        assert response.status_code == 200, response.text
        payloads.append(response.json())

    assert all(payload == payloads[0] for payload in payloads), payloads
    data = payloads[0].get("data") or []
    assert data, payloads[0]
    # The exact ids returned must themselves be identical across calls.
    id_sequences = [[item["id"] for item in p.get("data", [])]
                    for p in payloads]
    assert all(seq == id_sequences[0] for seq in id_sequences), id_sequences

    after = _read_tracking(conn)
    assert after == before, (before, after)
