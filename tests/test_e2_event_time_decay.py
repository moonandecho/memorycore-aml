#!/usr/bin/env python3
"""Phase B 回归（改动前必红）：AML_DECAY_EVENT_TIME 事件时间衰减。

覆盖：
  1) 开关默认 off；
  2) 事件时间前缀解析，失败时回退写入时间（last_recalled → timestamp）；
  3) 公式与 core.decay._apply_decay 等价（importance 保护线、90 天半衰、
     向下取整天数、稳定 tie-break），只换基准时间；
  4) opt-in=1 时 /search 顺序确实按事件时间改变；opt-in=0 时与
     _apply_decay 的 id 顺序/content 保持一致。
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from starlette.testclient import TestClient

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import memorycore.aml_server as aml  # noqa: E402
from memorycore.core.decay import _apply_decay  # noqa: E402


class FakeCold:
    def __init__(self):
        self.items = []
        self.calls = []

    def stats(self, all_sessions=False):
        return {"total": len(self.items)}

    def recall_results(self, query, top_k=5, author_id=None, **kwargs):
        self.calls.append({"query": query, "top_k": top_k,
                           "author_id": author_id})
        return [dict(x) for x in self.items]


@pytest.fixture
def http_client(monkeypatch, tmp_path):
    fake = FakeCold()
    monkeypatch.setattr(aml, "_get_client", lambda: fake)
    monkeypatch.setenv("MNEMOSYNE_DATA_DIR", str(tmp_path))
    app = aml.mcp.streamable_http_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c, fake


def _cand(cid, dense, content, ts="2026-09-19T00:00:00Z"):
    return {
        "id": cid,
        "content": content,
        "dense_score": dense,
        "importance": 0.6,
        "timestamp": ts,
        "last_recalled": None,
    }


def _ids(data):
    return [row["id"] for row in data]


# ---------------------------------------------------------------------------
# 默认 off / 解析 / 公式
# ---------------------------------------------------------------------------

def test_decay_event_time_switch_default_off(monkeypatch):
    monkeypatch.delenv("AML_DECAY_EVENT_TIME", raising=False)
    assert getattr(aml, "_DECAY_EVENT_TIME", None) is False


def test_event_time_prefix_parser():
    parser = aml._event_time_from_content
    dt = parser("[2023-05-07 13:56] Caroline went to the support group")
    assert dt == datetime(2023, 5, 7, 13, 56, tzinfo=timezone.utc)
    assert parser("no prefix 2023-05-07") is None
    assert parser("[not-a-date] x") is None
    assert parser(None) is None


def test_apply_decay_event_time_formula_equivalent_to_core():
    fn = aml._apply_decay_event_time
    now = datetime(2023, 8, 5, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "old", "content": "[2023-05-07 12:00] old event",
         "dense_score": 0.9, "importance": 0.6,
         "timestamp": "2026-09-19T00:00:00Z", "last_recalled": None},
        {"id": "protected", "content": "[2022-01-01 00:00] pinned",
         "dense_score": 0.5, "importance": 0.8,
         "timestamp": "2026-09-19T00:00:00Z", "last_recalled": None},
        {"id": "fallback", "content": "no event prefix",
         "dense_score": 1.0, "importance": 0.6,
         "timestamp": "2023-08-04T12:00:00Z", "last_recalled": None},
    ]
    out = {r["id"]: r for r in fn([dict(r) for r in rows], now=now)}
    # 90 天整 -> 0.5 因子（与 core.decay 的 delta.days / 90 完全同口径）
    assert out["old"]["final_score"] == pytest.approx(0.9 * 0.5 ** (90 / 90))
    # importance >= 0.8 保护线：final = dense，即使事件很旧
    assert out["protected"]["final_score"] == pytest.approx(0.5)
    # 解析失败回退 timestamp（写入时间）：1 天 -> 0.5^(1/90)
    assert out["fallback"]["final_score"] == pytest.approx(1.0 * 0.5 ** (1 / 90))


def test_apply_decay_event_time_tie_break_is_stable():
    fn = aml._apply_decay_event_time
    now = datetime(2023, 8, 5, 12, 0, tzinfo=timezone.utc)
    rows = [
        {"id": "first", "content": "[2023-05-07 12:00] same",
         "dense_score": 1.0, "importance": 0.6},
        {"id": "second", "content": "[2023-05-07 12:00] same",
         "dense_score": 1.0, "importance": 0.6},
    ]
    assert [r["id"] for r in fn(rows, now=now)] == ["first", "second"]


# ---------------------------------------------------------------------------
# /search 端到端（off 逐字节现状；on 按事件时间重排）
# ---------------------------------------------------------------------------

def test_search_opt_in_event_time_changes_order_but_off_matches_apply_decay(
        http_client, monkeypatch):
    c, fake = http_client
    fake.items = [
        _cand("a", 1.0, "[2022-01-01 00:00] old event A"),
        _cand("b", 0.6, "[2023-06-01 00:00] newer event B"),
    ]

    # off = 现状：与 core.decay._apply_decay 的 id/content 顺序逐字节一致。
    monkeypatch.setattr(aml, "_DECAY_EVENT_TIME", False)
    r_off = c.post("/search", json={"query": "old newer event",
                                    "user_id": "u-e2:off", "top_k": 2})
    assert r_off.status_code == 200, r_off.text
    expected_off = [dict(x) for x in fake.items]
    expected_off = _apply_decay(expected_off)
    off_data = r_off.json()["data"]
    assert _ids(off_data) == [x["id"] for x in expected_off]
    assert [x["content"] for x in off_data] == [
        (x.get("content") or "").strip() for x in expected_off]

    # on = 处理：两个事件相隔 >1 年，事件时间基准应把 2023 的 b 排在 2022 的 a 前。
    monkeypatch.setattr(aml, "_DECAY_EVENT_TIME", True)
    r_on = c.post("/search", json={"query": "old newer event",
                                   "user_id": "u-e2:on", "top_k": 2})
    assert r_on.status_code == 200, r_on.text
    assert _ids(r_on.json()["data"]) == ["b", "a"], r_on.text


def test_search_opt_in_falls_back_to_write_time_for_bad_prefix(
        http_client, monkeypatch):
    c, fake = http_client
    fake.items = [
        _cand("old-write", 1.0, "no prefix old write",
              ts="2023-01-01T00:00:00Z"),
        _cand("new-write", 0.6, "no prefix new write",
              ts="2026-09-18T00:00:00Z"),
    ]
    monkeypatch.setattr(aml, "_DECAY_EVENT_TIME", True)
    r = c.post("/search", json={"query": "old new write",
                                "user_id": "u-e2:fallback", "top_k": 2})
    assert r.status_code == 200, r.text
    # 无事件前缀 -> 回退 timestamp，2026 写入比 2023 写入新 -> 排前。
    assert _ids(r.json()["data"]) == ["new-write", "old-write"], r.text


def test_search_event_time_tie_break_uses_original_candidate_order(
        http_client, monkeypatch):
    """生产路径也必须在事件时间同分时保持原始候选顺序。

    构造：A 写时间更旧（baseline 衰减后 B 在前），但事件时间按 90 天差
    使两者 final_score 精确同分（0.25）。若事件衰减在 baseline 排序之后
    才执行，tie-break 会错误地继承 baseline 的 [B, A]；正确实现应从原始
    候选池直接做事件时间稳定排序，得到 [A, B]。
    """
    from datetime import timedelta
    now = datetime.now(timezone.utc).replace(microsecond=0)

    def ev_prefix(days):
        return (now - timedelta(days=days)).strftime('[%Y-%m-%d %H:%M] ') + 'tie'

    row_a = _cand('a', 1.0, ev_prefix(180),
                  ts=(now - timedelta(days=360)).isoformat())
    row_b = _cand('b', 0.5, ev_prefix(90),
                  ts=now.isoformat())
    row_a['importance'] = 0.6
    row_b['importance'] = 0.6
    c, fake = http_client
    fake.items = [row_a, row_b]

    # baseline(off)：写时间衰减让 b 排前
    monkeypatch.setattr(aml, '_DECAY_EVENT_TIME', False)
    r_off = c.post('/search', json={'query': 'tie break',
                                    'user_id': 'u-e2:tie-off', 'top_k': 2})
    assert r_off.status_code == 200, r_off.text
    assert _ids(r_off.json()['data']) == ['b', 'a'], r_off.text
    # event-time(on)：精确同分 -> 稳定排序应保留原始 [a, b]
    monkeypatch.setattr(aml, '_DECAY_EVENT_TIME', True)
    r_on = c.post('/search', json={'query': 'tie break',
                                   'user_id': 'u-e2:tie-on', 'top_k': 2})
    assert r_on.status_code == 200, r_on.text
    assert _ids(r_on.json()['data']) == ['a', 'b'], r_on.text
