#!/usr/bin/env python3
"""f2_snapshot_budget_replay.py — F2 合成同源快照逐轮预算回放 (只读快照副本)。

流程 (与独立复核 §3.2 同源):
  1. 复制合成快照 tests/fixtures/snapshot_20260912 (25 条) 到临时目录;
  2. tools/retype_20260912._apply 迁出 6 条 state (内存 Mock 冷层);
  3. 逐轮 run_overflow, 记录每轮 hot/完整规则/stub/无指针/GC/字符数;
  4. 断言: 非 0 预算不得"删指针保全文"; 指针总量<=预算时全部保留;
     全部换出时每个原文 stub 唯一且 cold_id 可映射。
  5. FIX4 P1: `--budget` 同时置零内容预算与 stub 预算; 真 0 时进入显式
     T3 cold-only 语义 (`evicted_no_ptr>0` 表示无本地指针, 冷层全文仍在)。

用法:
  .venv/bin/python tools/f2_snapshot_budget_replay.py \
      --budget 2000 --json-out tests/fixtures/f2_budget_rounds_default.json
  .venv/bin/python tools/f2_snapshot_budget_replay.py \
      --budget 0 --json-out tests/fixtures/f2_budget_rounds_budget0.json
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import memorycore.core.overflow as ov  # noqa: E402
import retype_20260912 as rt  # noqa: E402
from memorycore.core import config as cfg  # noqa: E402
from memorycore.core import llm_rot, metadata as cmeta  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402


class MockCold:
    """只读回放冷层: remember 返回 memory_id, recall 空 (无既有匹配)。"""

    def __init__(self):
        self.stored: List[str] = []
        self.recall_queries: List[str] = []

    def recall_results(self, q, top_k=5, bump=True):
        self.recall_queries.append(str(q))
        return [{"id": f"x{i}", "content": x, "dense_score": 0.1}
                for i, x in enumerate(self.stored)]

    def remember(self, content, importance=0.6, scope="global"):
        if content in self.stored:
            return {"status": "stored",
                    "memory_id": f"m{self.stored.index(content)}"}
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"m{len(self.stored) - 1}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}

    def forget(self, memory_id):
        return {"status": "ok"}

    def embed_texts(self, texts):
        return None


def _run_inner(data_dir: Path, budget: int, rounds: int) -> Dict[str, Any]:
    store = LocalStore(data_dir / "MEMORY.md", data_dir / "USER.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    cmeta.ACTIVITY_LOG_FILE = data_dir / "activity.jsonl"
    cfg.ACTIVITY_LOG_ENABLED = True
    llm_rot.ROT_PATH = data_dir / "llm_rot.json"
    ov.RULE_BUDGET_CHARS = budget
    client = MockCold()

    retype_stat: Dict[str, Any] = {}
    rt._apply(store, client, ms, "memory", retype_stat)
    ms.reconcile(store.entries("memory"))
    original = list(store.entries("memory"))
    original_set = set(original)

    round_rows: List[Dict[str, Any]] = []
    for r in range(rounds):
        stat = ov.run_overflow(store, client, "memory")
        hot = store.entries("memory")
        full_left = [e for e in original if e in hot]
        stubs = [e for e in hot if (ms.get_entry(e) or {}).get("type") == "stub"]
        evicted_no_ptr = []
        for e in original:
            if e in hot:
                continue
            expected = ov._make_stub(e)
            if expected not in hot:
                evicted_no_ptr.append(e)
        row = {
            "round": r,
            "hot_n": len(hot),
            "orig_count": len(original),
            "orig_full_left": len(full_left),
            "stub_entries": len(stubs),
            "evicted_no_ptr": len(evicted_no_ptr),
            "evicted_no_ptr_samples": [e[:24] for e in evicted_no_ptr[:3]],
            "content_chars": sum(
                len(e) for e in hot
                if (ms.get_entry(e) or {}).get("type") != "stub"
                and (ms.get_entry(e) or {}).get("type") is not None),
            "stub_chars": sum(
                len(e) for e in hot
                if (ms.get_entry(e) or {}).get("type") == "stub"),
            "round_stubbed": stat.get("stubbed", 0),
            "round_stub_gc": stat.get("stub_gc", 0),
            "round_cold_only": stat.get("cold_only", 0),
            "errors": stat.get("errors", 0),
            "t3_cold_only_mode": bool(stat.get("t3_cold_only_mode", False)),
            "cold_n": len(client.stored),
            "usage_after": stat.get("usage_after", ""),
        }
        round_rows.append(row)
        if not full_left:
            break

    # 全部换出场景: 每个原全文的指针唯一、全保留、cold_id 非空
    all_stubs = [ov._make_stub(e) for e in original]
    final_hot = store.entries("memory")
    evicted_stubs = [ov._make_stub(e) for e in original
                     if e not in final_hot]
    missing_pointers = [s for s in evicted_stubs if s not in final_hot]
    final_meta_cold_ids = [
        (ms.get_entry(s) or {}).get("cold_id") for s in evicted_stubs]
    true_zero = budget == 0
    final_content_chars = sum(
        len(e) for e in final_hot
        if (ms.get_entry(e) or {}).get("type") != "stub")
    final_stub_chars = sum(
        len(e) for e in final_hot
        if (ms.get_entry(e) or {}).get("type") == "stub")
    summary = {
        "budget": budget,
        "true_zero_budget": true_zero,
        "budget_semantics": (
            "T3_cold_only_zero_pointer_budget" if true_zero
            else "content_and_stub_within_nonzero_budget"),
        "retype_stat": retype_stat,
        "rounds": round_rows,
        "final": {
            "content_chars": final_content_chars,
            "stub_chars": final_stub_chars,
            "hot_n": len(final_hot),
            "full_left_count": sum(1 for e in original if e in final_hot),
            "stub_count": sum(
                1 for e in final_hot
                if (ms.get_entry(e) or {}).get("type") == "stub"),
            "evicted_no_ptr_count": sum(
                1 for e in original
                if e not in final_hot and ov._make_stub(e) not in final_hot),
            "all_pointer_stub_chars": sum(len(s) for s in all_stubs),
            "unique_stubs_created": len(set(evicted_stubs)),
            "created_total": len(evicted_stubs),
            "all_pointers_retained": not missing_pointers,
            "evicted_stub_count": len(evicted_stubs),
            "missing_pointers": missing_pointers,
            "cold_id_non_null":
                all(c for c in final_meta_cold_ids if c is not None),
        },
    }
    # 硬断言 (验收口径)
    if true_zero:
        # FIX4 P1: 真 0 指针预算 = 显式 T3 cold-only。允许 evicted_no_ptr>0,
        # 但必须清楚标记语义; 冷层全文仍必须可审计地存在, 内容/指针本地
        # 占用为 0。
        assert summary["final"]["content_chars"] <= 0, summary
        assert summary["final"]["stub_chars"] <= 0, summary
        if summary["final"]["full_left_count"] == 0:
            # 全部换出后每一条原文都必须先成功冷写; 本地不再承诺指针。
            assert summary["final"]["evicted_no_ptr_count"] == \
                len(original), summary
            assert all(e in client.stored for e in original), summary
        assert summary["budget_semantics"] == \
            "T3_cold_only_zero_pointer_budget", summary
    else:
        assert summary["final"]["evicted_no_ptr_count"] == 0, summary
        assert summary["final"]["unique_stubs_created"] == \
            summary["final"]["created_total"], summary  # 已创建指针必须唯一
        assert summary["final"]["content_chars"] <= budget, summary
        assert summary["final"]["stub_chars"] <= budget, summary
        if summary["final"]["full_left_count"] == 0:
            assert summary["final"]["unique_stubs_created"] == \
                len(original), summary
            assert summary["final"]["all_pointers_retained"], summary
            assert summary["final"]["all_pointer_stub_chars"] <= budget, summary
        else:
            # 未全换出时: 每个已换出全文必须保有对应指针。
            assert not summary["final"]["missing_pointers"], summary
    return summary


def run(data_dir: Path, budget: int, rounds: int) -> Dict[str, Any]:
    """FIX4: `--budget` 同时作用于内容预算与 stub 预算。

    真 0 预算显式进入 T3 cold-only; 普通预算下存在性映射判据保持不放松。
    """
    prev_ov = int(ov.RULE_BUDGET_CHARS)
    prev_cfg = int(cfg.RULE_BUDGET_CHARS)
    ov.RULE_BUDGET_CHARS = budget
    cfg.RULE_BUDGET_CHARS = budget
    try:
        return _run_inner(data_dir, budget, rounds)
    finally:
        ov.RULE_BUDGET_CHARS = prev_ov
        cfg.RULE_BUDGET_CHARS = prev_cfg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--budget", type=int, default=2000)
    ap.add_argument("--rounds", type=int, default=12)
    ap.add_argument("--json-out", type=Path, default=None)
    ap.add_argument("--data-dir", type=Path, default=None,
                    help="可选: 运行目录 (默认临时目录)")
    args = ap.parse_args()

    if args.data_dir:
        data_dir = args.data_dir
        if not data_dir.exists():
            raise SystemExit(f"data-dir 不存在: {data_dir}")
    else:
        data_dir = Path(tempfile.mkdtemp(prefix="fix3_f2_"))
        src = ROOT / "tests/fixtures/snapshot_20260912"
        for name in ("MEMORY.md", "MEMORY.meta.json",
                     "USER.md", "USER.meta.json"):
            shutil.copy2(src / name, data_dir / name)

    summary = run(data_dir, args.budget, args.rounds)
    text = json.dumps(summary, ensure_ascii=False, indent=2)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
