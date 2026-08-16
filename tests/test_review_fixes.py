#!/usr/bin/env python3
"""tests/test_review_fixes.py — final-review regression locks (F1/F3/F4).

Locks the three mandatory final-review fixes:
  F1: sidecar failure never blocks overflow (reconcile/stamp exceptions
      degrade to the legacy path, errors+1)
  F3: replace failure is not counted/stamped (stat integrity)
  F4: completion-marker negation/pending exclusion
      (未定稿/未拍板/待拍板/定稿:规范... -> rule)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memorycore.core import metadata as meta_mod  # noqa: E402
from memorycore.core.classifier import classify_entry_type  # noqa: E402
from memorycore.core.overflow import run_overflow  # noqa: E402
from conftest import days_ago_str  # noqa: E402


# ---- F4: pending/negation exclusion (probes from the final review) ----

@pytest.mark.parametrize("content", [
    "2026-08-16 公众号头像方案未定稿, 两版都保留",
    "2026-08-16 迁移方案未拍板, 等用户评估后再定",
    "2026-08-16 需求清单待拍板, 用户还没确认",
    "2026-08-16 定稿: 宣传文档写作规范已确立, 必须遵守",
    "2026-08-16 需求未定案, 继续调研统计口径",
    "2026-08-16 方案定稿前两版都保留",
])
def test_f4_pending_or_rule_like_is_rule(content):
    """In-progress / precept content must never be typed state (F4 lock)."""
    assert classify_entry_type(content) == "rule", content


def test_f4_genuine_completion_still_state():
    """Genuine completion (拍板/已定稿) still types state (no over-tightening)."""
    assert classify_entry_type(
        f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案") == "state"
    assert classify_entry_type(
        f"{days_ago_str(2)} 方案已定稿, 不再更换") == "state"


# ---- F1: sidecar failure never blocks overflow ----

def test_f1_reconcile_failure_degrades_to_legacy(tmp_store, mock_client, monkeypatch):
    """reconcile raises OSError -> overflow continues via legacy keywords,
    errors+1, and the 8-day state entry still sinks through the keyword path."""
    def _boom(self, entries, now=None):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "reconcile", _boom)

    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert stat["errors"] >= 1, "sidecar failure must be counted in errors"
    # degraded to legacy: the 8-day state still sinks via the keyword path
    assert entry not in tmp_store.entries("memory")
    assert entry in mock_client.stored


def test_f1_stamp_failure_does_not_crash_overflow(tmp_store, mock_client,
                                                  monkeypatch, meta_for):
    """Compression-path stamp raises OSError -> overflow does not crash,
    compression still completes (reconcile re-stamps later)."""
    from datetime import datetime, timedelta, timezone
    from memorycore.core import overflow as ov
    filler = "".join(f"这是第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。" for i in range(1, 8))
    entry = f"用户偏好({days_ago_str(40)}): 极简选型。" + filler
    tmp_store.add("memory", entry)
    # real stamp first (backdate updated_at), then make stamp fail
    meta_for("memory").stamp(entry, "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=40))

    def _boom_stamp(self, content, entry_type, written_at=None,
                    updated_at=None, origin="hermes"):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "stamp", _boom_stamp)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)

    stat = run_overflow(tmp_store, mock_client, "memory")
    assert compressed in tmp_store.entries("memory"), "compression must complete (replace succeeded)"
    assert stat["compressed"] == 1
    assert stat["errors"] == 0, "stamp failure degrades silently (reconcile re-stamps)"


# ---- F3: replace result verification ----

def test_f3_replace_failure_not_counted(tmp_store, mock_client, monkeypatch, meta_for):
    """Compression replace fails (concurrent edit) -> compressed not counted,
    errors+1, original entry stays hot."""
    from datetime import datetime, timedelta, timezone
    from memorycore.local_store import LocalStore
    from memorycore.core import overflow as ov

    filler = "".join(f"这是第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。" for i in range(1, 8))
    entry = f"用户偏好({days_ago_str(40)}): 极简选型。" + filler
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=40))
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)

    def _fail_replace(self, target, old_text, new_content):
        return {"success": False, "error": "concurrent edit (mock)"}
    monkeypatch.setattr(LocalStore, "replace", _fail_replace)

    stat = run_overflow(tmp_store, mock_client, "memory")
    assert stat["compressed"] == 0, "failed replace must not count compressed"
    assert stat["errors"] >= 1
    assert entry in tmp_store.entries("memory"), "original entry stays hot"
