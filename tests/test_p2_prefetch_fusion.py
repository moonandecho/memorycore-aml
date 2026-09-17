#!/usr/bin/env python3
"""tests/test_p2_prefetch_fusion.py — prefetch 插件接入融合内核（2026-09-18）。

覆盖：
  * 默认（env 未设）→ 候选池 = 预注册 candidate_k（默认 30），排序走
    `core.recall_fusion` 内核（与治理层同一份实现）；
  * `MEMORYCORE_RECALL_FUSION=0` → 候选池回到 20，排序走旧 `_apply_decay`；
  * 滚动基线的样本口径**不变**：只喂引擎序前 `_RECALL_CANDIDATES` 条。

为什么单独一个文件：插件的排序段过去自己拼（引擎序 + 纯 decay），治理层的
排序改进到不了「每轮注入」这条真实流量路径 —— 本文件把「两条路径共用内核」
和「池大小/基线口径」钉住，防止再次漂移。
"""
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import conftest  # noqa: E402

PLUGIN_PATH = Path(conftest.PLUGIN_PATH)

_ITEMS = [
    {"id": f"c{i}",
     "content": "阿尔法贝塔记录" if i == 7 else "无关内容",
     "dense_score": round(0.9 - i * 0.02, 3),
     "importance": 0.9}
    for i in range(30)
]


def _load_plugin():
    spec = importlib.util.spec_from_file_location("p2_prefetch_fusion_plugin",
                                                  PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _provider(mod, items, monkeypatch, *, fusion_calls, decay_calls, baselines):
    provider = mod.MemoryCorePrefetchProvider()
    provider._load_directory = lambda: []
    provider._injected_ids = set()
    provider._hot_norm = ""
    provider._record_baseline = lambda results: baselines.append(len(results))
    provider._mark_injected_audit = lambda results: None

    class _FakeClient:
        def __init__(self, *a, **k):
            self.queries = []

        def recall_results(self, query, top_k=5, bump=True):
            self.queries.append((query, top_k, bump))
            return [dict(it) for it in items][:top_k]

    fake = _FakeClient()
    mod.ColdStoreClient = lambda *a, **k: fake
    mod.LocalStore = lambda *a, **k: object()
    mod.MetaStore = lambda *a, **k: object()
    mod.log_activity_query = lambda q: None
    mod.restore_stubs_from_results = (
        lambda store, metas, results: list(results))

    real_fuse = mod._recall_fusion.fuse_candidates
    real_decay = mod._apply_decay

    def _spy_fuse(results, query, top_k):
        fusion_calls.append((len(results), top_k))
        return real_fuse(results, query, top_k)

    def _spy_decay(results):
        decay_calls.append(len(results))
        return real_decay(results)

    monkeypatch.setattr(mod._recall_fusion, "fuse_candidates", _spy_fuse)
    monkeypatch.setattr(mod, "_apply_decay", _spy_decay)
    return provider, fake


def test_plugin_default_uses_fusion_kernel(monkeypatch):
    monkeypatch.delenv("MEMORYCORE_RECALL_FUSION", raising=False)
    fusion_calls, decay_calls, baselines = [], [], []
    mod = _load_plugin()
    provider, fake = _provider(mod, _ITEMS, monkeypatch,
                               fusion_calls=fusion_calls,
                               decay_calls=decay_calls, baselines=baselines)

    provider.prefetch("阿尔法贝塔")

    assert fake.queries, "插件未发起冷层召回"
    assert fake.queries[0][1] == 30, "默认应取 candidate_k=30 条候选"
    assert fake.queries[0][2] is False, "召回必须只读 (bump=False)"
    assert fusion_calls == [(30, 30)], "池应整池交给融合内核排序"
    assert decay_calls == [], "融合开时不应再走纯 decay 排序"
    assert baselines and baselines[0] == 20, "基线样本口径须固定为前 20 条"


def test_plugin_fusion_off_keeps_legacy_path(monkeypatch):
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "0")
    fusion_calls, decay_calls, baselines = [], [], []
    mod = _load_plugin()
    provider, fake = _provider(mod, _ITEMS, monkeypatch,
                               fusion_calls=fusion_calls,
                               decay_calls=decay_calls, baselines=baselines)

    provider.prefetch("阿尔法贝塔")

    assert fake.queries and fake.queries[0][1] == 20, "关闭时应回到旧口径 20"
    assert fake.queries[0][2] is False
    assert fusion_calls == [], "关闭时不得调用融合内核"
    assert decay_calls == [20], "关闭时排序走旧 _apply_decay"
    assert baselines and baselines[0] == 20
