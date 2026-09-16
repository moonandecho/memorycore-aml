#!/usr/bin/env python3
"""tests/test_fixp2_nonfinite.py — FIX-P2 (REVIEW-6 L2) 有限化口径回归。

覆盖:
  * kw=+inf / "inf" / "1e400" → 该候选不被判 K、不写回, 事件
    channel 与 k_source 自洽 (仍可经 S 通道正常注入);
  * kw=10**400 / dense_score=10**400 → 单条异常分数只影响该条,
    prefetch 不得整条返回空 (正常目录/注入内容仍可用);
  * 6 组正常有限 mock → prefetch 返回值 sha256 与整改前一致。

全部内存 mock; 不联网、不碰生产冷层、不把真实数据写进测试。
"""
import hashlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from memorycore.core import recall_probe  # noqa: E402


def _load_plugin(name: str):
    try:
        from conftest import PLUGIN_PATH
        path = PLUGIN_PATH
    except Exception:
        path = ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeClient:
    """固定冷层候选 mock (只读, 不联网)。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]
        self.queries = []

    def recall_results(self, query, top_k=20, bump=True):
        self.queries.append((query, top_k, bool(bump)))
        return [dict(it) for it in self.items]


def _make_provider(mod, items, records, tmp_path, restore_mode="keep"):
    """隔离 provider; 返回 (provider, fake_client, write_calls)。"""
    provider = mod.MemoryCorePrefetchProvider()
    provider._load_directory = lambda: [dict(r) for r in records]
    provider._injected_ids = set()
    provider._hot_norm = ""
    provider._record_baseline = lambda results: None
    provider._mark_injected_audit = lambda results: None
    fake = _FakeClient(items)
    write_calls = []

    class _FakeStore:
        memory_path = tmp_path / "MEMORY.md"
        user_path = tmp_path / "USER.md"

    def _restore(store, metas, results):
        write_calls.append(
            [r.get("id") for r in results if isinstance(r, dict)])
        if restore_mode == "remove_all_wb":
            return []
        return [dict(r) for r in results]

    mod.ColdStoreClient = lambda *a, **k: fake
    mod.LocalStore = lambda *a, **k: _FakeStore()
    mod.MetaStore = lambda *a, **k: _FakeStore()
    mod.log_activity_query = lambda q: None
    mod.restore_stubs_from_results = _restore
    return provider, fake, write_calls


def _probe_env(monkeypatch, tmp_path):
    target = tmp_path / "probe" / "recall_probe.jsonl"
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE", "1")
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE", str(target))
    recall_probe.reset_probe_metrics()
    return target


def _probe_off(monkeypatch, tmp_path):
    monkeypatch.delenv("MEMORYCORE_RECALL_PROBE", raising=False)
    monkeypatch.setenv("MEMORYCORE_RECALL_PROBE_FILE",
                       str(tmp_path / "probe_off.jsonl"))


def _read_events(target):
    lines = Path(target).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _assert_event_consistent(event):
    """F1/F3: K 行必须有 k_source, H 行必须为空串。"""
    assert len(event["channel"]) == len(event["k_source"])
    for channel, k_source in zip(event["channel"], event["k_source"]):
        if channel == "K":
            assert k_source, "channel=K 的行 k_source 不得为空"
        if channel == "H":
            assert k_source == "", "channel=H 的行 k_source 必须为空"


# ---------------------- ① 非有限 keyword/fts 证据 ---------------------------

@pytest.mark.parametrize("bad_value", [float("inf"), "inf", "1e400"])
def test_l2_nonfinite_keyword_not_k_no_writeback_event_consistent(
        bad_value, tmp_path, monkeypatch):
    """/+inf/`inf`/`1e400`: 不作为 K 证据, 不写回, channel/k_source 自洽。"""
    target = _probe_env(monkeypatch, tmp_path)
    mod = _load_plugin(f"fixp2_nonfinite_kw_{bad_value!r}")
    items = [
        {"id": "c-bad", "content": "xray yankee zulu",
         "dense_score": 0.90, "importance": 0.9,
         "keyword_score": bad_value},
        {"id": "c-ok", "content": "normal payload alpha",
         "dense_score": 0.80, "importance": 0.9,
         "keyword_score": 0.5},
    ]
    records = [{"handle": "#zz", "topic": "other",
                "cold_id": "cold-never"}]
    provider, _fake, write_calls = _make_provider(
        mod, items, records, tmp_path)

    assert provider._keyword_consensus(
        "发布前复核部署", {"keyword_score": bad_value, "content": ""}) is False

    out = provider.prefetch("发布前复核部署")
    assert out, "prefetch 不得整条为空"
    assert "xray yankee zulu" in out, "坏 K 分候选应仍可经 S 通道注入"
    assert "normal payload alpha" in out

    events = _read_events(target)
    assert len(events) == 1
    event = events[0]
    _assert_event_consistent(event)
    bad_idx = event["returned_ids"].index("c-bad")
    assert event["channel"][bad_idx] == "S"
    assert event["k_source"][bad_idx] == ""
    assert event["candidates"][bad_idx]["channel"] == "S"
    assert event["injected"][bad_idx] is True
    assert not any("c-bad" in call for call in write_calls), \
        "无 K 共识的坏分行不得触发写回"

    ok_idx = event["returned_ids"].index("c-ok")
    assert event["channel"][ok_idx] == "K"
    assert event["k_source"][ok_idx] == "cold_kw"
    assert event["injected"][ok_idx] is True


@pytest.mark.parametrize("bad_value", [float("nan"), "-inf"])
def test_l2_nonfinite_keyword_nan_neg_inf_not_k(
        bad_value, tmp_path, monkeypatch):
    """NaN / -inf 与 P0 探针口径一致: 视为 0, 不作为 K 证据。"""
    _probe_off(monkeypatch, tmp_path)
    mod = _load_plugin(f"fixp2_nan_neginf_{bad_value!r}")
    provider = mod.MemoryCorePrefetchProvider()
    assert provider._keyword_consensus(
        "q", {"keyword_score": bad_value, "content": ""}) is False
    assert provider._keyword_consensus(
        "q", {"fts_score": bad_value, "content": ""}) is False


# ---------------------- ② 超大整数分数不拖垮 prefetch ------------------------

def test_l2_huge_int_keyword_score_prefetch_not_empty(
        tmp_path, monkeypatch):
    """kw=10**400: 不可转 float → 0; 单条降级而非 prefetch 整条为空。"""
    target = _probe_env(monkeypatch, tmp_path)
    mod = _load_plugin("fixp2_huge_int_kw")
    huge = 10 ** 400
    items = [
        {"id": "c-huge", "content": "huge payload",
         "dense_score": 0.90, "importance": 0.9,
         "keyword_score": huge},
        {"id": "c-ok", "content": "normal payload alpha",
         "dense_score": 0.80, "importance": 0.9,
         "keyword_score": 0.5},
    ]
    records = [{"handle": "#zz", "topic": "other",
                "cold_id": "cold-never"}]
    provider, _fake, write_calls = _make_provider(
        mod, items, records, tmp_path)
    assert provider._keyword_consensus(
        "发布前复核部署", {"keyword_score": huge, "content": ""}) is False

    out = provider.prefetch("发布前复核部署")
    assert "## 常驻规则目录" in out, "正常目录不得因单条坏分数丢失"
    assert "normal payload alpha" in out, "其它正常候选仍须注入"
    assert "huge payload" in out, "坏分行按 0 后仍可经 S 通道注入"

    events = _read_events(target)
    assert len(events) == 1, "单条坏分数不得吞掉整条 prefetch 事件"
    event = events[0]
    _assert_event_consistent(event)
    huge_idx = event["returned_ids"].index("c-huge")
    assert event["channel"][huge_idx] == "S"
    assert event["k_source"][huge_idx] == ""
    assert event["injected"][huge_idx] is True
    assert not any("c-huge" in call for call in write_calls)


@pytest.mark.parametrize("importance", [0.5, 0.9])
def test_l2_huge_int_dense_score_prefetch_not_empty(
        importance, tmp_path, monkeypatch):
    """dense_score=10**400: decay/格式化都不得把 prefetch 拖空。"""
    target = _probe_env(monkeypatch, tmp_path)
    mod = _load_plugin(f"fixp2_huge_int_dense_{importance}")
    huge = 10 ** 400
    items = [
        {"id": "c-huge-dense", "content": "huge dense payload",
         "dense_score": huge, "importance": importance,
         "keyword_score": 0.5},
        {"id": "c-s", "content": "normal semantic payload",
         "dense_score": 0.80, "importance": 0.9},
    ]
    provider, _fake, write_calls = _make_provider(
        mod, items, [], tmp_path)

    out = provider.prefetch("发布前复核部署")
    assert "huge dense payload" in out, \
        "坏 dense 行仍有 K 证据, 应正常注入"
    assert "normal semantic payload" in out
    assert "[0.00|K] huge dense payload" in out, \
        "异常 dense 在注入文本中按 0 展示"

    events = _read_events(target)
    assert len(events) == 1
    event = events[0]
    _assert_event_consistent(event)
    idx = event["returned_ids"].index("c-huge-dense")
    assert event["dense_scores"][idx] == 0.0
    assert event["keyword_scores"][idx] == 0.5
    assert event["channel"][idx] == "K"
    assert event["k_source"][idx] == "cold_kw"
    assert event["injected"][idx] is True
    assert any("c-huge-dense" in call for call in write_calls), \
        "K 证据仍有效, 写回应正常触发"


# ---------------------- ③ 有限输入 6 组 mock 字节不变 ------------------------

_FINITE_SCENARIOS = [
    dict(
        name="mixed_hks",
        items=[
            {"id": "cold-h5", "content": "", "dense_score": 0.90,
             "keyword_score": 0.7, "fts_score": 0.7, "importance": 0.9},
            {"id": "c-k", "content": "无关内容", "dense_score": 0.60,
             "keyword_score": 0.8, "importance": 0.9},
            {"id": "c-s", "content": "语义内容", "dense_score": 0.80,
             "importance": 0.9},
        ],
        records=[{"handle": "#h5", "topic": "主题戊",
                  "cold_id": "cold-h5"}],
        query="主题戊", restore_mode="keep",
    ),
    dict(
        name="unique_all",
        items=[
            {"id": "u-k", "content": "无关内容A", "dense_score": 0.80,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "u-l", "content": "阿尔法贝塔记录", "dense_score": 0.30,
             "importance": 0.9},
            {"id": "u-s", "content": "语义内容", "dense_score": 0.90,
             "importance": 0.9},
        ],
        records=[], query="阿尔法贝塔", restore_mode="keep",
    ),
    dict(
        name="dup_ids",
        items=[
            {"id": "dup", "content": "发布前复核部署", "dense_score": 0.80,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "dup", "content": "另一个部署复核", "dense_score": 0.80,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "c-s", "content": "语义内容", "dense_score": 0.70,
             "importance": 0.9},
        ],
        records=[], query="发布前复核部署", restore_mode="keep",
    ),
    dict(
        name="dup_reorder",
        items=[
            {"id": "dup", "content": "低分部署复核", "dense_score": 0.30,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "dup", "content": "高分部署复核", "dense_score": 0.90,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "c-s", "content": "语义内容", "dense_score": 0.70,
             "importance": 0.9},
        ],
        records=[], query="部署复核流程", restore_mode="keep",
    ),
    dict(
        name="removed_k",
        items=[
            {"id": "c-kw", "content": "无关内容A", "dense_score": 0.95,
             "keyword_score": 0.8, "importance": 0.9},
            {"id": "c-s", "content": "语义内容", "dense_score": 0.90,
             "importance": 0.9},
        ],
        records=[], query="查询内容", restore_mode="remove_all_wb",
    ),
    dict(
        name="odd_ids",
        items=[
            {"id": None, "content": "空ID甲", "dense_score": 0.80,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": None, "content": "空ID乙", "dense_score": 0.80,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": 1, "content": "数字一", "dense_score": 0.70,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "1", "content": "字符串一", "dense_score": 0.70,
             "keyword_score": 0.9, "importance": 0.9},
            {"id": "c-s", "content": "语义内容", "dense_score": 0.60,
             "importance": 0.9},
        ],
        records=[], query="发布前复核部署", restore_mode="keep",
    ),
]

# 整改前 (review6) 同样 6 组 mock 的 prefetch return 规范化 JSON sha256。
_FINITE_RETURN_SHA256 = (
    "a892c433ed9e2ea152adfa0d2a3efef7c764aea3e38fdf02a5a9a751984ac4d2")


def test_l2_finite_six_mocks_return_unchanged(tmp_path, monkeypatch):
    """③ 正常有限分数下 prefetch 返回值与整改前逐字节一致。"""
    _probe_off(monkeypatch, tmp_path)
    mod = _load_plugin("fixp2_finite_six")
    returns = {}
    for scenario in _FINITE_SCENARIOS:
        provider, _fake, _writes = _make_provider(
            mod, scenario["items"], scenario["records"], tmp_path,
            restore_mode=scenario["restore_mode"])
        returns[scenario["name"]] = provider.prefetch(scenario["query"])
    blob = json.dumps(returns, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    assert hashlib.sha256(blob).hexdigest() == _FINITE_RETURN_SHA256
