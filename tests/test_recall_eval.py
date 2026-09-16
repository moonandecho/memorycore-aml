#!/usr/bin/env python3
"""tests/test_recall_eval.py — P0 召回评估 runner 格式/安全边界/畸形输入。

覆盖:
  * 只输出聚合指标; 绝不出现 qid/query/targets/逐题召回 id;
  * negative_control FP rate 可复算, 异常按 miss 计;
  * 四处畸形输入必须明确失败: targets=[]+supportable=true、
    negative_control 语义错配、--top-k 0、manifest hash 不符;
  * 合法正样本 + 合法负控必须放行 (双向)。
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TOOLS = REPO_ROOT / "tools"
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(TOOLS))

import run_recall_eval  # noqa: E402


def _label(qid, query, targets, bucket, supportable=True, anchor="a" * 64):
    return {"qid": qid, "query": query, "targets": targets,
            "supportable": supportable, "bucket": bucket,
            "anchor_sha256": anchor}


def _write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
                    + "\n", encoding="utf-8")


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, str(TOOLS / "run_recall_eval.py"), *args],
        cwd=str(REPO_ROOT), capture_output=True, text=True, check=False)


def test_evaluate_labels_aggregates_hit_mrr_and_negative_fp():
    rows = [
        _label("q1", "q-entity", ["T-ENTITY"], "entity"),
        _label("q2", "q-date", ["T-DATE"], "date"),
        _label("q3", "q-neg-1", [], "negative_control", supportable=False),
        _label("q4", "q-neg-2", [], "negative_control", supportable=False),
    ]
    recall_map = {
        "q-entity": ["X-1", "T-ENTITY"],  # rank 2
        "q-date": ["T-DATE"],             # rank 1
        "q-neg-1": [],
        "q-neg-2": ["X-2"],               # FP
    }
    report = run_recall_eval.evaluate_labels(
        rows, lambda q, k: recall_map[q], top_k=5, labels_sha256="f" * 64,
        label_version="test-v1", embed_model="test-model",
        manifest_buckets=list(run_recall_eval.BUCKETS))
    metrics = report["metrics"]
    assert metrics["sample_count"] == 4
    assert metrics["supportable_n"] == 2
    assert metrics["negative_control_n"] == 2
    assert metrics["hit@1"] == 0.5
    assert metrics["hit@3"] == 1.0
    assert metrics["hit@5"] == 1.0
    assert metrics["mrr"] == 0.75
    assert metrics["negative_control_FP_rate"] == 0.5
    assert report["edition"]["labels_sha256"] == "f" * 64
    assert report["by_bucket"]["entity"]["n"] == 1
    assert report["by_bucket"]["negative_control"]["n"] == 2
    blob = json.dumps(report, ensure_ascii=False)
    for leaked in ("T-ENTITY", "T-DATE", "q-entity", "q-date", "targets",
                   '"qid"', "q1"):
        assert leaked not in blob, leaked


def test_evaluate_labels_handles_recall_errors_as_miss():
    rows = [_label("q1", "q-err", ["T-1"], "entity")]

    def broken_recall(query, top_k):
        raise RuntimeError("cold unavailable")

    report = run_recall_eval.evaluate_labels(rows, broken_recall, top_k=3)
    assert report["metrics"]["errors"] == 1
    assert report["metrics"]["hit@3"] == 0.0
    assert report["metrics"]["mrr"] == 0.0


def test_legal_positive_and_negative_control_pass():
    """双向放行: 合法正样本与合法 negative_control 都不得被校验误杀。"""
    rows = [
        _label("q-pos", "q-pos", ["T-1"], "entity"),
        _label("q-neg", "q-neg", [], "negative_control", supportable=False),
    ]
    report = run_recall_eval.evaluate_labels(
        rows, lambda q, k: ["T-1"] if q == "q-pos" else [], top_k=5)
    assert report["metrics"]["sample_count"] == 2
    assert report["metrics"]["supportable_n"] == 1
    assert report["metrics"]["negative_control_n"] == 1
    assert report["metrics"]["negative_control_FP_rate"] == 0.0
    assert report["metrics"]["hit@1"] == 1.0


def test_malformed_targets_supportable_combo_rejected():
    row = _label("q1", "q", [], "entity", supportable=True)
    try:
        run_recall_eval.evaluate_labels([row], lambda q, k: [], top_k=5)
    except ValueError as exc:
        assert "non-empty targets" in str(exc)
    else:
        raise AssertionError("targets=[] + supportable=true 必须失败")


def test_malformed_negative_control_semantics_rejected():
    bad_support = _label("q1", "q", [], "negative_control", supportable=True)
    bad_targets = _label("q2", "q", ["T"], "negative_control",
                         supportable=False)
    for row in (bad_support, bad_targets):
        try:
            run_recall_eval.evaluate_labels([row], lambda q, k: [], top_k=5)
        except ValueError as exc:
            assert "negative_control" in str(exc)
        else:
            raise AssertionError("negative_control 语义错配必须失败")


def test_top_k_zero_rejected():
    row = _label("q1", "q", ["T"], "entity")
    try:
        run_recall_eval.evaluate_labels([row], lambda q, k: ["T"], top_k=0)
    except ValueError as exc:
        assert "top_k" in str(exc)
    else:
        raise AssertionError("top_k=0 必须失败")


def test_cli_output_has_no_per_question_target_leak(tmp_path):
    labels = tmp_path / "labels.jsonl"
    target_id = "cold_SYNTH_TARGET_XYZ"
    secret_query = "SECRET_SYNTH_QUERY_XYZ"
    labels.write_text(json.dumps({
        "qid": "secret-qid-xyz", "query": secret_query,
        "targets": [target_id], "supportable": True, "bucket": "entity",
        "anchor_sha256": "b" * 64}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    proc = _run_cli("--labels", str(labels), "--manifest",
                    str(tmp_path / "missing-manifest.json"),
                    "--recall-source", "empty")
    assert proc.returncode == 0, proc.stderr
    for leaked in (target_id, secret_query, "secret-qid-xyz", "targets"):
        assert leaked not in proc.stdout, f"runner leaked {leaked!r}"
    report = json.loads(proc.stdout)
    assert report["metrics"]["sample_count"] == 1
    assert report["edition"]["labels_sha256"]
    assert report["recall_source"] == "empty"


def test_cli_malformed_semantics_fails_without_label_leak(tmp_path):
    labels = tmp_path / "labels.jsonl"
    secret_query = "SECRET_MALFORMED_SYNTH_QUERY"
    labels.write_text(json.dumps({
        "qid": "secret-malformed-qid", "query": secret_query,
        "targets": [], "supportable": True, "bucket": "entity",
        "anchor_sha256": "a" * 64}, ensure_ascii=False) + "\n",
        encoding="utf-8")
    proc = _run_cli("--labels", str(labels),
                    "--manifest", str(tmp_path / "missing.json"),
                    "--recall-source", "empty")
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "non-empty targets" in proc.stderr
    for leaked in (secret_query, "secret-malformed-qid"):
        assert leaked not in proc.stderr, f"CLI failure leaked {leaked!r}"


def test_cli_top_k_zero_fails(tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text("{}\n", encoding="utf-8")
    proc = _run_cli("--labels", str(labels),
                    "--manifest", str(tmp_path / "missing.json"),
                    "--top-k", "0", "--recall-source", "empty")
    assert proc.returncode == 2
    assert proc.stdout.strip() == ""
    assert "--top-k" in proc.stderr


def test_cli_manifest_hash_mismatch_fails(tmp_path):
    labels = tmp_path / "labels.jsonl"
    labels.write_text(json.dumps(_label("q1", "q", ["T"], "entity")) + "\n",
                      encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"labels_sha256": "0" * 64}),
                        encoding="utf-8")
    proc = _run_cli("--labels", str(labels), "--manifest", str(manifest),
                    "--recall-source", "empty")
    assert proc.returncode == 2
    assert "labels_sha256" in proc.stderr
    assert proc.stdout.strip() == ""


def test_cli_repo_synthetic_dev_and_manifest_hashes_pass():
    """仓库自带纯合成 dev + manifest 双 hash 登记必须放行。"""
    proc = _run_cli("--labels", str(REPO_ROOT / "eval" / "eval_dev.labels.jsonl"),
                    "--manifest", str(REPO_ROOT / "eval" / "manifest.json"),
                    "--recall-source", "empty")
    assert proc.returncode == 0, proc.stderr
    report = json.loads(proc.stdout)
    assert report["metrics"]["sample_count"] == 6
    assert report["metrics"]["negative_control_n"] == 2
    assert hashlib.sha256(
        (REPO_ROOT / "eval" / "eval_dev.labels.jsonl").read_bytes()
    ).hexdigest() == report["edition_hash"]
