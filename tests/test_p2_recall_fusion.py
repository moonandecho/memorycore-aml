#!/usr/bin/env python3
"""tests/test_p2_recall_fusion.py — P2 召回融合 (默认关) 回归。

覆盖:
  * 单一 `_recall_core` 同时服务生产 `memorycore_recall` 与只读评估
    `recall_readonly`；
  * 开关关闭时生产/只读路径的逐字节 golden 基线；
  * 预注册环境开关 / candidate_k / RRF 权重与公式；
  * 融合开启时的候选扩集、截断、探针字段与只读无写回。
"""
import hashlib
import json

import pytest

from memorycore import server
from memorycore.core.decay import _apply_decay


class _RecallClient:
    """固定结果 mock: 记录 top_k / bump, 返回深拷贝切片。"""

    def __init__(self, items):
        self.items = [dict(it) for it in items]
        self.calls = []
        self.bumps = []

    def recall_results(self, query, top_k=5, bump=True):
        self.calls.append((query, top_k))
        self.bumps.append(bump)
        return [dict(it) for it in self.items[:top_k]]


def _boom(name):
    def _raiser(*args, **kwargs):
        raise AssertionError(f"readonly/fusion path must not call {name}")
    return _raiser


_GOLDEN_ITEMS = [
    {"id": "A", "content": "alpha", "dense_score": 0.90, "importance": 0.9,
     "keyword_score": "oops", "fts_score": None},
    {"id": "B", "content": "beta", "dense_score": 0.30, "importance": 0.9},
    {"id": "C", "content": "gamma", "dense_score": 0.50, "importance": 0.9,
     "keyword_score": 0.42},
]
# 由实施前实现 (内部行为不变) 直接捕获并冻结:
#   生产: memorycore_recall("合成查询", top_k=3)
#   只读: recall_readonly("合成查询", top_k=3)
_GOLDEN_PROD_SHA256 = (
    "6a72588b03484b9012fa7f473c507a5bb8f7c1a17908a1a28c91056900f9e351")
_GOLDEN_READONLY_SHA256 = (
    "dd3a50e8857fe18972158348320d01de0729f11221c0461d21413bf2f21dc88f")


def _patch_recall(monkeypatch, tmp_store, items):
    client = _RecallClient(items)
    monkeypatch.setattr(server, "_store", tmp_store)
    monkeypatch.setattr(server, "_client", client)
    monkeypatch.delenv("MEMORYCORE_RECALL_FUSION", raising=False)
    monkeypatch.delenv("MEMORYCORE_RECALL_FUSION_CANDIDATE_K",
                       raising=False)
    return client


# ---------------------------------------------------------------------------
# 单一内核 + 开关关闭字节一致
# ---------------------------------------------------------------------------

def test_recall_kernel_shared_by_production_and_readonly(
        tmp_store, monkeypatch):
    items = [dict(it) for it in _GOLDEN_ITEMS]
    client = _patch_recall(monkeypatch, tmp_store, items)
    core_calls = []
    real_core = server._recall_core

    def _spy(*args, **kwargs):
        core_calls.append((args, kwargs))
        return real_core(*args, **kwargs)

    monkeypatch.setattr(server, "_recall_core", _spy)
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "0")

    prod = json.loads(server.memorycore_recall("合成查询", top_k=3))
    readonly = server.recall_readonly("合成查询", top_k=3)

    prod_ids = [r["id"] for r in prod["results"]]
    readonly_ids = [r["id"] for r in readonly]
    assert prod_ids == readonly_ids == ["A", "C", "B"]
    assert len(core_calls) == 2
    assert all(call[1]["fusion_on"] is False for call in core_calls)
    assert client.calls == [("合成查询", 3), ("合成查询", 3)]
    assert client.bumps == [False, False]


def test_fusion_off_production_bytes_identical(tmp_store, monkeypatch):
    """mock 输入下 memorycore_recall 返回与改动前逐字节一致。"""
    _patch_recall(monkeypatch, tmp_store, _GOLDEN_ITEMS)
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "0")
    out = server.memorycore_recall("合成查询", top_k=3)
    assert hashlib.sha256(out.encode()).hexdigest() == _GOLDEN_PROD_SHA256


def test_fusion_off_readonly_bytes_identical(tmp_store, monkeypatch):
    """只读评估路径开关关闭时同为改动前 golden。"""
    _patch_recall(monkeypatch, tmp_store, _GOLDEN_ITEMS)
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "0")
    out = json.dumps(server.recall_readonly("合成查询", top_k=3),
                     ensure_ascii=False)
    assert hashlib.sha256(out.encode()).hexdigest() == \
        _GOLDEN_READONLY_SHA256


# ---------------------------------------------------------------------------
# 预注册配置
# ---------------------------------------------------------------------------

def test_fusion_switch_default_off(monkeypatch):
    monkeypatch.delenv("MEMORYCORE_RECALL_FUSION", raising=False)
    assert server._recall_fusion_enabled() is False
    for off in ("0", "false", "off", "no", "none", "", " "):
        monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", off)
        assert server._recall_fusion_enabled() is False
    for on in ("1", "true", "on", "yes"):
        monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", on)
        assert server._recall_fusion_enabled() is True


def test_fusion_candidate_k_preregistered_and_capped(monkeypatch):
    monkeypatch.delenv("MEMORYCORE_RECALL_FUSION_CANDIDATE_K", raising=False)
    assert server._recall_fusion_candidate_k() == 30
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION_CANDIDATE_K", "10")
    assert server._recall_fusion_candidate_k() == 10
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION_CANDIDATE_K", "500")
    assert server._recall_fusion_candidate_k() == 50
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION_CANDIDATE_K", "0")
    assert server._recall_fusion_candidate_k() == 1
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION_CANDIDATE_K", "bogus")
    assert server._recall_fusion_candidate_k() == 30


def test_fusion_rrf_hyperparameters_preregistered():
    assert server._RECALL_FUSION_RRF_K == 5
    assert server._RECALL_FUSION_W_ENGINE == 1.0
    assert server._RECALL_FUSION_W_DECAY == 1.5
    assert server._RECALL_FUSION_W_LEX == 0.25
    assert server._RECALL_FUSION_CANDIDATE_K_DEFAULT == 30


# ---------------------------------------------------------------------------
# 词法弱特征 / RRF 纯函数
# ---------------------------------------------------------------------------

def test_lex_score_preregistered_formula():
    assert server._recall_fusion_lex_score(
        "atlas", "project atlas notes") == 0.4
    assert server._recall_fusion_lex_score("atlas", "atlases") == 0.0
    assert server._recall_fusion_lex_score(
        "abc_123", "see abc_123 here") == 1.7  # identifier 1.0 + 数字 0.7
    # 日期 1.0 + 数字 0.7 + 连续中文 "年月日" 0.5。
    assert server._recall_fusion_lex_score(
        "2026年9月1日", "记录 2026年9月1日") == 2.2
    assert server._recall_fusion_lex_score(
        "项目进度", "今日项目进度更新") == 1.0
    # 长文本保护: len(q)*len(c) > 200_000 时跳过 LCS 项。
    guard = "x" + ("占位" * 100000) + "项目进度"
    assert len("项目进度") * len(guard) > 200_000
    assert server._recall_fusion_lex_score("项目进度", guard) == 0.0


def test_fuse_candidates_rrf_expected_order(monkeypatch):
    def _fake_decay(rows):
        for row in rows:
            row["final_score"] = row.pop("_decay_score")
        rows.sort(key=lambda r: r.get("final_score", 0), reverse=True)
        return rows

    monkeypatch.setattr(server, "_apply_decay", _fake_decay)
    rows = [
        {"id": "A", "content": "alpha", "dense_score": 0.1,
         "_decay_score": 0.1, "importance": 0.9},
        {"id": "B", "content": "beta", "dense_score": 0.3,
         "_decay_score": 0.5, "importance": 0.9},
        {"id": "C", "content": "gamma", "dense_score": 0.2,
         "_decay_score": 0.9, "importance": 0.9},
    ]
    # engine: A1 B2 C3; decay: C1 B2 A3; lex: A rank1.
    # A = 1/6 + 1.5/8 + 0.25/6; C = 1/8 + 1.5/6; B = 1/7 + 1.5/7.
    ordered = server._fuse_recall_candidates(rows, "alpha", 3)
    assert [r["id"] for r in ordered] == ["A", "C", "B"]
    assert ordered[0]["final_score"] == 0.1
    assert ordered[1]["final_score"] == 0.9
    assert ordered[2]["final_score"] == 0.5


# ---------------------------------------------------------------------------
# 融合开启: 生产与只读路径
# ---------------------------------------------------------------------------

def _fusion_items(n=40, lex_idx=4):
    items = []
    for i in range(n):
        items.append({
            "id": f"id{i}",
            "content": "needle" if i == lex_idx else "filler",
            "dense_score": round(1.0 - i * 0.01, 4),
            "importance": 0.9,
        })
    return items


def test_memorycore_recall_fusion_on_candidate_k_and_probe(
        tmp_store, monkeypatch):
    client = _patch_recall(monkeypatch, tmp_store, _fusion_items())
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "1")
    events = []
    monkeypatch.setattr(server, "log_activity_query", lambda q: None)
    monkeypatch.setattr(server, "record_recall_probe",
                        lambda event: events.append(event))

    data = json.loads(server.memorycore_recall("needle", top_k=4))

    assert client.calls == [("needle", 30)]
    assert client.bumps == [False]
    assert [r["id"] for r in data["results"]] == [
        "id0", "id1", "id2", "id4"]
    assert set(data["results"][0]) == {
        "id", "content", "dense_score", "importance", "final_score",
        "keyword_score", "fts_score", "page_fault", "channel"}
    event = events[0]
    assert event["candidate_count"] == 30
    assert event["top_k"] == 4
    assert event["returned_ids"] == ["id0", "id1", "id2", "id4"]
    assert event["candidate_ids"] == ["id0", "id1", "id2", "id4"]
    assert event["channel"] == ["S", "S", "S", "K"]


def test_readonly_fusion_on_no_hot_writes_and_same_order(
        tmp_store, monkeypatch):
    client = _patch_recall(monkeypatch, tmp_store, _fusion_items())
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "1")
    for meth in ("add", "replace", "remove", "remove_by_exact"):
        monkeypatch.setattr(tmp_store, meth, _boom(f"LocalStore.{meth}"))
    monkeypatch.setattr(server, "restore_stubs_from_results",
                        _boom("restore_stubs_from_results"))
    monkeypatch.setattr(server, "log_activity_query",
                        _boom("log_activity_query"))
    monkeypatch.setattr(server, "record_recall_probe",
                        _boom("record_recall_probe"))

    got = server.recall_readonly("needle", top_k=4)

    assert [r["id"] for r in got] == ["id0", "id1", "id2", "id4"]
    assert client.calls == [("needle", 30)]
    assert client.bumps == [False]
    assert not tmp_store.memory_path.exists(), "融合只读路径不得创建热层"
    assert not tmp_store.user_path.exists(), "融合只读路径不得创建热层"


def test_readonly_fusion_on_override_via_keyword(
        tmp_store, monkeypatch):
    client = _patch_recall(monkeypatch, tmp_store, _fusion_items())
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "0")

    got = server.recall_readonly("needle", top_k=2, fusion_on=True)

    assert [r["id"] for r in got] == ["id0", "id1"]
    assert client.calls == [("needle", 30)]
    assert client.bumps == [False]


def test_probe_candidate_count_preserved_on_annotation_error(
        tmp_store, monkeypatch):
    """标注失败时 error 探针的 candidate_count 仍等于冷层返回条数。"""
    client = _patch_recall(monkeypatch, tmp_store, _GOLDEN_ITEMS)
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "0")
    events = []
    monkeypatch.setattr(server, "log_activity_query", lambda q: None)
    monkeypatch.setattr(server, "record_recall_probe",
                        lambda event: events.append(event))
    monkeypatch.setattr(server, "restore_stubs_from_results",
                        lambda store, mss, results: results)

    def _boom_channel(*args, **kwargs):
        raise RuntimeError("channel boom")

    monkeypatch.setattr(server, "_recall_channel_of", _boom_channel)

    out = json.loads(server.memorycore_recall("合成查询", top_k=3))

    assert "error" in out and "channel boom" in out["error"]
    assert client.calls == [("合成查询", 3)]
    assert len(events) == 1
    assert events[0]["candidate_count"] == 3
    assert events[0]["returned_ids"] == []


def test_handle_mode_does_not_fuse(tmp_store, monkeypatch):
    """handle 直查保持现状: env 开启也不扩候选/不融合。"""
    items = [
        {"id": "c-old", "content": "主题甲", "dense_score": 0.9,
         "importance": 0.9},
    ]
    client = _patch_recall(monkeypatch, tmp_store, items)
    stub = "主题甲→recall(\"主题甲\")"
    tmp_store.add("memory", stub)
    ms = server._metastore_for("memory")
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="c-old", handle="#h1")
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "1")
    monkeypatch.setattr(server, "log_activity_query", lambda q: None)
    monkeypatch.setattr(server, "restore_stubs_from_results",
                        lambda store, mss, results: results)

    # P2-FIX-F: 旧代码没有融合函数, monkeypatch.setattr 会直接失败;
    # 新代码 handle 分支在融合开启时也必须完全不进入 _fuse_recall_candidates。
    fuse_calls = []

    def _fuse_must_not_be_called(*args, **kwargs):
        fuse_calls.append((args, kwargs))
        raise AssertionError("handle branch must not call fusion")

    monkeypatch.setattr(server, "_fuse_recall_candidates",
                        _fuse_must_not_be_called)
    data = json.loads(server.memorycore_recall(
        "原始查询", top_k=2, handle="#h1"))

    assert fuse_calls == []
    assert client.calls and client.calls[0][1] == 2, \
        "handle 直查不得使用 candidate_k"
    assert data["mode"] == "handle"
    assert data["results"][0]["channel"] == "H"


# ---------------------------------------------------------------------------
# P2-FIX-B/C/E: 行身份 RRF / 非字符串 content / env 白名单
# ---------------------------------------------------------------------------

def _fuse_rows(rows, query="q", top_k=3):
    return server._fuse_recall_candidates(
        [dict(r) for r in rows], query, top_k)


def test_fuse_candidates_duplicate_ids_keep_each_row_identity():
    """重复 id 不折叠: 两条各自保留 engine/decay 名次与原顺序。"""
    got = _fuse_rows([
        {"id": "dup", "content": "first", "dense_score": 0.9,
         "importance": 0.9},
        {"id": "dup", "content": "second", "dense_score": 0.8,
         "importance": 0.9},
        {"id": "x", "content": "third", "dense_score": 0.7,
         "importance": 0.9},
    ])
    assert [(r.get("id"), r.get("content")) for r in got] == [
        ("dup", "first"), ("dup", "second"), ("x", "third")]


def test_fuse_candidates_missing_ids_are_independent_rows():
    """缺 id 行按位置参与, 不通过 None key 互相覆盖。"""
    got = _fuse_rows([
        {"content": "a", "dense_score": 0.9, "importance": 0.9},
        {"content": "b", "dense_score": 0.8, "importance": 0.9},
        {"id": "x", "content": "c", "dense_score": 0.7,
         "importance": 0.9},
    ])
    assert [(r.get("id"), r.get("content")) for r in got] == [
        (None, "a"), (None, "b"), ("x", "c")]


def test_fuse_candidates_mixed_ids_and_missing_rows_keep_full_count():
    """混合有 id / 无 id: 条数与顺序都保持候选行身份。"""
    got = _fuse_rows([
        {"content": "a1", "dense_score": 0.99, "importance": 0.9},
        {"id": "dup", "content": "d1", "dense_score": 0.98,
         "importance": 0.9},
        {"content": "a2", "dense_score": 0.97, "importance": 0.9},
        {"id": "dup", "content": "d2", "dense_score": 0.96,
         "importance": 0.9},
    ], top_k=4)
    assert [r.get("content") for r in got] == ["a1", "d1", "a2", "d2"]


def test_fuse_candidates_same_id_different_scores_get_distinct_ranks():
    """两个相同 id 不同分数: 不回退成同一条, 两行各自按 RRF 计分。"""
    got = _fuse_rows([
        {"id": "same", "content": "high", "dense_score": 0.95,
         "importance": 0.9},
        {"id": "same", "content": "low", "dense_score": 0.55,
         "importance": 0.9},
        {"id": "other", "content": "mid", "dense_score": 0.75,
         "importance": 0.9},
    ])
    assert [r.get("content") for r in got] == ["high", "mid", "low"]


def test_lex_score_non_string_content_is_zero_not_exception():
    for content in (123, b"needle", ["needle"], None, {"x": 1}):
        assert server._recall_fusion_lex_score("needle", content) == 0.0


def test_fusion_on_non_string_content_keeps_recall_non_empty(
        tmp_store, monkeypatch):
    """融合开启时 int/bytes/list/None/缺字段都不得让整条召回报错/空。"""
    items = [
        {"id": "int", "content": 123, "dense_score": 0.99,
         "importance": 0.9},
        {"id": "bytes", "content": b"needle", "dense_score": 0.98,
         "importance": 0.9},
        {"id": "list", "content": ["needle"], "dense_score": 0.97,
         "importance": 0.9},
        {"id": "none", "content": None, "dense_score": 0.96,
         "importance": 0.9},
        {"id": "missing", "dense_score": 0.95, "importance": 0.9},
    ]
    client = _patch_recall(monkeypatch, tmp_store, items)
    monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", "1")

    got = server.recall_readonly("needle", top_k=5)

    assert [r["id"] for r in got] == [
        "int", "bytes", "list", "none", "missing"]
    assert client.calls == [("needle", 30)]
    assert client.bumps == [False]


@pytest.mark.parametrize("value,expected", [
    (None, False),
    ("", False),
    (" ", False),
    ("\t\n", False),
    ("0", False),
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("TrUe", True),
    (" true ", True),
    ("yes", True),
    ("YES", True),
    ("on", True),
    ("ON", True),
    ("off", False),
    ("no", False),
    ("none", False),
    ("flase", False),
    ("2", False),
    ("random-string", False),
])
def test_fusion_switch_explicit_whitelist(value, expected, monkeypatch):
    if value is None:
        monkeypatch.delenv("MEMORYCORE_RECALL_FUSION", raising=False)
    else:
        monkeypatch.setenv("MEMORYCORE_RECALL_FUSION", value)
    assert server._recall_fusion_enabled() is expected
