#!/usr/bin/env python3
"""tests/test_store_fact.py — 写入口联动 (state 直冷 / rule 盖章)。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore import server  # noqa: E402  (venv: mcp + fastmcp 可用, 装饰器返回原函数)

from conftest import MockMnemosyneClient  # noqa: E402


def _patch_server(tmp_store, mock_client):
    """monkeypatch server 模块级实例 → 临时目录 (不碰生产 MEMORY.md)。"""
    server._store = tmp_store
    server._client = mock_client


def test_store_fact_state_goes_cold(tmp_store, mock_client):
    """历史决策/状态记录 (含拍板等完成态词) → cold_stored, 热层零条目。"""
    _patch_server(tmp_store, mock_client)
    r = json.loads(server.memorycore_store_fact(
        "2026-08-15 拍板: GPU 压测方案定稿, 不再更换方案", importance=0.8))
    assert r["status"] == "cold_stored", r
    assert tmp_store.entries("memory") == []
    assert mock_client.stored, "应已写冷层"


def test_store_fact_rule_stamps_metadata(tmp_store, mock_client, meta_for):
    """准则 → 热层 stored + sidecar 盖章 {rule, origin=store_fact}。"""
    _patch_server(tmp_store, mock_client)
    r = json.loads(server.memorycore_store_fact(
        "用户偏好: 极简选型, Go/Rust 单二进制", importance=0.8))
    assert r["status"] == "stored", r
    assert len(tmp_store.entries("memory")) == 1
    m = meta_for("memory").get_entry("用户偏好: 极简选型, Go/Rust 单二进制")
    assert m and m["type"] == "rule" and m["origin"] == "store_fact"


def test_import_server_ok():
    """验收 4: python3 -c 'import server' 可正常导入 (venv)。"""
    assert hasattr(server, "memorycore_store_fact")
    assert hasattr(server, "memorycore_memory_audit")
