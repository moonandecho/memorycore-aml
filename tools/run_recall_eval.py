#!/usr/bin/env python3
"""tools/run_recall_eval.py — 召回评估 runner (P0: 只输出聚合指标)。

读取 sealed labels JSONL → 对每条 query 调用只读召回函数 → 只输出:
  * edition hash (labels 文件 sha256) + 样本数;
  * hit@1 / hit@3 / hit@5 / MRR / negative_control_FP_rate;
  * 按 bucket 分桶后的同一组聚合指标;
  * 每条 query 的召回异常计数 (只计数, 不输出 query/目标/明细)。

安全边界 (冻结口径):
  * 不打印逐题答案、不打印 targets / qid / query 文本 / 召回 id 列表;
  * 标签文件只读; runner 不生成标签、不写 sealed holdout;
  * 默认只读召回: bump=False;
  * 默认 source=cold 需要冷层可达; 离线协议自检可用
    ``--recall-source empty`` (仅证明输出格式, 不产生真实评估数字)。

标签行格式见 ``eval/README.md``。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BUCKETS = ("entity", "date", "paraphrase", "multihop", "negative_control")
_LABEL_KEYS = ("qid", "query", "targets", "supportable", "bucket",
               "anchor_sha256")

RecallFn = Callable[[str, int], Sequence[Any]]


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_labels(path: Path) -> List[Dict[str, Any]]:
    """读 labels JSONL; 空行/``#`` 行忽略 (holdout 空占位 = 0 样本合法)。"""
    rows: List[Dict[str, Any]] = []
    text = Path(path).read_text(encoding="utf-8")
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            item = json.loads(line)
        except Exception as exc:
            # 不把原始行拼进错误信息, 避免潜在标签泄漏到 stderr。
            raise ValueError(
                f"labels JSONL malformed at line {lineno}: {type(exc).__name__}")
        if not isinstance(item, dict):
            raise ValueError(f"labels JSONL line {lineno} is not an object")
        rows.append(item)
    return rows


def _validate_labels(rows: Sequence[Dict[str, Any]]) -> None:
    seen_qid = set()
    for i, row in enumerate(rows):
        for key in _LABEL_KEYS:
            if key not in row:
                raise ValueError(f"label {i} missing required key {key!r}")
        qid = row.get("qid")
        if not isinstance(qid, str) or not qid:
            raise ValueError(f"label {i} qid must be non-empty str")
        if qid in seen_qid:
            raise ValueError(f"label {i} duplicate qid")
        seen_qid.add(qid)
        if not isinstance(row.get("query"), str):
            raise ValueError(f"label {i} query must be str")
        targets = row.get("targets")
        if not isinstance(targets, list) or not all(
                isinstance(x, str) and x for x in targets):
            raise ValueError(f"label {i} targets must be list[str]")
        supportable = row.get("supportable")
        if not isinstance(supportable, bool):
            raise ValueError(f"label {i} supportable must be bool")
        bucket = row.get("bucket")
        if bucket not in BUCKETS:
            raise ValueError(f"label {i} bucket must be one of {BUCKETS}")
        # R6: supportable/bucket/targets 语义一致性; 防止正样本分母被空
        # targets 扩大, 或同一行同时进正样本与负控。
        if bucket == "negative_control":
            if supportable is not False or targets:
                raise ValueError(
                    f"label {i} negative_control requires supportable=false "
                    f"and targets=[]")
        elif supportable is not True or not targets:
            raise ValueError(
                f"label {i} bucket={bucket!r} requires supportable=true "
                f"and non-empty targets")
        if not isinstance(row.get("anchor_sha256"), str):
            raise ValueError(f"label {i} anchor_sha256 must be str")


def _result_ids(raw_results: Sequence[Any]) -> List[str]:
    ids: List[str] = []
    for item in raw_results or []:
        if isinstance(item, str):
            ids.append(item)
        elif isinstance(item, dict):
            rid = item.get("id")
            if rid is not None:
                ids.append(str(rid))
    return ids


def _rank_of_first_target(returned_ids: Sequence[str],
                          targets: Sequence[str]) -> int:
    target_set = set(targets or [])
    for rank, rid in enumerate(returned_ids, 1):
        if rid in target_set:
            return rank
    return 0


def _empty_recall(query: str, top_k: int) -> List[str]:
    """离线协议自检占位: 永不返回结果 (不产生逐题/真实数据)。"""
    return []


def _cold_store_recall_factory() -> RecallFn:
    """默认只读冷层召回 (bump=False); 惰性 import, 避免 runner 导入即联网。

    使用本仓库的 ``memorycore.cold_store_client.ColdStoreClient`` 多 backend
    客户端 (LocalBackend / RemoteBackend); 冷层不可达时由 main() 捕获并
    以 exit 2 明确失败。
    """
    from memorycore.cold_store_client import ColdStoreClient
    client = ColdStoreClient()

    def _recall(query: str, top_k: int) -> List[str]:
        results = client.recall_results(query, top_k=top_k, bump=False)
        return _result_ids(results)

    return _recall


def _recall_source(name: str) -> RecallFn:
    if name == "empty":
        return _empty_recall
    if name in ("cold", "mnemosyne"):
        return _cold_store_recall_factory()
    raise ValueError(f"unknown recall source: {name}")


def _pct(num: int, den: int) -> float:
    return round(num / den, 6) if den else 0.0


def _bucket_metrics(sample_ranks: Sequence[int], top_k: int,
                    all_metrics: Dict[str, Any]) -> Dict[str, Any]:
    n = len(sample_ranks)
    hits_at = 0
    mrr_sum = 0.0
    for rank in sample_ranks:
        if 0 < rank <= top_k:
            hits_at += 1
            mrr_sum += 1.0 / rank
    return {
        "n": n,
        "hit@k": _pct(hits_at, n),
        "mrr": round(mrr_sum / n, 6) if n else 0.0,
        # R9: 非 negative_control 桶没有自己的负控样本, 不得复制全局 FP。
        "negative_control_FP_rate": None,
    }


def evaluate_labels(rows: Sequence[Dict[str, Any]], recall_fn: RecallFn,
                    *, top_k: int = 5,
                    labels_sha256: str = "",
                    label_version: Optional[str] = None,
                    embed_model: Optional[str] = None,
                    manifest_buckets: Optional[Sequence[str]] = None,
                    frozen_at: Optional[str] = None) -> Dict[str, Any]:
    """执行评估并返回**只包含聚合指标**的 report dict。

    语义:
      * positive/supportable 样本: target 首次出现在第 k 名则 hit@k=1; MRR=1/rank;
      * negative_control (targets=[]): top_k 返回任一结果 = 一次 FP;
      * 召回调用异常按 miss 计, 只累计 ``errors`` 数量。
    """
    _validate_labels(rows)
    top_k = int(top_k)
    if top_k < 1:
        # R6: 不静默改成 1; 指标名与截断集合必须一致。
        raise ValueError("top_k must be >= 1")
    ranks: List[int] = []
    pos_ranks: List[int] = []
    bucket_ranks: Dict[str, List[int]] = {b: [] for b in BUCKETS}
    negative_n = 0
    negative_fp = 0
    errors = 0
    for row in rows:
        targets = list(row.get("targets") or [])
        supportable = bool(row.get("supportable"))
        try:
            raw = recall_fn(str(row.get("query") or ""), top_k)
            returned_ids = _result_ids(raw)[:top_k]
        except Exception:
            errors += 1
            returned_ids = []
        is_negative = (str(row.get("bucket")) == "negative_control")
        if is_negative:
            negative_n += 1
            if returned_ids:
                negative_fp += 1
        rank = _rank_of_first_target(returned_ids, targets) if supportable else 0
        ranks.append(rank)
        if supportable:
            pos_ranks.append(rank)
            bucket_ranks.setdefault(str(row.get("bucket")), []).append(rank)
    n = len(rows)
    supportable_n = len(pos_ranks)

    metric_notes: Dict[str, str] = {}

    def _hit_at(k: int):
        # R6: top_k < k 时该指标显式置 null, 不用截断后的短列表冒充 hit@k。
        if top_k < k:
            metric_notes[f"hit@{k}"] = (
                f"null: top_k={top_k} < {k}; 截断集合无法测该指标")
            return None
        return _pct(sum(1 for r in pos_ranks if 0 < r <= k), supportable_n)

    if top_k < 5:
        metric_notes["mrr"] = (
            f"MRR@{top_k} (top_k={top_k} < 5; mrr 名保持兼容, "
            f"不是无截断 MRR)")
    mrr = (round(sum(1.0 / r for r in pos_ranks if r > 0) / supportable_n, 6)
           if supportable_n else 0.0)
    metrics = {
        "hit@1": _hit_at(1),
        "hit@3": _hit_at(3),
        "hit@5": _hit_at(5),
        "hit@k": _hit_at(top_k),
        "mrr": mrr,
        "negative_control_FP_rate": _pct(negative_fp, negative_n),
        "negative_control_n": negative_n,
        "supportable_n": supportable_n,
        "sample_count": n,
        "errors": errors,
    }
    by_bucket: Dict[str, Any] = {}
    observed_buckets = [b for b in BUCKETS
                        if b in bucket_ranks or
                        any(str(r.get("bucket")) == b for r in rows)]
    for bucket in observed_buckets:
        br = bucket_ranks.get(bucket, [])
        if bucket == "negative_control":
            by_bucket[bucket] = {
                "n": negative_n,
                "hit@k": 0.0,
                "mrr": 0.0,
                "negative_control_FP_rate": metrics["negative_control_FP_rate"],
            }
        else:
            by_bucket[bucket] = _bucket_metrics(br, top_k, metrics)
    edition = {
        "labels_sha256": labels_sha256,
        "sample_count": n,
        "label_version": label_version,
        "embed_model": embed_model,
        "frozen_at": frozen_at,
        "buckets": list(manifest_buckets or observed_buckets),
    }
    return {
        "schema_version": "recall-eval-p0",
        "edition_hash": labels_sha256,
        "sample_count": n,
        "edition": edition,
        "top_k": top_k,
        "metrics": metrics,
        "metric_notes": metric_notes,
        "by_bucket": by_bucket,
    }


def _read_manifest(path: Optional[Path]) -> Dict[str, Any]:
    if not path or not Path(path).exists():
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _manifest_hash_for(manifest: Dict[str, Any],
                       labels_path: Path) -> Optional[str]:
    """返回 manifest 登记的 labels_sha256 (按实际 labels 文件选择)。

    manifest 可登记两份文件: ``labels_file``/``labels_sha256`` (正式评估集,
    默认 holdout) 与 ``dev_labels_file``/``dev_labels_sha256`` (合成调试样例)。
    只有传入文件命中登记路径时才使用对应 hash; 未命中时回退到
    ``labels_sha256``, 保持外部只写单份 hash 的简单用法。
    """
    try:
        actual = Path(labels_path).resolve()
    except Exception:
        return manifest.get("labels_sha256")
    for file_key, hash_key in (("labels_file", "labels_sha256"),
                               ("dev_labels_file", "dev_labels_sha256")):
        rel = manifest.get(file_key)
        digest = manifest.get(hash_key)
        if not isinstance(rel, str) or not rel:
            continue
        try:
            candidate = Path(rel)
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            if candidate.resolve() == actual:
                return digest if isinstance(digest, str) else None
        except Exception:
            continue
    return manifest.get("labels_sha256")


def main(argv: Optional[Sequence[str]] = None, *,
         recall_fn: Optional[RecallFn] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--labels", type=Path,
                    default=ROOT / "eval" / "eval_holdout.labels.jsonl",
                    help="labels JSONL (只读)")
    ap.add_argument("--manifest", type=Path,
                    default=ROOT / "eval" / "manifest.json",
                    help="可选 manifest (只读取 edition 元数据)")
    ap.add_argument("--top-k", type=int, default=5,
                    help="评估截断 (默认 5; 仅只读召回, 不改变 server 行为)")
    ap.add_argument("--recall-source",
                    choices=("cold", "mnemosyne", "empty"),
                    default="cold",
                    help="cold = 只读冷层评估 (mnemosyne 为兼容别名); "
                         "empty = 离线输出格式自检, 不产生真实数字")
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args(argv)

    if args.top_k < 1:
        # R6: CLI 明确失败, 不静默改成 1。
        print("error: --top-k must be >= 1", file=sys.stderr)
        return 2
    labels_path = Path(args.labels)
    raw_bytes = labels_path.read_bytes()
    labels_sha256 = _sha256_bytes(raw_bytes)
    manifest = _read_manifest(args.manifest)
    if args.manifest is not None and Path(args.manifest).exists():
        expected = _manifest_hash_for(manifest, labels_path)
        if not isinstance(expected, str) or expected != labels_sha256:
            # R6: manifest 登记 hash 与实测不一致 -> fail, 不静默覆盖。
            print("error: manifest labels_sha256 mismatch", file=sys.stderr)
            return 2
    try:
        rows = load_labels(labels_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        fn = recall_fn or _recall_source(args.recall_source)
    except Exception as exc:  # noqa: BLE001
        print(f"error: recall source unavailable: {type(exc).__name__}",
              file=sys.stderr)
        return 2
    try:
        report = evaluate_labels(
            rows, fn, top_k=args.top_k,
            labels_sha256=labels_sha256,
            label_version=manifest.get("label_version"),
            embed_model=manifest.get("embed_model"),
            manifest_buckets=manifest.get("buckets"),
            frozen_at=manifest.get("frozen_at"),
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    report["recall_source"] = ("injected" if recall_fn is not None
                               else args.recall_source)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        Path(args.json_out).write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
