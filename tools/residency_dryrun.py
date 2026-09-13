#!/usr/bin/env python3
"""tools/residency_dryrun.py — FIX8 新鲜窗口乘数合成快照只读干跑。

默认读 bundled 合成快照 tests/fixtures/snapshot_20260912
(MEMORY.md + MEMORY.meta.json), 也可用 MEMORYCORE_REALDATA_DIR 或
--realdata-dir 指向你自己的数据目录。对同一 need_chars 分别模拟:
  - before: 新鲜窗口关闭 (RULE_MIN_RESIDENCY_DAYS=0) = 旧纯 _rule_rank;
  - after : 新鲜窗口开启 (默认 7d) = rank ×GRACE_MULT (统一候选池).
用临时副本逐条取候选并按生产 `_select_retirement_candidates` 语义删除,
从而输出完整换出顺序; 每条含 rank / 是否新鲜 / 是否被选 (目标 need 以内)。
输入快照只读, 不修改快照文件。默认输出 tools/residency_dryrun.json
是本地报告产物 (已加入 .gitignore), 重跑会生成你自己的报告, 不进版本库。

用法:
  .venv/bin/python tools/residency_dryrun.py
  .venv/bin/python tools/residency_dryrun.py --need-chars 1029 \
      --realdata-dir tests/fixtures/snapshot_20260912 \
      --out /tmp/residency_dryrun.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parent.parent  # memorycore/
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    _rule_rank, _soft_residency_grace_ts,
)
from memorycore.local_store import LocalStore  # noqa: E402


def _load_snapshot(realdata_dir: Path):
    md = realdata_dir / "MEMORY.md"
    meta = realdata_dir / "MEMORY.meta.json"
    if not md.exists() or not meta.exists():
        raise SystemExit(f"快照缺失: {md} / {meta}")
    store = LocalStore(md, realdata_dir / "USER.md")
    ms = MetaStore("memory", memory_path=md,
                   user_path=realdata_dir / "USER.md")
    entries = store.entries("memory")
    metas: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        metas[e] = dict(ms.get_entry(e) or {})
    return md, meta, entries, metas


def _simulate_order(md_src: Path, meta_src: Path, entries: List[str],
                    metas: Dict[str, Dict[str, Any]], *,
                    soft_days: int, need_chars: int,
                    ref_now: datetime) -> Dict[str, Any]:
    """在临时副本上逐条模拟换出, 返回 (完整候选顺序 + selected flags)。"""
    with tempfile.TemporaryDirectory(prefix="fix5-residency-") as td:
        td_path = Path(td)
        mem_copy = td_path / "MEMORY.md"
        meta_copy = td_path / "MEMORY.meta.json"
        shutil.copy2(md_src, mem_copy)
        shutil.copy2(meta_src, meta_copy)
        store = LocalStore(mem_copy, td_path / "USER.md")
        ms = MetaStore("memory", memory_path=mem_copy,
                       user_path=td_path / "USER.md")

        saved_ov = ov.RULE_MIN_RESIDENCY_DAYS
        saved_cfg = getattr(config_mod, "RULE_MIN_RESIDENCY_DAYS", 7)
        ov.RULE_MIN_RESIDENCY_DAYS = soft_days
        config_mod.RULE_MIN_RESIDENCY_DAYS = soft_days
        try:
            order: List[Dict[str, Any]] = []
            freed_before = 0
            while True:
                batch = ov._select_retirement_candidates(
                    store, ms, "memory", 1, {}, include_stubs=False)
                if not batch:
                    break
                e = batch[0]
                if e not in store.entries("memory"):
                    break
                m = dict(metas.get(e) or {})
                gts = _soft_residency_grace_ts(m, ref_now)
                rank = _rule_rank(e, m, ref_now)
                selected = freed_before < need_chars
                order.append({
                    "order": len(order),
                    "chars": len(e),
                    "rank": round(rank, 6),
                    "in_grace": gts is not None,
                    "grace_ts": gts.isoformat() if gts is not None else None,
                    "written_at": m.get("written_at"),
                    "last_recall_hit_at": m.get("last_recall_hit_at"),
                    "selected": selected,
                    "freed_before": freed_before,
                    "text": e,
                })
                freed_before += len(e)
                res = store.remove_by_exact("memory", e)
                if not res.get("success"):
                    # 兜底: 理论上不应发生; 防止死循环。
                    break
        finally:
            ov.RULE_MIN_RESIDENCY_DAYS = saved_ov
            config_mod.RULE_MIN_RESIDENCY_DAYS = saved_cfg

    selected_entries = [c for c in order if c["selected"]]
    selected_chars = sum(c["chars"] for c in selected_entries)
    return {
        "soft_residency_days": soft_days,
        "order": order,
        "selected_count": len(selected_entries),
        "selected_chars": selected_chars,
        "need_satisfied": selected_chars >= need_chars,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="FIX5 软驻留合成快照干跑")
    default_realdata = Path(os.environ.get(
        "MEMORYCORE_REALDATA_DIR", str(ROOT / "realdata")))
    if not default_realdata.exists():
        # bundled read-only snapshot fixture (no production data required)
        default_realdata = ROOT / "tests" / "fixtures" / "snapshot_20260912"
    ap.add_argument("--realdata-dir", type=Path, default=default_realdata,
                    help="含 MEMORY.md/MEMORY.meta.json 的快照目录")
    ap.add_argument("--budget-chars", type=int, default=None,
                    help="规则预算 (默认 core.config.RULE_BUDGET_CHARS)")
    ap.add_argument("--need-chars", type=int, default=None,
                    help="需换出字数 (默认 = 快照内容字数 - budget)")
    ap.add_argument("--out", type=Path,
                    default=ROOT / "tools" / "residency_dryrun.json",
                    help="输出 JSON 路径 (默认文件已 gitignore, 重跑生成自己的报告)")
    ap.add_argument("--ref-now", default=None,
                    help="参考 now (ISO8601; 默认当前时间; 便于证据复现)")
    args = ap.parse_args()

    realdata_dir = args.realdata_dir.resolve()
    # 发布/报告路径中性化: bundled 快照输出相对仓库路径, 不把本机绝对路径
    # 写进生成的报告; 外部自定义目录保持原路径。
    try:
        display_dir = str(realdata_dir.relative_to(ROOT))
    except ValueError:
        display_dir = str(realdata_dir)
    md, meta_path, entries, metas = _load_snapshot(realdata_dir)
    total_chars = sum(len(e) for e in entries)
    budget = args.budget_chars
    if budget is None:
        budget = int(getattr(config_mod, "RULE_BUDGET_CHARS", 2000))
    need = args.need_chars
    if need is None:
        need = max(total_chars - budget, 0)
    if args.ref_now:
        ref_now = datetime.fromisoformat(args.ref_now)
        if ref_now.tzinfo is None:
            ref_now = ref_now.replace(tzinfo=timezone.utc)
    else:
        ref_now = datetime.now(timezone.utc)
    default_days = int(getattr(config_mod, "RULE_MIN_RESIDENCY_DAYS", 7))

    before = _simulate_order(md, meta_path, entries, metas,
                             soft_days=0, need_chars=need, ref_now=ref_now)
    after = _simulate_order(md, meta_path, entries, metas,
                            soft_days=default_days, need_chars=need,
                            ref_now=ref_now)

    # 当天新写入标识: 快照里 written_at 取最大日期的那批 (真实数据 = 2026-09-13
    # 两条新写入)。也额外输出所有宽限条目位置, 便于人工核对。
    def _max_written_date() -> str:
        vals = [str((metas.get(e) or {}).get("written_at") or "")[:10]
                for e in entries]
        vals = [v for v in vals if v]
        return max(vals) if vals else ""

    max_date = _max_written_date()
    new_entries = [e for e in entries
                   if str((metas.get(e) or {}).get("written_at") or "")[:10]
                   == max_date]
    def _pos(order_doc, target_entries):
        return [c["order"] for c in order_doc["order"] if c["text"] in target_entries]

    before_pos = _pos(before, new_entries)
    after_pos = _pos(after, new_entries)
    before_order = [c["text"] for c in before["order"]]
    after_order = [c["text"] for c in after["order"]]

    full_after = _simulate_order(md, meta_path, entries, metas,
                                 soft_days=default_days,
                                 need_chars=total_chars + 1,
                                 ref_now=ref_now)
    out: Dict[str, Any] = {
        "generated_at": ref_now.isoformat(),
        "snapshot": {
            "realdata_dir": display_dir,
            "entry_count": len(entries),
            "content_chars": total_chars,
        },
        "budget_chars": budget,
        "need_chars": need,
        "soft_residency_days": default_days,
        "grace_mult": float(getattr(config_mod, "GRACE_MULT", 1.0)),
        "before_multiplier_disabled": before,
        "after_multiplier_enabled": after,
        # 兼容旧字段名 (FIX5/7 调用方若直接读 JSON).
        "before_soft_residency_disabled": before,
        "after_soft_residency_enabled": after,
        "full_pressure_after": full_after,
        "acceptance": {
            "newly_written_entries_text": new_entries,
            "newly_written_date": max_date,
            "before_positions": before_pos,
            "after_positions": after_pos,
            "new_not_first_seven_after": all(p >= 7 for p in after_pos),
            "new_not_first_two_after": all(p >= 2 for p in after_pos),
            "new_not_first_two_before": all(p >= 2 for p in before_pos),
            "before_first_two": before_order[:2],
            "after_first_two": after_order[:2],
            "before_need_satisfied": before["need_satisfied"],
            "after_need_satisfied": after["need_satisfied"],
            # 足够压力 (need=全量+1) 下新写入仍被统一池选走, 非资格豁免。
            "new_evictable_when_full": all(
                e in {c["text"] for c in full_after["order"]}
                for e in new_entries),
            "full_after_need_satisfied": full_after["need_satisfied"],
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2),
                         encoding="utf-8")
    print(f"Wrote {args.out}")
    print(f"entries={len(entries)} chars={total_chars} "
          f"need={need} budget={budget}")
    print(f"before_first_two={before_order[:2]}")
    print(f"after_first_two={after_order[:2]}")
    print(f"new_entry_positions before={before_pos} after={after_pos} "
          f"(要求 after 不在前 7)")
    print(f"need_satisfied before={before['need_satisfied']} "
          f"after={after['need_satisfied']} "
          f"(selected_chars before={before['selected_chars']} "
          f"after={after['selected_chars']})")
    print(f"full_pressure new_evictable="
          f"{out['acceptance']['new_evictable_when_full']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
