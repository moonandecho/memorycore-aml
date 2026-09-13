#!/usr/bin/env python3
"""fix4_p1_extremes.py — FIX4 P1 极多短全文极端逐轮回放。

用途:
  * 验证 120 条 20 字短规则 + 真 0 预算 (内容预算与 stub 预算同为 0)
    不会出现 "冷写成功→本地 5000 硬顶 replace 失败→errors 卡死" 平台;
  * 同时可用 `--code-dir` 指向修复前副本, 生成修复前逐轮 JSON 对照。

用法:
  .venv/bin/python tools/fix4_p1_extremes.py --json-out /tmp/p1_after.json
  .venv/bin/python tools/fix4_p1_extremes.py \
      --code-dir ../memorycore-pre-fix4 --json-out /tmp/p1_before.json
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List


class MockCold:
    """无查重命中冷层: 每次 remember 都返回新 cold id, recall 空。"""

    def __init__(self) -> None:
        self.stored: List[str] = []

    def recall_results(self, query, top_k=5, bump=True):
        return []

    def remember(self, content, importance=0.6, scope="global"):
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"m{len(self.stored) - 1}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}

    def forget(self, memory_id):
        return {"status": "ok"}


def _load_code(code_dir: Path):
    code_dir = code_dir.resolve()
    sys.path.insert(0, str(code_dir))
    import memorycore.core.overflow as ov  # noqa: E402
    from memorycore import local_store as ls  # noqa: E402
    from memorycore.core import config as cfg  # noqa: E402
    from memorycore.core.metadata import MetaStore  # noqa: E402
    return ov, ls.LocalStore, cfg, MetaStore


def _make_short_entries(n: int, length: int = 20) -> List[str]:
    """生成 n 条不同前缀的短全文, 每条固定 length 字。"""
    out = []
    for i in range(n):
        head = f"{i:04d}号规则:"
        body = ("甲乙丙丁戊己庚辛壬癸子丑寅卯辰巳午未" * 4)
        entry = (head + body)[:length]
        if len(entry) < length:
            entry += "戊" * (length - len(entry))
        out.append(entry)
    assert len(set(out)) == n, "短规则必须互不相同"
    return out


def run_short120(code_dir: Path, n: int = 120,
                 entry_len: int = 20,
                 budget: int = 0,
                 rounds: int = 20) -> Dict[str, Any]:
    ov, LocalStore, cfg, MetaStore = _load_code(code_dir)
    old = datetime.now(timezone.utc) - timedelta(days=90)
    tmp = Path(tempfile.mkdtemp(prefix="fix4_p1_short_"))
    store = LocalStore(tmp / "MEMORY.md", tmp / "USER.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    client = MockCold()
    entries = _make_short_entries(n, entry_len)
    for e in entries:
        store.add("memory", e)
        ms.stamp(e, "rule", weight=1.0, updated_at=old,
                 last_active_at=old)
    original = list(store.entries("memory"))

    prev_ov = int(ov.RULE_BUDGET_CHARS)
    prev_cfg = int(cfg.RULE_BUDGET_CHARS)
    ov.RULE_BUDGET_CHARS = budget
    cfg.RULE_BUDGET_CHARS = budget
    rows: List[Dict[str, Any]] = []
    try:
        for r in range(rounds):
            stat: Dict[str, Any] = {}
            exc = ""
            try:
                ov.enforce_rule_budget(store, client, "memory", ms, stat)
            except Exception as e:  # pragma: no cover - 观测路径
                exc = repr(e)
            hot = store.entries("memory")
            content_entries = []
            stub_entries = []
            for e in hot:
                m = ms.get_entry(e) or {}
                typ = m.get("type")
                if typ == "stub" or (typ is None
                                     and e.startswith(ov.STUB_PREFIX)):
                    stub_entries.append(e)
                else:
                    content_entries.append(e)
            expected_missing = []
            for e in original:
                if e in hot:
                    continue
                if ov._make_stub(e) not in hot:
                    expected_missing.append(e)
            row = {
                "round": r,
                "content_n": len(content_entries),
                "stub_n": len(stub_entries),
                "content_chars": sum(len(e) for e in content_entries),
                "stub_chars": sum(len(e) for e in stub_entries),
                "total_chars": store.char_count("memory"),
                "round_stubbed": stat.get("stubbed", 0),
                "round_cold_only": stat.get("cold_only", 0),
                "errors": stat.get("errors", 0),
                "stub_gc": stat.get("stub_gc", 0),
                "pressure_batches": stat.get("pressure_batches", 0),
                "t3_cold_only_mode": bool(
                    stat.get("t3_cold_only_mode", False)),
                "evicted_no_ptr": len(expected_missing),
                "evicted_no_ptr_samples": [e[:24] for e in expected_missing[:3]],
                "cold_n": len(client.stored),
                "exception": exc,
            }
            rows.append(row)
            if not content_entries:
                break
    finally:
        ov.RULE_BUDGET_CHARS = prev_ov
        cfg.RULE_BUDGET_CHARS = prev_cfg

    final = rows[-1] if rows else {}
    return {
        "scenario": "short_120_texts_20chars_true_zero_budget",
        "code_dir": str(code_dir.resolve()),
        "budget": budget,
        "true_zero_budget": budget == 0,
        "budget_semantics": (
            "T3_cold_only_zero_pointer_budget" if budget == 0
            else "nonzero_budget_pointer_required"),
        "entry_count": n,
        "entry_len": entry_len,
        "rounds_requested": rounds,
        "rounds_observed": len(rows),
        "rounds": rows,
        "final": final,
        "pass_no_errors_after_fix": bool(
            rows and all(r["errors"] == 0 for r in rows)
            and final.get("content_chars", 0) <= budget),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--code-dir", type=Path,
                    default=Path(__file__).resolve().parents[1],
                    help="core 实现目录 (默认当前 memorycore)")
    ap.add_argument("--count", type=int, default=120)
    ap.add_argument("--entry-len", type=int, default=20)
    ap.add_argument("--budget", type=int, default=0)
    ap.add_argument("--rounds", type=int, default=20)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()
    report = run_short120(args.code_dir, args.count, args.entry_len,
                          args.budget, args.rounds)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0 if report["pass_no_errors_after_fix"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
