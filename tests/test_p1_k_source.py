#!/usr/bin/env python3
"""tests/test_p1_k_source.py — P1-A: 探针事件 k_source 证据来源。

验收对应:
  * cold_kw / cold_fts / cold_kw_fts / local_lex / 空串 五种取值;
  * 旧 mock 只给 dense_score (字段缺失) 不炸;
  * k_source 只进探针事件, 不进 MCP 返回契约;
  * channel 仍按 H>K>S 的既有语义计算, 不因 k_source 改变。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore import server  # noqa: E402
from memorycore.core import recall_probe  # noqa: E402


class _RecallClient:
    """固定顺序候选 mock; 不访问任何真实冷层。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]

    def recall_results(self, query, top_k=5, bump=True):
        return [dict(it) for it in self.items]


def _one_event(tmp_store, monkeypatch, tmp_path, items, query, top_k=10, handle=""):
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    server._store = tmp_store
    server._client = _RecallClient(items)
    response = json.loads(server.memorycore_recall(query, top_k=top_k, handle=handle))
    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1, lines
    return response, json.loads(lines[0])


def test_k_source_five_values_and_missing_field(tmp_store, tmp_path, monkeypatch):
    """A 核心: 五种 k_source + 旧 mock 缺字段, 一次事件内区分。"""
    items = [
        {"id": "kw", "content": "甲内容", "dense_score": 0.90,
         "importance": 0.5, "keyword_score": 0.3},
        {"id": "fts", "content": "乙内容", "dense_score": 0.90,
         "importance": 0.5, "fts_score": 0.4},
        {"id": "both", "content": "丙内容", "dense_score": 0.90,
         "importance": 0.5, "keyword_score": 0.2, "fts_score": 0.1},
        {"id": "local", "content": "阿尔法贝塔记录", "dense_score": 0.90,
         "importance": 0.5},
        {"id": "none", "content": "丁内容", "dense_score": 0.90,
         "importance": 0.5},
        # 旧 mock: 只有 dense_score, 缺 keyword/fts/content 也不得抛
        {"id": "missing", "dense_score": 0.90, "importance": 0.5},
    ]
    response, event = _one_event(
        tmp_store, monkeypatch, tmp_path, items, "阿尔法贝塔", top_k=10)
    got = dict(zip(event["returned_ids"], event["k_source"]))
    assert got["kw"] == "cold_kw"
    assert got["fts"] == "cold_fts"
    assert got["both"] == "cold_kw_fts"
    assert got["local"] == "local_lex"
    assert got["none"] == ""
    assert got["missing"] == ""
    # 事件数组与 returned_ids 严格等长
    assert len(event["k_source"]) == len(event["returned_ids"])
    # channel 语义不受 k_source 影响
    channels = dict(zip(event["returned_ids"], event["channel"]))
    assert channels["local"] == "K"
    assert channels["kw"] == "K"
    assert channels["fts"] == "K"
    assert channels["both"] == "K"
    assert channels["none"] == "S"
    assert channels["missing"] == "S"
    # k_source 不得进入 MCP 返回
    for row in response["results"]:
        assert "k_source" not in row


def test_k_source_type_anomaly_falls_back_to_empty(tmp_store, tmp_path, monkeypatch):
    """类型异常 (字符串/None/非有限) 一律视为无冷层 K 证据。"""
    items = [
        {"id": "bad_kw", "content": "戊内容", "dense_score": 0.8,
         "importance": 0.5, "keyword_score": "oops"},
        {"id": "bad_fts", "content": "己内容", "dense_score": 0.8,
         "importance": 0.5, "fts_score": None},
        # keyword 异常 + 本地 bigram 命中 → 只能归 local_lex
        {"id": "bad_kw_local", "content": "伽马德尔塔记录", "dense_score": 0.8,
         "importance": 0.5, "keyword_score": "oops"},
    ]
    _response, event = _one_event(
        tmp_store, monkeypatch, tmp_path, items, "伽马德尔塔", top_k=5)
    got = dict(zip(event["returned_ids"], event["k_source"]))
    assert got["bad_kw"] == ""
    assert got["bad_fts"] == ""
    assert got["bad_kw_local"] == "local_lex"
    assert len(event["k_source"]) == len(event["returned_ids"])


def test_k_source_handle_row_has_no_k_evidence(tmp_store, meta_for,
                                               tmp_path, monkeypatch):
    """H (handle 直查) 行无冷层 K 分数/本地 lex 命中 → k_source=\"\"。"""
    stub = "[规则指针]主题乙→recall:0123456789abcdef"
    tmp_store.add("memory", stub)
    meta_for("memory").stamp(stub, "stub", origin="stub_sink",
                             cold_id="cold-h2", handle="#h2")
    items = [
        {"id": "cold-h2", "content": "", "dense_score": 0.9,
         "importance": 0.8},
    ]
    _response, event = _one_event(
        tmp_store, monkeypatch, tmp_path, items, "主题乙", top_k=3,
        handle="#h2")
    assert event["channel"] == ["H"]
    assert event["k_source"] == [""]


def test_probe_whitelist_accepts_k_source_and_injected(tmp_path, monkeypatch):
    """P1: k_source/injected 已进白名单; query/content 明文仍不落盘。"""
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe({
        "source": "prefetch",
        "query_sha256": "q" * 64,
        "query_len": 4,
        "k_source": ["cold_kw", "local_lex", ""],
        "injected": [True, False, True],
        "selected": ["a", "c"],
        "returned_ids": ["a", "b", "c"],
        "query": "明文查询不得落盘",
        "content": "正文不得落盘",
    })
    assert recall_probe.get_probe_metrics() == {
        "attempts": 1, "written": 1, "errors": 0, "dropped": 0}
    event = json.loads(target.read_text(encoding="utf-8").splitlines()[0])
    assert event["k_source"] == ["cold_kw", "local_lex", ""]
    assert event["injected"] == [True, False, True]
    raw = target.read_text(encoding="utf-8")
    assert "明文查询不得落盘" not in raw
    assert "正文不得落盘" not in raw
