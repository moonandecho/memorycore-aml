#!/usr/bin/env python3
"""tests/test_e2e_sink_convergence.py — §6.2 用例 11: 全链路验收 (DESIGN §Q6)。

以 25 条合成快照构造 LocalStore + Mock 冷层, 验证机械收敛阶梯:
  1. 迁移 (retype_20260912) → 2598 chars / 51% (19 条合成 rule)
  2. 统一预算连续换出 → 规则/state 内容 ≤2000 chars (40%)
  3. 指针预算独立核算 → stub 总字符也 ≤2000 chars
安全不变式: 冷层写成功才删本地; 每轮挤 ≤MAX_EVICT_PER_RUN; 离热全文冷层可查。
"""
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))

import retype_20260912 as rt  # noqa: E402

from conftest import MockMnemosyneClient  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core import overflow as ov  # noqa: E402

FX = Path(__file__).resolve().parent / "fixtures" / "snapshot_20260912"
FILES = ["MEMORY.md", "MEMORY.meta.json", "USER.md", "USER.meta.json"]


def _setup(tmp_path):
    d = tmp_path / "data"
    d.mkdir()
    for f in FILES:
        shutil.copy2(FX / f, d / f)
    store = LocalStore(memory_path=d / "MEMORY.md", user_path=d / "USER.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    return store, ms


def _load_queries(tmp_path, days=7):
    """把快照查询灌进活动日志 (与采集面同格式, 只留快照参考时点前 7 天)。

    合成快照是冻结语料, 因此以文件内最大时间戳为参考 now, 保证任何日期
    重跑都得到同一窗口; 不存在未来/过期导致的挂钟依赖。
    """
    from memorycore.core import metadata as meta_mod

    parsed = []
    for line in (FX / "recent_queries.jsonl").read_text(
            encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
            ts = datetime.fromisoformat(str(d.get("ts", "")).replace("Z", "+00:00"))
        except ValueError:
            continue
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        parsed.append((ts, d))
    ref_now = max((ts for ts, _ in parsed), default=datetime.now(timezone.utc))
    cut = ref_now - timedelta(days=days)
    kept = []
    for ts, d in parsed:
        if ts >= cut:
            q = (d.get("query") or "").strip()
            if q:
                kept.append({"ts": ts.isoformat(), "query": q})
    log = meta_mod.ACTIVITY_LOG_FILE
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as f:
        for row in kept:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return len(kept)


def _cold_has_text(client, text):
    """Mock 冷层是否保有某段全文 (remember 与 update 都计入)。"""
    for c in getattr(client, "_cold", []):
        if (c.get("content") or "") == text:
            return True
    for _, merged in getattr(client, "updated", []):
        if text in merged:
            return True
    return False


def test_full_chain_convergence(tmp_path, monkeypatch):
    """迁移 → 统一预算连续换出至规则生态 <= 2000 字符 (CACHE-POLICY-V2)。

    安全不变式: 冷层写成功才删/替换本地; 所有离热全文在冷层可查;
    stub <= STUB_MAX_CHARS; errors==0; 每轮换出 <= MAX_EVICT_PER_RUN 由
    enforce_rule_budget 单批封顶 (这里只验证最终收敛与零丢失)。
    """
    from memorycore.core import config as config_mod
    from memorycore.core import metadata as meta_mod
    from memorycore.core.config import RULE_BUDGET_CHARS, STUB_MAX_CHARS
    from memorycore.core.overflow import _is_protected_rule
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    monkeypatch.setattr(config_mod, "RULE_BUDGET_ENABLED", True)
    store, ms = _setup(tmp_path)
    n_queries = _load_queries(tmp_path)
    assert n_queries > 0, "活动日志应至少包含一条 7 天窗口查询"
    client = MockMnemosyneClient()

    # ---- 1. 迁移: 6 条 state 归位 (结构断言) ----
    import hashlib
    before_entries = list(store.entries("memory"))
    before_hashes = {hashlib.sha256(e.encode()).hexdigest()
                     for e in before_entries}
    stat = {"errors": 0}
    rt._apply(store, client, ms, "memory", stat)
    migrated_total = (stat.get("migrated_new", 0) + stat.get("migrated_same", 0)
                      + stat.get("migrated_merged", 0))
    assert migrated_total == len(rt.TARGET_ENTRIES), stat
    assert stat["errors"] == 0, stat
    ms.reconcile(store.entries("memory"))
    after_hashes = {hashlib.sha256(e.encode()).hexdigest()
                    for e in store.entries("memory")}
    assert after_hashes == before_hashes - rt.TARGET_STATE_SET
    for h, tag in rt.TARGET_ENTRIES.items():
        original = next(e for e in before_entries
                        if hashlib.sha256(e.encode()).hexdigest() == h)
        assert _cold_has_text(client, original), f"{tag} 全文应在冷层"

    # ---- 2. 连续统一预算换出 (F2 两阶段计数) --------------------------
    # content_chars = rule/state 全文; stub_chars = 指针自身预算, 两者分开。
    original_rules = []
    for e in store.entries("memory"):
        m = ms.get_entry(e) or {}
        if m.get("type") == "rule":
            original_rules.append(e)
    assert original_rules, "迁移后应存在 rule 供换出"

    def _content_chars(entries):
        return sum(len(e) for e in entries
                   if (ms.get_entry(e) or {}).get("type") in ("rule", "state"))

    for _round in range(12):
        r = ov.run_overflow(store, client, "memory")
        assert r.get("errors", 0) == 0, r
        ents = store.entries("memory")
        content_chars = _content_chars(ents)
        remaining_content = [e for e in ents
                             if (ms.get_entry(e) or {}).get("type")
                             in ("rule", "state")]
        if content_chars <= RULE_BUDGET_CHARS or not remaining_content:
            break
    ents = store.entries("memory")
    content_chars = _content_chars(ents)
    stub_chars = sum(
        len(e) for e in ents
        if (ms.get_entry(e) or {}).get("type") == "stub")
    # F2: 内容预算必须收敛; 指针预算独立核算 (≤RULE_BUDGET_CHARS)。
    assert content_chars <= RULE_BUDGET_CHARS, content_chars
    assert stub_chars <= RULE_BUDGET_CHARS, stub_chars

    # ---- 3. 安全不变式: 离热全文全部在冷层, stub 上限 ----
    for e in original_rules:
        if e not in ents:
            assert _cold_has_text(client, e), e[:40]
    stubs = [e for e in ents if e.startswith(ov.STUB_PREFIX)]
    assert all(len(s) <= STUB_MAX_CHARS for s in stubs), "stub 长度越界"
    assert store.usage_pct("memory") <= 55, store.usage_pct("memory")
