#!/usr/bin/env python3
"""tools/retype_20260912.py — 示例：把被误判为 rule 的完成态记录重标为
state（配 bundled 合成快照）。

把"穿 rule 衣服的历史决策/状态记录"重标为 state 并立即冷迁移, 热层从
70% 机械降到 ~51% (2598/5000 chars; 合成快照基线)。目标条目按 sha256
内容精确锁定 (清单硬编码, 不随环境漂移):

  #19 巡检归档 / #20 回滚演练 / #22 旧版导出器退役 /
  #23 构建流水线迁移 / #24 文档站调研 / #25 缓存策略评审

用法 (默认 --dry-run, 不落盘):
  .venv/bin/python tools/retype_20260912.py --dry-run
  .venv/bin/python tools/retype_20260912.py --apply
  --data-dir DIR    数据目录覆盖 (默认 $MEMORY_DIR 或 ~/.hermes/memories)
  --backup-dir DIR  备份目录覆盖 (默认 $MEMORYCORE_BACKUP_DIR 或 ~/.memorycore/backups)

安全铁律:
  - 冷层写成功才删本地 (迁移前 recall 查重: same 不重写 / similar merge-update /
    无匹配 remember); 任一冷层失败 → 该条原样留热层并盖章 state 兜底, errors+1。
  - 删除走 LocalStore.remove_by_exact (回收队列 30 天机制不受影响)。
  - 备份: MEMORY.md / MEMORY.meta.json / USER.md / USER.meta.json 复制到
    backups/retype-20260912-<ts>/ 并记录 sha256; 回滚 = 停止写入后原子恢复。
  - 幂等: sha256 清单匹配 + recall 查重 + exact remove 三重防双写/双删;
    可重复运行。冷层不回滚 (回滚后热冷短期冗余由 S5 跨层 dedup 收敛)。
"""
from __future__ import annotations

import argparse
import hashlib
import os
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

from memorycore.local_store import LocalStore  # noqa: E402
from memorycore.core.classifier import classify_entry_type  # noqa: E402
from memorycore.core.metadata import (MetaStore, parse_embedded_date,  # noqa: E402
                           _ts_anchor)
from memorycore.core.config import MEMORY_DIR, IMPORTANCE_PROTECT  # noqa: E402

# 迁移清单 (sha256 内容精确锁定; 条目编号对应合成快照 MEMORY.md 逐条顺序)
TARGET_ENTRIES = {
    "3328d9ea10b4c569c9e75cb0042d14f66121608c0ac2fd4b643e08a2e5363980": "#19 巡检归档",
    "776e2a5787b3b01e4399b1f3166bdada2c01ce819c9b45e8d7527bb3926c8a44": "#20 回滚演练",
    "bc52e58f541d20bfb06a73e2cdb438aea95b52d71d8c1947c785f47a15a8b917": "#22 旧版导出器退役",
    "c7a022b669307a132df312eb96e83d1cfab2e5ba38d3b6c3caede7454d8cca19": "#23 构建流水线迁移",
    "28d76b05cda58fbf20cd7f8fc05cb7a8b97ab36819116b8bf081df1ecf37fe8b": "#24 文档站调研",
    "e568bfd5aeb87f465430129609c82f571d04fcebd2022aa87c06ac2873e83009": "#25 缓存策略评审",
}
TARGET_STATE_SET = set(TARGET_ENTRIES)  # 干跑校验: state 集合必须恰为这 6 条

BACKUP_FILES = ["MEMORY.md", "MEMORY.meta.json", "USER.md", "USER.meta.json"]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _backup(data_dir: Path, backup_root: Path) -> Path:
    """复制 4 文件 + sha256 manifest → 返回备份目录 (幂等: 时间戳区分)。"""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    bdir = backup_root / f"retype-20260912-{ts}"
    bdir.mkdir(parents=True, exist_ok=True)
    manifest = {}
    for name in BACKUP_FILES:
        src = data_dir / name
        if src.exists():
            dst = bdir / name
            shutil.copy2(src, dst)
            manifest[name] = {"sha256": _sha256(dst), "bytes": dst.stat().st_size}
        else:
            manifest[name] = {"sha256": None, "bytes": 0, "missing": True}
    (bdir / "BACKUP-MANIFEST.json").write_text(
        json.dumps({"created_at": datetime.now(timezone.utc).isoformat(),
                    "source_dir": str(data_dir), "files": manifest},
                   ensure_ascii=False, indent=1), encoding="utf-8")
    return bdir


def _classify_report(entries):
    """干跑判型: 返回 (rows, rule_n, state_n, state_hashes)。"""
    rows, state_hashes = [], []
    for i, e in enumerate(entries, 1):
        h = hashlib.sha256(e.encode()).hexdigest()
        t = classify_entry_type(e)
        rows.append((i, h, t, e))
        if t == "state":
            state_hashes.append(h)
    return rows, len(entries) - len(state_hashes), len(state_hashes), state_hashes


def _dry_run(store, metastore, target: str) -> dict:
    """干跑: 逐条输出 old_type → new_type + 校验 state 集合 = 6 条目标。"""
    entries = store.entries(target)
    rows, rule_n, state_n, state_hashes = _classify_report(entries)
    print(f"[dry-run] {target}: {len(entries)} 条, 新判型 = "
          f"{rule_n} rule / {state_n} state")
    for i, h, t, e in rows:
        old = (metastore.get_entry(e) or {}).get("type", "legacy")
        tag = TARGET_ENTRIES.get(h, "")
        arrow = f"{old} -> {t}"
        if tag:
            arrow += f"  [迁移目标 {tag}]"
        print(f"  #{i:>2} [{h[:12]}] {arrow} :: {e[:42]}")
    state_set = set(state_hashes)
    ok = (state_n == len(TARGET_STATE_SET)
          and state_set == TARGET_STATE_SET)
    print(f"[dry-run] state 集合校验: "
          f"{'通过' if ok else '失败'} "
          f"(期望 {len(TARGET_STATE_SET)} 条 = "
          f"{', '.join(sorted(TARGET_ENTRIES.values()))})")
    if not ok:
        extra = state_set - TARGET_STATE_SET
        missing = TARGET_STATE_SET - state_set
        if extra:
            print(f"  多余 state (不应被判 state): {len(extra)} 条")
        if missing:
            print(f"  缺失 state (应判 state 未判): {len(missing)} 条")
    return {"ok": ok, "rule_n": rule_n, "state_n": state_n,
            "state_hashes": sorted(state_hashes)}


def _restamp(metastore, entry, meta, is_already_state: bool) -> None:
    """重标 sidecar (只写元数据, 不碰 .md; 保留 importance/weight/
    last_active_at/cold_id)。#22 已是 state → 只补 type_source, 不改 written_at。

    FIX7 I2: 本工具是活动路径, 所有旧时间戳读取统一走 _ts_anchor
    (future 夹 now + ts_anomaly 可见), 不再直接 _parse_iso。
    """
    _sha = hashlib.sha256((entry or "").encode("utf-8")).hexdigest()

    def _ts(key: str):
        return _ts_anchor(meta.get(key), field=key, sha=_sha)

    if is_already_state:
        # #22: 保留原 written_at/origin, 只补 type_source
        metastore.stamp(
            entry, "state",
            written_at=_ts("written_at"),
            updated_at=_ts("updated_at"),
            origin=meta.get("origin", "legacy"),
            importance=float(meta.get("importance") or 0.8),
            weight=float(meta.get("weight")) if meta.get("weight") is not None else None,
            last_active_at=_ts("last_active_at"),
            cold_id=meta.get("cold_id"),
            type_source="manual_migration",
            schema=2,
            # FIX7 I4: 显式 written_at 会触发旧 fallback 清除; 本入口是
            # 同一物理条目重标 (不是人工重置出生时刻), 必须显式继承标记。
            reconcile_anchor_fallback=meta.get(
                "reconcile_anchor_fallback"),
        )
    else:
        # 5 条非 state: written_at=内嵌日期 (缺失则保留原值, 不伪造)
        d = parse_embedded_date(entry)
        if d is not None:
            # P3 (FIX8): 工具解析出的内嵌日期必须经 _ts_anchor(field=
            # "embedded_date") 再写入; 未来日期夹 now + ts_anomaly 可见.
            d = _ts_anchor(d, field="embedded_date", sha=_sha)
        metastore.stamp(
            entry, "state",
            written_at=(d or _ts("written_at")),
            updated_at=_ts("updated_at"),
            origin="migration_20260912",
            importance=float(meta.get("importance") or 0.8),
            weight=float(meta.get("weight")) if meta.get("weight") is not None else None,
            last_active_at=_ts("last_active_at"),
            cold_id=meta.get("cold_id"),
            type_source="manual_migration",
            schema=2,
            reconcile_anchor_fallback=meta.get(
                "reconcile_anchor_fallback"),
        )

def _cold_migrate_one(client, store, target: str, entry: str) -> str:
    """单条冷迁移 (与 direct_write_govern state 路径同语义)。

    返回 migrated_new / migrated_same / migrated_merged / kept_hot_backstop。
    铁律: 冷层确认成功才删本地; 删除走 remove_by_exact (回收队列 30 天不变)。
    """
    from memorycore.core.overflow import _recall_safe, _find_best_match, _merge_two_entries

    try:
        existing = _recall_safe(client, entry)
    except Exception:
        return "kept_hot_backstop"
    if existing:
        matched = _find_best_match(entry, existing)
        if matched:
            if matched["level"] == "same":
                if store.remove_by_exact(target, entry).get("success"):
                    return "migrated_same"
                return "kept_hot_backstop"
            merged = _merge_two_entries(entry, matched["content"])
            try:
                r = client.update(matched["id"], merged)
                if r.get("status") == "updated":
                    if store.remove_by_exact(target, entry).get("success"):
                        return "migrated_merged"
                    return "kept_hot_backstop"
            except Exception:
                pass  # update 失败 → 降级 remember (与 overflow 同语义)
    try:
        r = client.remember(entry, importance=0.6, scope="global")
        if r.get("status") == "stored":
            if store.remove_by_exact(target, entry).get("success"):
                return "migrated_new"
            return "kept_hot_backstop"
    except Exception:
        pass
    return "kept_hot_backstop"


def _apply(store, client, metastore, target: str, stat: dict) -> None:
    """重标 + 立即冷迁移 (幂等: 已 absent → already_absent;
    已迁移章 + 仍在热层 → 只重试冷迁移)。"""
    entries = store.entries(target)
    present_hashes = {hashlib.sha256(e.encode()).hexdigest() for e in entries}
    for h, tag in TARGET_ENTRIES.items():
        if h not in present_hashes:
            # G-2 (2026-09-12 评审修复): 幂等运行的运维可见性 —
            # 目标条目已不在热层时显式计数 already_absent。
            stat["already_absent"] = stat.get("already_absent", 0) + 1
            print(f"  {tag}: already_absent (目标已不在热层)")
    for e in entries:
        h = hashlib.sha256(e.encode()).hexdigest()
        if h not in TARGET_ENTRIES:
            continue
        tag = TARGET_ENTRIES[h]
        meta = metastore.get_entry(e) or {}
        is_already_state = meta.get("type") == "state"
        if (is_already_state and meta.get("origin") == "migration_20260912"
                and meta.get("type_source") == "manual_migration"):
            print(f"  {tag}: sidecar 已迁移章 → 只重试冷迁移")
        elif is_already_state:
            print(f"  {tag}: 已是 state → 补 type_source 后冷迁移")
            _restamp(metastore, e, meta, is_already_state=True)
        else:
            print(f"  {tag}: rule → state 重标后冷迁移")
            _restamp(metastore, e, meta, is_already_state=False)
        result = _cold_migrate_one(client, store, target, e)
        stat[result] = stat.get(result, 0) + 1
        print(f"    -> {result}")
        if result == "kept_hot_backstop":
            stat["errors"] += 1  # 冷层失败/本地删除失败: 留热层 + state 兜底章
    print(f"[apply] {target}: "
          f"migrated_new={stat.get('migrated_new', 0)} "
          f"migrated_same={stat.get('migrated_same', 0)} "
          f"migrated_merged={stat.get('migrated_merged', 0)} "
          f"already_absent={stat.get('already_absent', 0)} "
          f"kept_hot_backstop={stat.get('kept_hot_backstop', 0)} "
          f"errors={stat.get('errors', 0)}")


def main() -> int:
    ap = argparse.ArgumentParser(description="存量 6 条误判条目归位 (DESIGN §Q5)")
    ap.add_argument("--dry-run", action="store_true",
                    help="干跑校验 (默认行为, 显式传入亦可)")
    ap.add_argument("--apply", action="store_true",
                    help="执行迁移 (默认 dry-run 只读不落盘)")
    ap.add_argument("--data-dir", default=str(MEMORY_DIR),
                    help="热层数据目录 (默认 $MEMORY_DIR)")
    ap.add_argument("--backup-dir",
                    default=os.environ.get(
                        "MEMORYCORE_BACKUP_DIR",
                        str(Path.home() / ".memorycore" / "backups")),
                    help="备份根目录 (默认 $MEMORYCORE_BACKUP_DIR)")
    args = ap.parse_args()

    data_dir = Path(args.data_dir)
    store = LocalStore(memory_path=data_dir / "MEMORY.md",
                       user_path=data_dir / "USER.md")
    metastore = MetaStore("memory", memory_path=store.memory_path,
                          user_path=store.user_path)
    user_metastore = MetaStore("user", memory_path=store.memory_path,
                               user_path=store.user_path)

    print("=" * 72)
    print(f"retype_20260912 {'--apply' if args.apply else '--dry-run'} "
          f"| data-dir={data_dir}")
    print("=" * 72)

    # ---- 1. 干跑校验 (apply 前强制通过) ----
    res = _dry_run(store, metastore, "memory")
    if not res["ok"]:
        print("[拒绝] 干跑校验失败: state 集合 != {#19,#20,#22,#23,#24,#25}, "
              "不执行任何修改 (请人工排查判型)。")
        return 1
    print(f"[dry-run] memory: {res['rule_n']} rule / {res['state_n']} state "
          f"(验收口径: 19 rule / 6 state)")

    if not args.apply:
        print("[dry-run] 只读校验完成, 未修改任何文件。"
              "确认无误后加 --apply 执行。")
        return 0

    # ---- 2. 备份 ----
    bdir = _backup(data_dir, Path(args.backup_dir))
    print(f"[apply] 备份完成: {bdir} (4 文件 + sha256 manifest)")

    # ---- 3. 重标 + 立即冷迁移 ----
    from memorycore.cold_store_client import ColdStoreClient
    client = ColdStoreClient()
    stat = {"errors": 0}
    _apply(store, client, metastore, "memory", stat)

    # ---- 4. reconcile 清孤儿键 ----
    try:
        r1 = metastore.reconcile(store.entries("memory"))
        r2 = user_metastore.reconcile(store.entries("user"))
        print(f"[apply] reconcile: memory {r1}, user {r2}")
    except Exception as e:
        print(f"[apply] reconcile 失败 (不影响迁移结果, 下次溢流兜底): {e}")
        stat["errors"] += 1

    # ---- 5. 收尾统计 ----
    print(f"[apply] memory 占用: {store.char_count('memory')} chars / "
          f"{store.usage_pct('memory')}% (目标 ≤55% = 2749 chars)")
    print(f"[apply] 汇总: {json.dumps(stat, ensure_ascii=False)}")
    if stat["errors"]:
        print("[apply] 存在冷层失败兜底条目 (kept_hot_backstop): "
              "原样留热层并已盖章 state, 下次溢流自动重试, 无数据丢失。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
