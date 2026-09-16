#!/usr/bin/env python3
"""tests/test_recall_probe.py — P0 只读观测探针 (默认关/fail-silent/路径守卫)。

覆盖:
  * 开关关 -> 不创建文件/目录; 开关开 -> 白名单字段齐全且 query 明文不落盘;
  * 写入失败 fail-silent, metrics 可观察;
  * 路径守卫: 符号链接、env 直指 activity、硬链接、lock 软链、lock 硬链;
  * 非普通文件 (FIFO/目录) 快速拒绝且不留 .lock 残留;
  * metrics 真实性: written=真正 append 数, 滚动删行计入 dropped,
    自身超预算只丢该行并记 errors。
"""
import hashlib
import json
import os
import stat
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memorycore.core import recall_probe

def _event():
    return {
        "source": "server_recall",
        "query_sha256": "q" * 64,
        "query_len": 7,
        "top_k": 3,
        "candidate_count": 2,
        "returned_ids": ["a", "b"],
        "dense_scores": [0.9, 0.8],
        "keyword_scores": [0.2, 0.0],
        "fts_scores": [0.0, 0.1],
        "channel": ["K", "K"],
        "selected": [True, True],
        "page_fault": False,
        "restore": 0,
        "latency_ms": 1.25,
        "error": None,
        # 下列内容即使被误传也不得落盘
        "query": "秘密明文窗口",
        "content": "不得落盘的内容",
    }


def _probe_env(monkeypatch, tmp_path, name="recall_probe.jsonl"):
    target = tmp_path / "probe" / name
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    return target


def _activity_env(monkeypatch, tmp_path):
    activity = tmp_path / "activity.jsonl"
    activity.write_text('{"seed":"real"}\n', encoding="utf-8")
    monkeypatch.setattr(recall_probe._config, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(recall_probe._config, "ACTIVITY_LOG_FILE", activity)
    return activity


def test_probe_disabled_creates_no_file_or_dir(tmp_path, monkeypatch):
    """默认关 (env unset/0) -> 零文件/零目录创建, attempts=0。"""
    target = tmp_path / "probe-nested" / "recall_probe.jsonl"
    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE", raising=False)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe(_event())
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "0")
    recall_probe.record_recall_probe(_event())
    with pytest.raises(FileNotFoundError):
        os.stat(target)
    with pytest.raises(FileNotFoundError):
        os.stat(target.parent)
    assert recall_probe.get_probe_metrics() == {
        "attempts": 0, "written": 0, "errors": 0, "dropped": 0}


def test_probe_enabled_writes_full_event_and_no_plaintext(tmp_path, monkeypatch):
    target = _probe_env(monkeypatch, tmp_path)
    recall_probe.record_recall_probe(_event())
    metrics = recall_probe.get_probe_metrics()
    assert metrics == {"attempts": 1, "written": 1, "errors": 0, "dropped": 0}
    raw = target.read_text(encoding="utf-8")
    lines = raw.splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    expected = {"ts", "source", "query_sha256", "query_len", "top_k",
                "candidate_count", "returned_ids", "dense_scores",
                "keyword_scores", "fts_scores", "channel", "selected",
                "page_fault", "restore", "latency_ms", "error"}
    assert expected <= set(event)
    assert event["returned_ids"] == ["a", "b"]
    assert event["keyword_scores"] == [0.2, 0.0]
    assert event["fts_scores"] == [0.0, 0.1]
    raw_bytes = target.read_bytes()
    assert "秘密明文窗口".encode() not in raw_bytes
    assert "不得落盘的内容".encode() not in raw_bytes


def test_probe_write_failure_is_silent_but_observable(tmp_path, monkeypatch):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    target = blocker / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe(_event())  # 不得抛
    metrics = recall_probe.get_probe_metrics()
    assert metrics["attempts"] == 1
    assert metrics["errors"] == 1
    assert metrics["written"] == 0


def test_probe_query_helper_is_stable_sha256():
    payload = "合成探针 query"
    assert recall_probe.query_sha256(payload) == hashlib.sha256(
        payload.encode("utf-8")).hexdigest()


# ---- 路径守卫: 五形态 + FIFO/目录 -----------------------------------------

def test_guard_symlink_probe_to_activity_rejected(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    probe = tmp_path / "recall_probe.jsonl"
    probe.symlink_to(activity)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE_FILE", raising=False)
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe(_event())
    assert len(activity.read_text(encoding="utf-8").splitlines()) == 1
    assert probe.is_symlink(), "守卫只拒绝写入, 不吞/删用户 symlink"
    assert recall_probe.get_probe_metrics() == {
        "attempts": 1, "written": 0, "errors": 1, "dropped": 0}


def test_guard_env_override_direct_to_activity_rejected(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(activity))
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe(_event())
    assert len(activity.read_text(encoding="utf-8").splitlines()) == 1
    assert recall_probe.get_probe_metrics()["errors"] == 1


def test_guard_hardlink_probe_to_activity_rejected_unchanged(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    target = _probe_env(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    os.link(activity, target)
    assert os.path.samefile(target, activity)
    before = activity.read_bytes()
    recall_probe.record_recall_probe(_event())
    assert activity.read_bytes() == before, "硬链接绕过: activity 被探针追加"
    assert recall_probe.get_probe_metrics()["written"] == 0
    assert recall_probe.get_probe_metrics()["errors"] == 1


def test_guard_lock_symlink_to_activity_rejected(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    target = _probe_env(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(target) + ".lock")
    lock_path.symlink_to(activity)
    before = activity.read_bytes()
    recall_probe.record_recall_probe(_event())
    assert activity.read_bytes() == before
    assert not target.exists()
    assert lock_path.is_symlink(), "守卫只拒绝写入, 不吞/删用户 symlink"


def test_guard_lock_hardlink_to_activity_rejected(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    target = _probe_env(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(target) + ".lock")
    os.link(activity, lock_path)
    assert os.path.samefile(lock_path, activity)
    before = activity.read_bytes()
    recall_probe.record_recall_probe(_event())
    assert activity.read_bytes() == before
    assert not target.exists()


def test_guard_fifo_rejected_fast_without_lock_residue(tmp_path, monkeypatch):
    target = tmp_path / "probe.fifo"
    os.mkfifo(target)
    activity = _activity_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    before = activity.read_bytes()
    start = time.monotonic()
    recall_probe.record_recall_probe(_event())
    elapsed = time.monotonic() - start
    assert elapsed < 1.0, f"FIFO 探针路径阻塞了 {elapsed:.3f}s"
    metrics = recall_probe.get_probe_metrics()
    assert metrics["written"] == 0 and metrics["errors"] >= 1
    assert stat.S_ISFIFO(os.lstat(target).st_mode)
    assert not Path(str(target) + ".lock").exists(), "拒绝后不得留下 lock 残留"
    assert activity.read_bytes() == before


def test_guard_directory_rejected_without_lock_residue(tmp_path, monkeypatch):
    target = tmp_path / "probe-dir"
    target.mkdir()
    _activity_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe(_event())
    assert target.is_dir()
    assert not Path(str(target) + ".lock").exists()
    assert recall_probe.get_probe_metrics()["errors"] >= 1


# ---- metrics 真实性 --------------------------------------------------------

def test_metrics_rolling_dropped_and_written_truthful(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    target = _probe_env(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat()
    pad = "o" * 64600
    rows = [json.dumps({"ts": now, "source": f"historic-{i}",
                        "returned_ids": [f"old-{i}-" + pad]},
                       ensure_ascii=False, separators=(",", ":"))
            for i in range(4)]
    target.write_text("\n".join(rows) + "\n", encoding="utf-8")
    before_lines = len(rows)
    recall_probe.reset_probe_metrics()
    recall_probe.record_recall_probe({
        "source": "huge_rolling", "returned_ids": ["H" * 300000],
        "dense_scores": [0.2], "channel": ["S"], "selected": [True]})
    text = target.read_text(encoding="utf-8")
    lines = text.splitlines()
    metrics = recall_probe.get_probe_metrics()
    assert metrics["attempts"] == 1
    assert metrics["written"] == 1, "真正 append 成功必须计 written"
    assert metrics["errors"] == 0, "滚动删旧行是设计内, 不计 errors"
    assert metrics["dropped"] > 0, "滚动删行必须计入 dropped"
    assert metrics["written"] - metrics["dropped"] == len(lines) - before_lines
    assert "huge_rolling" in text
    assert activity.exists() and not target.samefile(activity)
    for line in lines:
        json.loads(line)


def test_metrics_latest_event_over_budget_drops_only_line(tmp_path, monkeypatch):
    activity = _activity_env(monkeypatch, tmp_path)
    target = _probe_env(monkeypatch, tmp_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(recall_probe, "PROBE_MAX_BYTES", 512)
    old_line = json.dumps({
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "old-oversized", "returned_ids": ["O" * 1000]},
        ensure_ascii=False, separators=(",", ":"))
    target.write_text(old_line + "\n", encoding="utf-8")
    before = target.read_bytes()
    recall_probe.record_recall_probe({
        "source": "new-over-budget", "returned_ids": ["N" * 1000]})
    assert target.read_bytes() == before, "超预算最新行必须整行丢弃, 历史不得被清"
    assert "new-over-budget" not in target.read_text(encoding="utf-8")
    assert activity.exists() and not target.samefile(activity)
    assert recall_probe.get_probe_metrics() == {
        "attempts": 1, "written": 0, "errors": 1, "dropped": 0}
