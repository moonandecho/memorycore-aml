#!/usr/bin/env python3
"""tests/test_prefetch_norm_dedup.py — prefetch 插件热层去重归一化烟测。

缺口1/任务3 共用归一化: 冷层召回内容若是热层条目的标点变体 → 不重复注入。
"""
import importlib.util
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_plugin():
    plugin_path = (Path(__file__).resolve().parent.parent
                   / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")
    spec = importlib.util.spec_from_file_location("memorycore_prefetch_norm",
                                                  plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_hot_layer_dedupe_normalized():
    """热层已有全角逗号条目 → 冷层半角逗号同内容 → 不注入。"""
    mod = _load_plugin()
    provider = mod.MemoryCorePrefetchProvider()
    provider._hot_text = "准则甲，结论先行，事实准确。"
    provider._hot_norm = mod.normalize_for_compare(provider._hot_text)
    results = [
        {"id": "c1", "content": "准则甲, 结论先行, 事实准确。", "dense_score": 0.9},
        {"id": "c2", "content": "完全不同的冷层事实。", "dense_score": 0.8},
    ]
    kept = provider._dedupe_hot_layer(results)
    assert [r["id"] for r in kept] == ["c2"], (
        "标点变体应与热层判重, 不再注入; 无关内容照常注入")


def test_hot_layer_dedupe_exact_still_works():
    """精确重复 (同字符) 仍判重 (原语义不破)。"""
    mod = _load_plugin()
    provider = mod.MemoryCorePrefetchProvider()
    provider._hot_text = "准则乙: 内容。"
    provider._hot_norm = mod.normalize_for_compare(provider._hot_text)
    results = [{"id": "c1", "content": "准则乙: 内容。", "dense_score": 0.9}]
    assert provider._dedupe_hot_layer(results) == []


def test_hot_layer_dedupe_empty_hot():
    """热层空 → 不过滤 (原语义不破)。"""
    mod = _load_plugin()
    provider = mod.MemoryCorePrefetchProvider()
    provider._hot_norm = ""
    results = [{"id": "c1", "content": "任意内容。", "dense_score": 0.9}]
    assert provider._dedupe_hot_layer(results) == results
