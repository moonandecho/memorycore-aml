#!/usr/bin/env python3
"""tests/test_normalize_dedup.py — 缺口1: local_store.add 去重归一化。

验证点:
  1. 全角/半角逗号变体 → 判重 (already exists), 不写第二条
  2. 空白折叠变体 → 判重
  3. 完全不同内容 → 写入成功
  4. 写入内容保持原始 (归一化仅用于比较, 不改变存储)
  5. MetaStore 键与写入内容一致 (无孤儿/双写): 被拒的变体不产生元数据键
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.local_store import LocalStore, normalize_for_compare  # noqa: E402


def test_fullwidth_halfwidth_comma_duplicate(tmp_store):
    """全角，与半角, 变体 → 判重; 只写第一条原始内容。"""
    full = "宣传写作规则: 结论先行，事实引用必须准确，措辞严谨"
    half = "宣传写作规则: 结论先行, 事实引用必须准确, 措辞严谨"
    r1 = tmp_store.add("memory", full)
    assert r1["success"] is True
    r2 = tmp_store.add("memory", half)
    assert r2["success"] is False
    assert r2["error"] == "Entry already exists (no duplicate added)."
    ents = tmp_store.entries("memory")
    assert ents == [full], "只保留第一条 (原始全角内容, 未被归一化改写)"


def test_whitespace_variant_duplicate(tmp_store):
    """连续空白折叠: "A  B" 与 "A\tB" 判重。"""
    assert tmp_store.add("memory", "准则 A  B  C").get("success") is True
    r = tmp_store.add("memory", "准则 A\tB C")
    assert r["success"] is False
    assert r["error"] == "Entry already exists (no duplicate added)."


def test_different_content_still_adds(tmp_store):
    """完全不同内容 → 成功写入。"""
    assert tmp_store.add("memory", "内容A，B").get("success") is True
    r = tmp_store.add("memory", "完全不同的准则X")
    assert r["success"] is True
    assert len(tmp_store.entries("memory")) == 2


def test_original_content_preserved(tmp_store):
    """存储内容保持原始字符 (全角标点/原空白), 不被归一化改写。"""
    raw = "规则：结论先行， 事实必须准确。"
    tmp_store.add("memory", raw)
    assert tmp_store.entries("memory") == [raw]


def test_meta_key_follows_written_content(tmp_store, meta_for):
    """归一化判重后元数据无孤儿: 被拒变体不产生 sidecar 键。

    add() 只写原始内容; store_fact 仅在 add 成功后才 stamp → 被拒变体
    不会以新键写入 sidecar。已写条目键 = sha256(原始内容), 查询一致。
    """
    ms = meta_for("memory")
    full = "准则甲，结论先行。"
    tmp_store.add("memory", full)
    ms.stamp(full, "rule", origin="store_fact")
    # 变体被拒 → 不 stamp → 无孤儿键
    r = tmp_store.add("memory", "准则甲, 结论先行。")
    assert r["success"] is False
    assert ms.get_entry("准则甲, 结论先行。") is None, "被拒变体不应有元数据键"
    assert ms.get_entry(full) is not None, "原始内容元数据完好"
    # reconcile 对被拒变体无影响 (无键无条目)
    st = ms.reconcile(tmp_store.entries("memory"))
    assert st["gc"] == 0


def test_normalize_function_reusable(tmp_store):
    """归一化函数可复用 (任务 3 stub 查找/去重共用同一实现)。"""
    assert normalize_for_compare("A，B") == normalize_for_compare("A, B")
    assert normalize_for_compare("【规则】\u3000x  y") == "[规则] x y"
