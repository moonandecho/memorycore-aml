#!/usr/bin/env python3
"""replay_fault_rate.py — 热层缓存化缺页率回放 (F5: 只读固化 fixture)。

数据来源: `tests/fixtures/fault_replay_silver.json` (一次性归档生成器见
`tools/build_fault_replay_fixture.py`, 本脚本不重建 silver, 不读 activity,
不自算标签, 不依赖 Ollama/模型状态)。

口径:
  * fixture 固化 200 条查询 / 226 个查询-规则对 / 规则全文 / dense 向量;
  * baseline: K=3 + dense≥HIT_STRONG_COS=0.48 (fixture 内直接复算, 参考
    EVIDENCE 41.5%);
  * 本实现: K=20 候选 + S(≥0.42)/K(lex bigram≥2)/H(主题句柄匹配) 三通道,
    H>K>S 排序, 注入 top5; 低信息/噪声 query 已由 F1 闸门排除在 silver
    fixture 选取之外 (生成器口径), 本离线回放**不再重复执行 gate** —
    这是纯函数复刻 d 与 plugin 的同口径隔离设计; 变更见 FIX4 P3。
  * 写回共识阈值 F4 与本脚本无关: 缺页判定按设计 = 目标规则出现在注入集
    (S-only 也允许注入; 写回另有 K/H 门槛, 由 plugin 单测覆盖);
  * 输出每条 fault 的 query/hash/目标/注入集, 支持独立逐条复核。

用法:
  .venv/bin/python tools/replay_fault_rate.py \
      --fixture tests/fixtures/fault_replay_silver.json \
      --json-out /tmp/replay_fault_rate.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURE = ROOT / "tests/fixtures/fault_replay_silver.json"

HIT_STRONG_COS = 0.48
HIT_WEAK_COS = 0.42
INJECT_TOP_N = 5
RECALL_CANDIDATES = 20
LEX_EVIDENCE_BIGRAMS = 2
BASELINE_REFERENCE = 0.415


# --- 纯函数本地复刻 (与 plugin/core 实现同口径, 不依赖被测代码 import) ----
def _norm(s: str) -> str:
    s = re.sub(r"[\s，。！？；;、,：:·\-—/\\=_]+", "", s or "")
    return s.lower()


def _lex_evidence(a: str, b: str) -> bool:
    na, nb = _norm(a), _norm(b)
    if len(na) < 2 or len(nb) < 2:
        return False
    sa = {na[i:i + 2] for i in range(len(na) - 1)}
    sb = {nb[i:i + 2] for i in range(len(nb) - 1)}
    return len(sa & sb) >= LEX_EVIDENCE_BIGRAMS


def _rule_topic(rule: str) -> str:
    """H 通道代理: 与 _make_stub/_stub_topic 一致 — 首句前 10 字。"""
    raw = (rule or "").strip()
    kw = re.sub(r"\s+", "", raw.split("。")[0][:10])
    return kw or re.sub(r"\s+", "", raw[:10])


def _bigrams(s: str) -> set:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _handle_match(q: str, rule: str) -> bool:
    topic = _rule_topic(rule)
    if not topic:
        return False
    qq = re.sub(r"\s+", "", q or "")
    if topic in qq or qq in topic:
        return True
    return len(_bigrams(qq) & _bigrams(topic)) >= 2


def _eval_query(query: str, dense_row: List[float], rules: List[str],
                target_indices: set) -> Dict[str, Any]:
    """回放单条 query 的注入集合; 返回 hit/channels/fault_detail。"""
    order = sorted(range(len(rules)),
                   key=lambda j: float(dense_row[j]), reverse=True)
    handles = {j for j in range(len(rules)) if _handle_match(query, rules[j])}
    picked: List[Dict[str, Any]] = []
    for j in order[:RECALL_CANDIDATES]:
        sc = float(dense_row[j])
        h = j in handles
        k = _lex_evidence(query, rules[j])
        s = sc >= HIT_WEAK_COS
        if not (h or k or s):
            continue
        ch = "H" if h else ("K" if k else "S")
        picked.append({"rule_index": j, "channel": ch,
                       "dense": round(sc, 6), "score": sc})
    channel_order = {"H": 0, "K": 1, "S": 2}
    picked.sort(key=lambda r: (channel_order.get(r["channel"], 3),
                               -r["dense"]))
    picked = picked[:INJECT_TOP_N]
    hit = any(r["rule_index"] in target_indices for r in picked)
    return {
        "hit": hit,
        "picked": picked,
        "top_dense": round(float(order[0] and dense_row[order[0]] or 0.0), 6)
        if order else 0.0,
        "rule_dense_order": order,
    }


def evaluate_fixture(fixture: Dict[str, Any]) -> Dict[str, Any]:
    rules = [str(r["text"]) for r in fixture["rules"]]
    rule_sha = [str(r["rule_sha256"]) for r in fixture["rules"]]
    queries_raw = fixture["queries"]
    hits = 0
    faults: List[Dict[str, Any]] = []
    channel_hits = {"H": 0, "K": 0, "S": 0}
    injected_labeled = 0
    for qi, qitem in enumerate(queries_raw):
        q = str(qitem["query"])
        dense_row = [float(x) for x in qitem["dense"]]
        targets = set(int(x) for x in qitem["target_rule_indices"])
        ev = _eval_query(q, dense_row, rules, targets)
        picked = ev["picked"]
        injected_labeled += len(picked)
        if ev["hit"]:
            hits += 1
            for ch in {r["channel"] for r in picked
                       if r["rule_index"] in targets}:
                channel_hits[ch] = channel_hits.get(ch, 0) + 1
        else:
            faults.append({
                "fixture_index": qi,
                "activity_index": qitem.get("activity_index"),
                "query": q,
                "query_sha256": qitem.get("query_sha256"),
                "target_rule_indices": sorted(targets),
                "target_rule_sha256": sorted({
                    rule_sha[j] for j in targets if 0 <= j < len(rule_sha)}),
                "injected": [
                    {"rule_index": r["rule_index"],
                     "rule_sha256": rule_sha[r["rule_index"]],
                     "channel": r["channel"], "dense": r["dense"]}
                    for r in picked],
            })

    # baseline 用固化 pairs + dense 直接复算 (K=3 且 dense>=0.48)
    baseline_hits = 0
    for qitem in queries_raw:
        dense_row = [float(x) for x in qitem["dense"]]
        targets = set(int(x) for x in qitem["target_rule_indices"])
        order = sorted(range(len(rules)),
                       key=lambda j: float(dense_row[j]), reverse=True)
        cand = [j for j in order[:3] if float(dense_row[j]) >= HIT_STRONG_COS]
        if any(j in targets for j in cand):
            baseline_hits += 1
    n = len(queries_raw)
    baseline_fault = (n - baseline_hits) / n if n else 0.0
    fault_rate = (n - hits) / n if n else 0.0
    # F5/FIX4 P3: 相对下降必须由 fixture 内复算的 baseline 驱动, 不硬编码。
    # `baseline_k3_048_reference` 仅作证据数值展示。
    rel_drop = ((baseline_fault - fault_rate) / baseline_fault
                if baseline_fault else 0.0)
    return {
        "silver": {
            "queries": n,
            "pairs": len(fixture.get("pairs", [])),
            "baseline_k3_048_fault_rate": round(baseline_fault, 4),
            "baseline_k3_048_reference": BASELINE_REFERENCE,
            "baseline_hits_replayed": baseline_hits,
        },
        "implementation": {
            "hits": hits,
            "faults": n - hits,
            "fault_rate": round(fault_rate, 4),
            "relative_drop_vs_baseline": round(rel_drop, 4),
            "relative_drop_formula": "replayed_baseline_k3_048",
            "pass_fault_le_10pct": fault_rate <= 0.10,
            "pass_relative_drop_ge_70pct": rel_drop >= 0.70,
            "channel_covered_hits": channel_hits,
            "avg_injected_per_labeled": round(injected_labeled / n, 3)
            if n else 0.0,
        },
        "faults_detail": faults,
    }


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE,
                    help="固化 silver fixture (唯一数据来源)")
    ap.add_argument("--json-out", type=Path, default=None)
    # 兼容旧命令: activity/rules 仅提示已废弃, 不再读取。
    ap.add_argument("--activity", type=Path, default=None,
                    help="[deprecated/ignored] 回放只读 fixture")
    ap.add_argument("--rules", type=Path, default=None,
                    help="[deprecated/ignored] 回放只读 fixture")
    ap.add_argument("--model", default=None,
                    help="[deprecated/ignored] fixture 已固化 dense 向量")
    args = ap.parse_args()

    fixture_path = Path(args.fixture)
    raw = fixture_path.read_text(encoding="utf-8")
    fixture = json.loads(raw)
    result = evaluate_fixture(fixture)
    report = {
        "fixture": str(fixture_path),
        "fixture_sha256": _sha256_text(raw),
        "provenance": fixture.get("provenance", {}),
        "result": result,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    imp = result["implementation"]
    ok = (imp["pass_fault_le_10pct"]
          and imp["pass_relative_drop_ge_70pct"])
    print(f"[replay-fixture] hits={imp['hits']}/"
          f"{result['silver']['queries']} faults={imp['faults']} "
          f"fault_rate={imp['fault_rate']:.1%} "
          f"relative_drop={imp['relative_drop_vs_baseline']:.1%} "
          f"baseline={result['silver']['baseline_k3_048_fault_rate']:.1%} "
          f"pass={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
