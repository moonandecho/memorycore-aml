#!/usr/bin/env python3
"""tests/test_recall_channel.py — P0 召回 channels additive 透传/探针接入。

验收:
  * 每条结果新增 keyword_score/fts_score/channel, 不改顺序 (_apply_decay 仍是
    唯一排序来源; 构造 K 分高 dense 分低的 mock 验证);
  * 缺字段/非有限值容错为 0, 本地 bigram 证据兜底为 K;
  * H=handle 直查, H>K>S;
  * bump=False / top_k 上限 / 探针开关关闭零落盘且不影响 restore 调用;
  * server 接入探针: 字段齐且无 query 明文。
"""
import hashlib
import json

from memorycore import server
from memorycore.core import recall_probe
from memorycore.core.decay import _apply_decay


class _RecallClient:
    """按固定顺序返回候选的只读 mock; 记录 bump/top_k 调用。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]
        self.calls = []
        self.bumps = []
        self.restore_calls = 0

    def recall_results(self, query, top_k=5, bump=True):
        self.calls.append((query, top_k))
        self.bumps.append(bump)
        return [dict(it) for it in self.items]


def _patch(tmp_store, client):
    server._store = tmp_store
    server._client = client


def test_missing_keyword_fts_order_identical_to_decay(tmp_store):
    """dense-only 冷层: 返回顺序 == _apply_decay 顺序, K/S 字段 additive。"""
    items = [
        {"id": "c1", "content": "甲", "dense_score": 0.40,
         "importance": 0.9},
        {"id": "c2", "content": "乙", "dense_score": 0.50,
         "importance": 0.9},
        {"id": "c3", "content": "丙", "dense_score": 0.45,
         "importance": 0.9},
    ]
    expected = [r["id"] for r in _apply_decay([dict(it) for it in items])]
    client = _RecallClient(items)
    _patch(tmp_store, client)
    data = json.loads(server.memorycore_recall("合成查询", top_k=5))
    assert [r["id"] for r in data["results"]] == expected
    for row in data["results"]:
        assert row["keyword_score"] == 0.0
        assert row["fts_score"] == 0.0
    assert client.bumps == [False], "bump=False 只读契约保持"
    assert client.calls == [("合成查询", 5)]


def test_high_keyword_low_dense_does_not_change_order(tmp_store):
    """K 分高 dense 分低 -> 仍按 final_score 排序, channel=K 不得升权/过滤。"""
    items = [
        {"id": "dense-high", "content": "甲", "dense_score": 0.99,
         "importance": 0.9, "keyword_score": 0.0},
        {"id": "kw-huge", "content": "乙", "dense_score": 0.10,
         "importance": 0.9, "keyword_score": 9.9, "fts_score": 0.0},
        {"id": "dense-mid", "content": "丙", "dense_score": 0.50,
         "importance": 0.9, "keyword_score": 0.0},
    ]
    client = _RecallClient(items)
    _patch(tmp_store, client)
    data = json.loads(server.memorycore_recall("合成查询", top_k=5))
    got = [r["id"] for r in data["results"]]
    assert got == ["dense-high", "dense-mid", "kw-huge"], got
    by_id = {r["id"]: r for r in data["results"]}
    assert by_id["kw-huge"]["channel"] == "K"
    assert by_id["dense-high"]["channel"] == "S"


def test_keyword_fts_channel_k_and_scores_additive(tmp_store):
    items = [
        {"id": "dense_only", "content": "甲", "dense_score": 0.90,
         "importance": 0.9},
        {"id": "kw", "content": "乙", "dense_score": 0.50,
         "importance": 0.9, "keyword_score": 0.42},
        {"id": "fts", "content": "丙", "dense_score": 0.50,
         "importance": 0.9, "fts_score": 0.37},
        {"id": "bad", "content": "丁", "dense_score": 0.50,
         "importance": 0.9, "keyword_score": "oops", "fts_score": None},
    ]
    _patch(tmp_store, _RecallClient(items))
    data = json.loads(server.memorycore_recall("合成查询", top_k=5))
    by_id = {r["id"]: r for r in data["results"]}
    assert by_id["dense_only"]["channel"] == "S"
    assert by_id["kw"]["channel"] == "K"
    assert by_id["fts"]["channel"] == "K"
    assert by_id["bad"]["channel"] == "S"
    assert by_id["kw"]["keyword_score"] == 0.42
    assert by_id["fts"]["fts_score"] == 0.37
    assert by_id["bad"]["keyword_score"] == 0.0
    assert by_id["bad"]["fts_score"] == 0.0


def test_local_bigram_evidence_marks_k_without_cold_scores(tmp_store):
    class _Client:
        def recall_results(self, query, top_k=5, bump=True):
            return [{"id": "dense-only", "content": "project atlas one",
                     "dense_score": 0.5, "importance": 0.5}]

    _patch(tmp_store, _Client())
    data = json.loads(server.memorycore_recall("project atlas", top_k=3))
    row = data["results"][0]
    assert row["keyword_score"] == 0.0
    assert row["fts_score"] == 0.0
    assert row["channel"] == "K", "旧冷层仅 dense + 本地 bigram 命中必须标 K"


def test_handle_direct_result_channel_h(tmp_store, meta_for):
    stub = "[规则指针]主题甲→recall:0123456789abcdef"
    tmp_store.add("memory", stub)
    meta_for("memory").stamp(stub, "stub", origin="stub_sink",
                             cold_id="cold-handle-1", handle="#h1")
    _patch(tmp_store, _RecallClient([
        {"id": "cold-handle-1", "content": "", "dense_score": 0.9,
         "importance": 0.8},
    ]))
    data = json.loads(server.memorycore_recall(
        "主题甲", top_k=3, handle="#h1"))
    assert data["mode"] == "handle"
    assert data["page_fault"] is True
    row = data["results"][0]
    assert row["id"] == "cold-handle-1"
    assert row["channel"] == "H"
    assert row["handle"] == "#h1"


def test_probe_off_zero_write_on_writes_full_event_no_plaintext(
        tmp_store, tmp_path, monkeypatch):
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE", raising=False)
    _patch(tmp_store, _RecallClient([
        {"id": "c1", "content": "甲", "dense_score": 0.8,
         "importance": 0.9, "keyword_score": 0.3},
        {"id": "c2", "content": "乙", "dense_score": 0.4,
         "importance": 0.9},
    ]))
    server.memorycore_recall("探针明文-不得落盘", top_k=2)
    assert not target.exists(), "开关关必须零文件创建"

    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    recall_probe.reset_probe_metrics()
    query = "探针明文-不得落盘"
    response = json.loads(server.memorycore_recall(query, top_k=2))
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["source"] == "server_recall"
    assert event["query_sha256"] == hashlib.sha256(
        query.encode("utf-8")).hexdigest()
    assert event["query_len"] == len(query)
    assert event["top_k"] == 2
    assert event["candidate_count"] == 2
    assert event["returned_ids"] == [r["id"] for r in response["results"]]
    assert event["dense_scores"] == [0.8, 0.4]
    assert event["keyword_scores"] == [0.3, 0.0]
    assert event["fts_scores"] == [0.0, 0.0]
    assert event["channel"] == ["K", "S"]
    assert event["selected"] == [True, True]
    assert event["error"] is None
    raw = target.read_text(encoding="utf-8")
    assert query not in raw
    assert recall_probe.get_probe_metrics() == {
        "attempts": 1, "written": 1, "errors": 0, "dropped": 0}


def test_top_k_clamped_to_10_and_min_1(tmp_store):
    client = _RecallClient([])
    _patch(tmp_store, client)
    server.memorycore_recall("查询", top_k=99)
    server.memorycore_recall("查询", top_k=0)
    assert client.calls == [("查询", 10), ("查询", 1)]


def test_recall_error_branch_returns_error_json(tmp_store, monkeypatch):
    class _Broken:
        def recall_results(self, query, top_k=5, bump=True):
            raise RuntimeError("cold unreachable")

    _patch(tmp_store, _Broken())
    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE", raising=False)
    data = json.loads(server.memorycore_recall("查询", top_k=3))
    assert "error" in data and "results" not in data
