#!/usr/bin/env python3
"""L1 优化轮回归（2026-09-19）：Add 的消息 timestamp 进入证据文本。

改动前必红：修改前 aml_add 丢弃 message.timestamp，证据里没有任何日期，
时间类问题（AML 维度 C）无解。这里锁三件事：
  1) 带 timestamp 的消息，写入/检索到的证据文本带 [YYYY-MM-DD HH:MM] 前缀；
  2) 缺失/非法 timestamp（无、字符串、负数、1e30、null、bool）不崩且不加前缀；
  3) 纯函数边界。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import memorycore.aml_server as aml  # noqa: E402

TS = int(datetime(2023, 5, 7, 13, 56, tzinfo=timezone.utc).timestamp() * 1000)


class FakeCold:
    """确定性假冷层（与 test_round3a_contract 同风格，不碰真实存储）"""

    def __init__(self):
        self.memories = []

    def stats(self, all_sessions=False):
        return {"total": len(self.memories)}

    def remember(self, content, importance=0.6, scope="global",
                 author_id=None, source=None, **kwargs):
        mid = f"m{len(self.memories) + 1}"
        self.memories.append({"id": mid, "content": content,
                              "author_id": author_id, "dense_score": 0.9,
                              "importance": importance})
        return {"status": "stored", "memory_id": mid}

    def update(self, memory_id, content, author_id=None, **kwargs):
        for m in self.memories:
            if m.get("id") == memory_id:
                m["content"] = content
        return {"status": "updated", "memory_id": memory_id}

    def recall_results(self, query, top_k=5, author_id=None, **kwargs):
        return [dict(m) for m in self.memories
                if author_id in (None, m.get("author_id"))][:top_k]


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


def test_l1_message_timestamp_becomes_evidence_date_prefix(aml_http):
    c, fake = aml_http
    body = {"request_id": "l1:1", "user_id": "u-l1", "session_id": "s-l1",
            "messages": [{"role": "user",
                          "content": "I went to the support group today.",
                          "timestamp": TS}]}
    r = c.post("/add", json=body)
    assert r.status_code == 200 and r.json()["success"] is True
    assert fake.memories, "应至少写入一条分片"
    assert "[2023-05-07 13:56]" in fake.memories[0]["content"], fake.memories[0]["content"]
    s = c.post("/search", json={"query": "support group", "user_id": "u-l1",
                               "top_k": 5})
    assert s.status_code == 200
    assert "[2023-05-07" in s.json()["data"][0]["content"]


def test_l1_bad_timestamps_are_safe_and_prefixless(aml_http):
    c, fake = aml_http
    msgs = [{"role": "user", "content": "no ts at all"},
            {"role": "user", "content": "string ts", "timestamp": "abc"},
            {"role": "user", "content": "negative ts", "timestamp": -5},
            {"role": "user", "content": "huge ts", "timestamp": 1e30},
            {"role": "user", "content": "null ts", "timestamp": None},
            {"role": "user", "content": "bool ts", "timestamp": True}]
    r = c.post("/add", json={"request_id": "l1:2", "user_id": "u-l1b",
                             "session_id": "s-l1b", "messages": msgs})
    assert r.status_code == 200 and r.json()["success"] is True
    assert fake.memories, "坏 timestamp 也必须正常写入"
    for m in fake.memories:
        assert not m["content"].startswith("["), f"不该有日期前缀: {m['content']!r}"


def test_l1_helper_boundaries():
    assert aml._event_date_prefix(TS) == "[2023-05-07 13:56] "
    assert aml._event_date_prefix(None) == ""
    assert aml._event_date_prefix("abc") == ""
    assert aml._event_date_prefix(-1) == ""
    assert aml._event_date_prefix(True) == ""
    assert aml._event_date_prefix(0) == ""