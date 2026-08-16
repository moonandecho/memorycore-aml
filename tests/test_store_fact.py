#!/usr/bin/env python3
"""tests/test_store_fact.py — write-entry linkage (state -> cold / rule -> stamp)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore import server  # noqa: E402  (@mcp.tool returns the plain function)

from conftest import MockMnemosyneClient  # noqa: E402


def _patch_server(tmp_store, mock_client):
    """Monkeypatch the server module singletons onto the temp store."""
    server._store = tmp_store
    server._client = mock_client


def test_store_fact_state_goes_cold(tmp_store, mock_client):
    """Historical decision/status record -> cold_stored, hot tier stays empty."""
    _patch_server(tmp_store, mock_client)
    r = json.loads(server.memorycore_store_fact(
        "2026-08-15 拍板: GPU 压测方案定稿, 不再更换方案", importance=0.8))
    assert r["status"] == "cold_stored", r
    assert tmp_store.entries("memory") == []
    assert mock_client.stored, "must be written to the cold tier"


def test_store_fact_rule_stamps_metadata(tmp_store, mock_client, meta_for):
    """Precept -> stored in hot tier + sidecar stamp {rule, origin=store_fact}."""
    _patch_server(tmp_store, mock_client)
    r = json.loads(server.memorycore_store_fact(
        "用户偏好: 极简选型, Go/Rust 单二进制", importance=0.8))
    assert r["status"] == "stored", r
    assert len(tmp_store.entries("memory")) == 1
    m = meta_for("memory").get_entry("用户偏好: 极简选型, Go/Rust 单二进制")
    assert m and m["type"] == "rule" and m["origin"] == "store_fact"


def test_import_server_ok():
    """Import smoke: server module exposes the expected tools."""
    assert hasattr(server, "memorycore_store_fact")
    assert hasattr(server, "memorycore_memory_audit")
