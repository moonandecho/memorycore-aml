#!/usr/bin/env python3
"""fix4_f3_collision_evidence.py — FIX4 P2 旧 4-hex 碰撞 pair 实测。

输出:
  * 旧 sha256[:4] 相同 (证明这是真实旧碰撞 pair);
  * 新 16-hex 指纹唯一;
  * 真实 `_handle_rule_stub_sink` 写回两条 stub + 两个 cold_id;
  * `restore_stubs_from_results` 按 cold_id 反序恢复, 两条原文各自回热层。

用法:
  .venv/bin/python tools/fix4_f3_collision_evidence.py
"""
from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import (  # noqa: E402
    _handle_rule_stub_sink, _make_stub, restore_stubs_from_results,
)
from memorycore.local_store import LocalStore  # noqa: E402

E1 = "系统配置修改前必须通知我并确认回滚点eGPva2A0Fg"
E2 = "系统配置修改前必须通知我并确认回滚点820DfQnPOM"


class SeqCold:
    def __init__(self):
        self.stored = []
        self.seq = 0

    def recall_results(self, q, top_k=5, bump=True):
        return []

    def remember(self, content, importance=0.6, scope="global"):
        self.seq += 1
        self.stored.append(content)
        return {"status": "stored", "memory_id": f"cold-{self.seq}"}

    def update(self, memory_id, content, importance=None):
        return {"status": "updated"}

    def forget(self, memory_id):
        return {"status": "ok"}


def main() -> int:
    old_h4_1 = hashlib.sha256(E1.encode()).hexdigest()[:4]
    old_h4_2 = hashlib.sha256(E2.encode()).hexdigest()[:4]
    s1, s2 = _make_stub(E1), _make_stub(E2)
    tmp = Path(tempfile.mkdtemp(prefix="fix4_f3_"))
    store = LocalStore(tmp / "MEMORY.md", tmp / "USER.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    for e in (E1, E2):
        store.add("memory", e)
        ms.stamp(e, "rule", weight=1.0, updated_at=old,
                 last_active_at=old)
    client = SeqCold()
    stat = {}
    _handle_rule_stub_sink(store, client, "memory", E1, ms, stat, [])
    _handle_rule_stub_sink(store, client, "memory", E2, ms, stat, [])
    metab = {s: (ms.get_entry(s) or {}) for s in (s1, s2)}
    stubs_now = [e for e in store.entries("memory")
                 if (ms.get_entry(e) or {}).get("type") == "stub"]
    remaining = restore_stubs_from_results(store, {"memory": ms}, [
        {"id": metab[s2].get("cold_id"), "content": E2},
        {"id": metab[s1].get("cold_id"), "content": E1},
    ])
    ents = store.entries("memory")
    report = {
        "pair": {"e1": E1, "e2": E2},
        "legacy_sha256_4hex": {"e1": old_h4_1, "e2": old_h4_2,
                               "equal_old_collision": old_h4_1 == old_h4_2},
        "new_stub_16hex": {
            "e1": s1, "e2": s2,
            "stub_unique": s1 != s2,
            "stub_len": [len(s1), len(s2)],
        },
        "sink_stat": stat,
        "cold_stored_both": E1 in client.stored and E2 in client.stored,
        "stubs_after_sink": stubs_now,
        "stub_count_after_sink": len(stubs_now),
        "cold_id_map": {s: metab[s].get("cold_id") for s in (s1, s2)},
        "after_restore": {
            "e1_in_hot": E1 in ents,
            "e2_in_hot": E2 in ents,
            "stubs_gone": not any(s in ents for s in (s1, s2)),
            "remaining": remaining,
            "cold_id_e1_preserved":
                (ms.get_entry(E1) or {}).get("cold_id")
                == metab[s1].get("cold_id"),
            "cold_id_e2_preserved":
                (ms.get_entry(E2) or {}).get("cold_id")
                == metab[s2].get("cold_id"),
        },
    }
    report["pass"] = bool(
        report["legacy_sha256_4hex"]["equal_old_collision"]
        and report["new_stub_16hex"]["stub_unique"]
        and report["cold_stored_both"]
        and report["stub_count_after_sink"] == 2
        and report["after_restore"]["e1_in_hot"]
        and report["after_restore"]["e2_in_hot"]
        and report["after_restore"]["stubs_gone"]
        and not remaining
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
