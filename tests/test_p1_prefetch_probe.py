#!/usr/bin/env python3
"""tests/test_p1_prefetch_probe.py — P1-B: prefetch 侧探针接入。

验收对应:
  * 开关关 → 零落盘、零目录创建;
  * 开关开 → 注入集合/顺序与关时逐字节一致 (同一 mock 输入差分);
  * 探针内部抛错 → prefetch 返回值不变;
  * 事件 source=prefetch, injected/selected 与最终注入一致, query 不落明文。
"""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _resolve_plugin_path() -> Path:
    """Release layout: <repo-root>/hermes-plugin/memorycore-prefetch/__init__.py."""
    try:
        from conftest import PLUGIN_PATH  # release layout via conftest
        return PLUGIN_PATH
    except Exception:
        return (ROOT / "hermes-plugin" / "memorycore-prefetch"
                / "__init__.py")


PLUGIN_PATH = _resolve_plugin_path()


def _load_plugin():
    spec = importlib.util.spec_from_file_location("p1_prefetch_plugin", PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClient:
    """固定冷层候选 mock; 不访问网络/生产冷层。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]
        self.queries = []

    def recall_results(self, query, top_k=5, bump=True):
        self.queries.append((query, top_k, bump))
        return [dict(it) for it in self.items]


def _provider(mod, items):
    provider = mod.MemoryCorePrefetchProvider()
    provider._load_directory = lambda: []
    provider._injected_ids = set()
    provider._hot_norm = ""
    provider._record_baseline = lambda results: None
    provider._mark_injected_audit = lambda results: None
    fake = _FakeClient(items)
    mod.ColdStoreClient = lambda *a, **k: fake
    mod.LocalStore = lambda *a, **k: object()
    mod.MetaStore = lambda *a, **k: object()
    mod.log_activity_query = lambda q: None
    # 本组 mock 的 K 候选会进入写回分支; 替换为只读 no-op, 隔离本地存储。
    mod.restore_stubs_from_results = lambda store, metas, results: list(results)
    return provider, fake


_ITEMS = [
    {"id": "c-s", "content": "语义内容", "dense_score": 0.90,
     "importance": 0.9},
    {"id": "c-kw", "content": "无关内容A", "dense_score": 0.30,
     "importance": 0.9, "keyword_score": 0.8},
    {"id": "c-lex", "content": "阿尔法贝塔记录", "dense_score": 0.20,
     "importance": 0.9},
    {"id": "c-low", "content": "无关内容B", "dense_score": 0.10,
     "importance": 0.9},
]


def test_prefetch_probe_disabled_is_zero_write(tmp_path, monkeypatch):
    """B①: 开关关 (含 env unset) → 探针文件与目录都不创建。"""
    target = tmp_path / "probe-nested" / "recall_probe.jsonl"
    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE", raising=False)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    mod = _load_plugin()
    provider, fake = _provider(mod, _ITEMS)
    out = provider.prefetch("阿尔法贝塔")
    assert "MemoryCore Recall" in out
    assert not target.exists()
    assert not target.parent.exists()
    assert fake.queries and fake.queries[0][2] is False


def test_prefetch_probe_enabled_identical_injection_and_event(tmp_path,
                                                              monkeypatch):
    """B②/B④: 开/关输出逐字节一致; 事件 injected/selected 与最终注入一致。"""
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))

    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE", raising=False)
    mod_off = _load_plugin()
    provider_off, _ = _provider(mod_off, _ITEMS)
    out_off = provider_off.prefetch("阿尔法贝塔")

    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    mod_on = _load_plugin()
    provider_on, fake_on = _provider(mod_on, _ITEMS)
    selected_capture = []
    provider_on._mark_injected_audit = (
        lambda results: selected_capture.append([r.get("id") for r in results]))
    out_on = provider_on.prefetch("阿尔法贝塔")

    # 同一组 mock 输入差分: 注入集合、顺序、格式逐字节一致
    assert out_on == out_off
    # 2026-09-18: 融合默认开 → 候选池 = 预注册 candidate_k(默认 30)。
    assert fake_on.queries and fake_on.queries[0][1] == 30
    assert fake_on.queries[0][2] is False
    # 注入顺序: K(dense 0.3) -> K(local lex, dense 0.2) -> S(dense 0.9)
    assert selected_capture == [["c-kw", "c-lex", "c-s"]]
    assert out_on.index("无关内容A") < out_on.index("阿尔法贝塔记录")
    assert out_on.index("阿尔法贝塔记录") < out_on.index("语义内容")
    assert "无关内容B" not in out_on

    lines = target.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])
    assert event["source"] == "prefetch"
    assert event["selected"] == ["c-kw", "c-lex", "c-s"]
    assert event["returned_ids"] == ["c-s", "c-kw", "c-lex", "c-low"]
    assert event["injected"] == [True, True, True, False]
    assert event["candidate_ids"] == event["returned_ids"]
    assert len(event["injected"]) == len(event["returned_ids"])
    assert event["query_sha256"] == __import__("hashlib").sha256(
        "阿尔法贝塔".encode("utf-8")).hexdigest()
    assert event["query_len"] == len("阿尔法贝塔")
    assert event["top_k"] == 5
    assert event["candidate_count"] == 4
    assert event["k_source"] == ["", "cold_kw", "local_lex", ""]
    raw = target.read_text(encoding="utf-8")
    assert "阿尔法贝塔" not in raw  # query 明文不落盘


def test_prefetch_probe_exception_does_not_change_return(tmp_path, monkeypatch):
    """B③: 探针内部 (monkeypatch) 抛错时 prefetch 返回值逐字节不变。"""
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))

    mod = _load_plugin()
    provider, _ = _provider(mod, _ITEMS)
    expected = provider.prefetch("阿尔法贝塔")

    def _boom(entry):
        raise RuntimeError("probe boom (synthetic)")

    mod.record_recall_probe = _boom
    provider2, fake2 = _provider(mod, _ITEMS)
    out = provider2.prefetch("阿尔法贝塔")
    assert out == expected
    assert fake2.queries and fake2.queries[0][2] is False


def test_prefetch_probe_query_failure_does_not_change_return(tmp_path,
                                                            monkeypatch):
    """即使 sha256 helper 被替换为抛错, prefetch 返回值也不变。"""
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    mod = _load_plugin()
    provider, _ = _provider(mod, _ITEMS)
    expected = provider.prefetch("阿尔法贝塔")

    def _boom(q):
        raise RuntimeError("hash boom (synthetic)")

    mod._probe_query_sha256 = _boom
    provider2, _ = _provider(mod, _ITEMS)
    out = provider2.prefetch("阿尔法贝塔")
    assert out == expected
