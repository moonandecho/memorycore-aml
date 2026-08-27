#!/usr/bin/env python3
"""tests/test_review_fixes.py — 终审修复回归 (F1/F3/F4)。

终审 FINAL-REVIEW.md 的 3 项必修修复的锁定测试:
  F1: sidecar 故障不阻塞溢流 (reconcile/stamp 异常降级 legacy, errors+1)
  F3: replace 失败不计数不盖章 (统计真实性)
  F4: 完成态词否定/待定前缀排除 (未定稿/未拍板/待拍板/定稿:规范 → rule)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest  # noqa: E402

from memorycore.core import metadata as meta_mod  # noqa: E402
from memorycore.core.classifier import classify_entry_type  # noqa: E402
from memorycore.core.overflow import run_overflow  # noqa: E402
from conftest import days_ago_str  # noqa: E402


# ---- F4: 否定/待定前缀排除 (探针回归, 终审实证 4 例 + 收紧后正例) ----

@pytest.mark.parametrize("content", [
    "2026-08-16 公众号头像方案未定稿, 两版都保留",
    "2026-08-16 迁移方案未拍板, 等用户评估后再定",
    "2026-08-16 需求清单待拍板, 用户还没确认",
    "2026-08-16 定稿: 宣传文档写作规范已确立, 必须遵守",
    "2026-08-16 需求未定案, 继续调研统计口径",
    "2026-08-16 方案定稿前两版都保留",
])
def test_f4_pending_or_rule_like_is_rule(content):
    """进行中/准则型内容不得误判 state (F4 锁定)。"""
    assert classify_entry_type(content) == "rule", content


def test_f4_genuine_completion_still_state():
    """真完成态 (已定稿/拍板) 仍判 state (收紧不误伤正例)。"""
    assert classify_entry_type(
        f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案") == "state"
    assert classify_entry_type(
        f"{days_ago_str(2)} 方案已定稿, 不再更换") == "state"


# ---- F1: sidecar 故障不阻塞溢流 ----

def test_f1_reconcile_failure_degrades_to_legacy(tmp_store, mock_client, monkeypatch):
    """reconcile 抛 OSError → 溢流不中断, errors+1, 降级 legacy 关键词路径。"""
    def _boom(self, entries, now=None):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "reconcile", _boom)

    entry = f"{days_ago_str(8)} 拍板: GPU 压测方案定稿, 不再更换方案"
    tmp_store.add("memory", entry)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert stat["errors"] >= 1, "sidecar 故障应记 errors"
    # 降级 legacy: 8 天 state 仍被关键词路径 (1.5 检测) 下沉, 溢流未中断
    assert entry not in tmp_store.entries("memory")
    assert entry in mock_client.stored


def test_f1_stamp_failure_does_not_crash_overflow(tmp_store, mock_client,
                                                  monkeypatch, meta_for):
    """压缩分支 stamp 抛 OSError → 溢流不中断, 压缩仍完成 (下次 reconcile 补盖)。"""
    from datetime import datetime, timedelta, timezone
    from memorycore.core import overflow as ov
    filler = "".join(f"这是第{i}条细节, 展开说明背景与过程, 属于可压缩的长尾内容。" for i in range(1, 8))
    entry = f"用户偏好({days_ago_str(40)}): 极简选型。" + filler
    tmp_store.add("memory", entry)
    # 先用真 stamp 回填历史 updated_at (monkeypatch 之前)
    meta_for("memory").stamp(entry, "rule",
                             updated_at=datetime.now(timezone.utc) - timedelta(days=40))

    def _boom_stamp(self, content, entry_type, written_at=None,
                    updated_at=None, origin="hermes"):
        raise OSError("disk full (mock)")
    monkeypatch.setattr(meta_mod.MetaStore, "stamp", _boom_stamp)
    compressed = "用户偏好: 极简选型 (压缩精简版)"
    monkeypatch.setattr(ov, "_llm_compress", lambda client, e: compressed)

    stat = run_overflow(tmp_store, mock_client, "memory")
    assert compressed in tmp_store.entries("memory"), "压缩应完成 (replace 已成功)"
    assert stat["compressed"] == 1
    assert stat["errors"] == 0, "stamp 失败只降级不计数 (下次 reconcile 补盖)"
    # 注意: 压缩路径 stamp 已包 try/except pass (F1 修复), 不阻塞不计错


# ---- F3: replace 返回值校验 ----

def test_f3_replace_failure_not_counted(tmp_store, mock_client, monkeypatch, meta_for):
    """压缩时 replace 失败 (并发编辑) → 不计数 compressed, errors+1, 原条目保留。"""
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
    assert stat["compressed"] == 0, "replace 失败不得计数 compressed"
    assert stat["errors"] >= 1
    assert entry in tmp_store.entries("memory"), "原条目仍在热层"
