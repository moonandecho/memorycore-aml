#!/usr/bin/env python3
"""tests/test_recall_eval_server_path.py — P2-step0 治理层只读评估路径。

验收:
  * ``--recall-source server`` 走 server.recall_readonly（同 recall_results
    bump=False + 同一份 _apply_decay），不走冷层直连的无 decay 旧路径；
  * 只读入口不调用任何热层写方法 / restore_stubs_from_results / 活动日志 /
    探针落盘；
  * top_k 与生产 recall 一致夹取，候选顺序由 _apply_decay 决定。
"""
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOLS))

import run_recall_eval  # noqa: E402
from memorycore import server
from memorycore.core.decay import _apply_decay


class _RecallClient:
    """固定结果 mock；记录 bump 与 top_k，确保只读契约。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]
        self.calls = []
        self.bumps = []

    def recall_results(self, query, top_k=5, bump=True):
        self.calls.append((query, top_k))
        self.bumps.append(bump)
        return [dict(it) for it in self.items]


def _boom(name):
    def _raiser(*args, **kwargs):
        raise AssertionError(f"readonly path must not call {name}")
    return _raiser


def _patch_server(monkeypatch, tmp_store, client):
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", client)
    for meth in ("add", "replace", "remove", "remove_by_exact"):
        monkeypatch.setattr(tmp_store, meth, _boom(f"LocalStore.{meth}"))
    monkeypatch.setattr(server, "restore_stubs_from_results",
                        _boom("restore_stubs_from_results"))
    monkeypatch.setattr(server, "log_activity_query",
                        _boom("log_activity_query"))
    monkeypatch.setattr(server, "record_recall_probe",
                        _boom("record_recall_probe"))


def _label(qid, query, targets, supportable=True, bucket="entity"):
    return {
        "qid": qid,
        "query": query,
        "targets": targets,
        "supportable": supportable,
        "bucket": bucket,
        "anchor_sha256": "a" * 64,
    }


def test_recall_readonly_bump_false_and_no_hot_writes(
        tmp_store, monkeypatch):
    items = [
        {"id": "A", "content": "甲", "dense_score": 0.90,
         "importance": 0.9, "keyword_score": "oops", "fts_score": None},
        {"id": "B", "content": "乙", "dense_score": 0.30, "importance": 0.9},
        {"id": "C", "content": "丙", "dense_score": 0.50, "importance": 0.9,
         "keyword_score": 0.42},
    ]
    expected = [r["id"] for r in _apply_decay([dict(it) for it in items])]
    client = _RecallClient(items)
    _patch_server(monkeypatch, tmp_store, client)

    got = server.recall_readonly("合成查询", top_k=3)

    assert [r["id"] for r in got] == expected == ["A", "C", "B"]
    assert client.calls == [("合成查询", 3)]
    assert client.bumps == [False]
    assert got[0]["keyword_score"] == 0.0  # 非有限字段收口为 0
    assert got[0]["fts_score"] == 0.0
    assert got[0]["channel"] == "S"
    assert got[1]["channel"] == "K"
    assert not (tmp_store.memory_path).exists(), "只读入口不得创建热层文件"
    assert not (tmp_store.user_path).exists(), "只读入口不得创建热层文件"


def test_server_recall_factory_uses_readonly_entrypoint(tmp_store, monkeypatch):
    client = _RecallClient([{"id": "T-1", "content": "目标",
                             "dense_score": 0.9, "importance": 0.9}])
    _patch_server(monkeypatch, tmp_store, client)

    recall_fn = run_recall_eval._recall_source("server")
    assert recall_fn is not None
    assert recall_fn("查询", 5) == ["T-1"]
    assert client.bumps == [False]


def test_runner_cli_server_source_end_to_end(tmp_store, monkeypatch,
                                             tmp_path, capsys):
    client = _RecallClient([{"id": "T-1", "content": "目标",
                             "dense_score": 0.9, "importance": 0.9}])
    _patch_server(monkeypatch, tmp_store, client)
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(
        _label("q1", "查询文本", ["T-1"]), ensure_ascii=False) + "\n",
        encoding="utf-8")

    rc = run_recall_eval.main([
        "--labels", str(labels),
        "--manifest", str(tmp_path / "missing-manifest.json"),
        "--recall-source", "server",
        "--top-k", "5",
    ])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert out["recall_source"] == "server"
    assert out["metrics"]["hit@1"] == 1.0
    assert client.bumps == [False]
    for leaked in ("T-1", "查询文本", "q1", '"targets"'):
        assert leaked not in json.dumps(out, ensure_ascii=False)


# ---------------------------------------------------------------------------
# P2-FIX-D: 评估侧冻结 decay 时间 (不修改 core/decay.py)
# ---------------------------------------------------------------------------

def test_freeze_decay_time_makes_apply_decay_stable():
    """同一冻结时间下, 同一输入两次 _apply_decay 结果完全相同。"""
    from memorycore.core import decay as decay_mod

    reference = run_recall_eval._parse_freeze_decay_time(
        "2026-09-16T19:01:22Z")
    original = run_recall_eval._install_frozen_decay_clock(reference)
    rows = [
        {"id": "old", "dense_score": 1.0, "importance": 0.5,
         "last_recalled": "2026-09-13T19:29:36"},
        {"id": "new", "dense_score": 0.9, "importance": 0.5,
         "last_recalled": "2026-09-16T19:00:00"},
    ]
    try:
        first = [(r["id"], r["final_score"])
                 for r in _apply_decay([dict(r) for r in rows])]
        second = [(r["id"], r["final_score"])
                  for r in _apply_decay([dict(r) for r in rows])]
    finally:
        run_recall_eval._restore_decay_clock(original)

    assert first == second
    assert first == [
        ("old", 0.9847147529344312), ("new", 0.9)]


def test_runner_freeze_time_records_reference_and_restores_clock(
        tmp_path, capsys):
    from memorycore.core import decay as decay_mod

    before = decay_mod.datetime
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(
        _label("q1", "查询", ["T-1"]), ensure_ascii=False) + "\n",
        encoding="utf-8")

    rc = run_recall_eval.main([
        "--labels", str(labels),
        "--manifest", str(tmp_path / "missing.json"),
        "--recall-source", "empty",
        "--freeze-decay-time", "2026-09-16T19:01:22Z",
    ])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["decay_reference_time"] == "2026-09-16T19:01:22Z"
    assert decay_mod.datetime is before, "冻结仅评估进程内生效并须恢复"


def test_runner_records_wall_clock_reference_without_freeze(tmp_path, capsys):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(
        _label("q1", "查询", ["T-1"]), ensure_ascii=False) + "\n",
        encoding="utf-8")

    rc = run_recall_eval.main([
        "--labels", str(labels),
        "--manifest", str(tmp_path / "missing.json"),
        "--recall-source", "empty",
    ])
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["decay_reference_time"].endswith("Z")
    assert "T" in out["decay_reference_time"]


def test_runner_invalid_freeze_time_fails_without_stdout(tmp_path, capsys):
    labels = tmp_path / "labels.jsonl"
    labels.write_text("", encoding="utf-8")

    rc = run_recall_eval.main([
        "--labels", str(labels),
        "--manifest", str(tmp_path / "missing.json"),
        "--recall-source", "empty",
        "--freeze-decay-time", "not-a-date",
    ])
    captured = capsys.readouterr()

    assert rc == 2
    assert captured.out == ""
    assert "ISO8601" in captured.err


def test_runner_freeze_time_affects_injected_recall_ranking(
        tmp_path, capsys):
    """冻结点跨过 delta.days 边界时, 结果与产物采样口径一致。"""
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(
        _label("q1", "查询", ["A"]), ensure_ascii=False) + "\n",
        encoding="utf-8")

    def recall_fn(query, top_k):
        candidates = [
            {"id": "A", "content": "x", "dense_score": 0.99,
             "importance": 0.5,
             "last_recalled": "2026-09-13T19:30:00"},
            {"id": "B", "content": "y", "dense_score": 0.971,
             "importance": 0.5,
             "last_recalled": "2026-09-16T19:00:00"},
        ]
        return [r["id"] for r in _apply_decay(candidates)][:top_k]

    rc = run_recall_eval.main([
        "--labels", str(labels),
        "--manifest", str(tmp_path / "missing.json"),
        "--top-k", "5",
        "--freeze-decay-time", "2026-09-16T19:01:22Z",
    ], recall_fn=recall_fn)
    out = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert out["metrics"]["hit@1"] == 1.0
    assert out["metrics"]["mrr"] == 1.0
    assert out["decay_reference_time"] == "2026-09-16T19:01:22Z"
