#!/usr/bin/env python3
"""tests/test_weekly_tidy.py — 周整理 (smart_tidy) 评审修复验收 (2026-09)。

评审报告 §4 测试计划 8 条 + 冷层失败保留用例:
  ① 活性豁免: 近 7 天词法命中 sb≥2 → 不沉; last_active_at 新鲜 (<7d) → 不沉;
     对照: 无活性 → 照常候选下沉
  ② 恢复条目 (written_at=now + 条内嵌老日期) → 不沉 (较新锚点语义)
  ③ 冷层已有 same → 不重复 remember 且本地删除; similar → update 合并; 无匹配 → remember
  ④ 无 LLM key → a) 全部跳过 (锁死保守行为)
  ⑤ protected / should_keep_local=True (用户偏好句) → 不沉
  ⑥ 合并路径: 原文 cold 已存在 → 不重复 remember
  ⑦ dry-run 零落盘 (store 与冷层调用计数均不变)
  ⑧ TIDY_MAX_SINK_PER_RUN=3 上限 + 热层保底 (entries≤3 不掏空)
隔离模式同 conftest (tmp_store/mock_client/meta_for, 绝不碰生产数据)。
注: smart_tidy 热层保底 len(entries)≤3 不掏空, 故所有"应下沉"用例加 3 条
填充条目 (无日期 → 非候选, 不干扰判定)。
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import metadata as meta_mod
from conftest import MockMnemosyneClient, days_ago_str
from memorycore import weekly_maintenance as wm


# ---- 工具 ----------------------------------------------------------------

FILLERS = ["规则甲: 用词简洁。", "规则乙: 代码注释用中文。", "规则丙: 提交信息写清楚。"]


class RecordingClient(MockMnemosyneClient):
    """mock 冷层 + 记录 remember/update 调用参数 (importance 对齐断言)。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.remember_calls = []
        self.update_calls = []

    def remember(self, content, importance=0.6, scope="global"):
        self.remember_calls.append((content, importance, scope))
        return super().remember(content, importance=importance, scope=scope)

    def update(self, memory_id, content, importance=None):
        self.update_calls.append((memory_id, content))
        return super().update(memory_id, content, importance=importance)


def _stat():
    return {"sunk": 0, "merged": 0, "errors": 0, "sink_dry": [],
            "merge_dry": [], "merge_skipped": 0}


def _add_candidate(tmp_store, meta_for, text=None, days_ago=30,
                   last_active_days=30, target="memory", importance=0.8):
    """加一条可沉候选 (state 型: 内嵌日期 + '已删' → should_keep_local=False)。"""
    if text is None:
        text = f"{days_ago_str(days_ago)} 已删: 打印机驱动冲突, 卸载重装。"
    tmp_store.add(target, text)
    now = datetime.now(timezone.utc)
    meta_for(target).stamp(
        text, "state",
        written_at=now - timedelta(days=days_ago),
        updated_at=now - timedelta(days=days_ago),
        last_active_at=now - timedelta(days=last_active_days),
        importance=importance)
    return text


def _add_fillers(tmp_store):
    """3 条非候选填充 (无日期 → 不参与下沉判定), 越过热层保底 len>3。"""
    for f in FILLERS:
        tmp_store.add("memory", f)


def _confirm_true(monkeypatch, calls):
    """LLM 确认 → True, 记录调用 (断言豁免过滤在 LLM 之前生效)。"""
    monkeypatch.setattr(wm, "_llm_confirm_sink",
                        lambda e: (calls.append(e), True)[1])


def _run_tidy(tmp_store, client, target="memory", dry=False):
    stat = _stat()
    wm.smart_tidy(tmp_store, client, target, stat, dry)
    return stat


# ---- ① 活性豁免 ----------------------------------------------------------

def test_lexical_activity_exempt(tmp_store, mock_client, meta_for, tmp_path,
                                 monkeypatch):
    """近 7 天查询 sb≥2 词法命中 → 不沉 (与 LRU 挤权同口径), LLM 确认不被调用。"""
    e = _add_candidate(tmp_store, meta_for)
    _add_fillers(tmp_store)
    meta_mod.log_activity_query("打印机怎么设置双面打印")  # sb=2: 打印/印机
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, mock_client)
    assert e in tmp_store.entries("memory"), "词法活跃条目不应被沉"
    assert stat["sunk"] == 0 and stat["errors"] == 0
    assert calls == [], "词法豁免应在 LLM 确认之前生效"
    assert mock_client.stored == []


def test_fresh_last_active_at_exempt(tmp_store, mock_client, meta_for,
                                     monkeypatch):
    """last_active_at 新鲜 (<7 天) → 不沉。"""
    e = _add_candidate(tmp_store, meta_for, last_active_days=2)
    _add_fillers(tmp_store)
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, mock_client)
    assert e in tmp_store.entries("memory"), "新鲜活性条目不应被沉"
    assert stat["sunk"] == 0
    assert calls == [], "活性豁免应在 LLM 确认之前生效"


def test_no_activity_control_sinks(tmp_store, meta_for, monkeypatch):
    """对照: 无任何活性 → 照常候选 → LLM 放行 → remember(importance=0.6) 后删本地。"""
    e = _add_candidate(tmp_store, meta_for)
    _add_fillers(tmp_store)
    client = RecordingClient()
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, client)
    assert e not in tmp_store.entries("memory"), "无活性对照条目应被沉"
    assert stat["sunk"] == 1
    assert len(calls) == 1
    assert client.remember_calls == [(e, 0.6, "global")], "P5: importance 0.6 对齐"


# ---- ② 恢复条目较新锚点 --------------------------------------------------

def test_entry_date_days_newer_anchor(tmp_store, meta_for, monkeypatch):
    """P4: _entry_date_days 取 min(内嵌, written_at) — 恢复条目获得全新时钟。

    change-detector 修复 (2026-09-12 评审 D1): 原断言写死 == 30, 而
    days_ago_str(30) 按本地朴素时钟生成、_entry_date_days 按 UTC 解析,
    差值随本地时区/运行时刻在 29/30 间漂移 (早上红下午绿、随日历漂移)。
    修复: 冻结 wm 时钟 + 断言语义区间 {29, 30, 31} 的关系断言, 不再写死绝对天数。
    """
    frozen = datetime.now(timezone.utc)

    class _FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen if tz else frozen.replace(tzinfo=None)

    monkeypatch.setattr(wm, "datetime", _FrozenDatetime)

    e = f"{days_ago_str(30)} 已删: 打印机驱动冲突, 卸载重装。"
    tmp_store.add("memory", e)
    # written_at 用冻结时钟盖章 (与 _entry_date_days 内部 now 同源 → 恒 0 天)
    meta_for("memory").stamp(e, "state", written_at=frozen)
    meta = meta_for("memory").get_entry(e)
    assert wm._entry_date_days(e, meta) == 0, "written_at=冻结 now → 0 天 (较新锚点)"
    # 老 written_at + 老内嵌日期 → 取 min (仍老); days_ago_str 按本地朴素时钟
    # 生成、_entry_date_days 按 UTC 解析, 差值为 30±1 天 (时区相位) — 关系断言
    old = frozen - timedelta(days=60)
    meta_for("memory").stamp(e, "state", written_at=old)
    meta = meta_for("memory").get_entry(e)
    days = wm._entry_date_days(e, meta)
    assert days in (29, 30, 31), f"两锚点均老 → 取较新者 (30±1d), 实际 {days}"
    # 均无 → None
    assert wm._entry_date_days("没有日期的普通条目", {}) is None


def test_restored_entry_not_sunk(tmp_store, mock_client, meta_for, monkeypatch):
    """written_at=now + 内嵌老日期 → 不沉 (较新锚点消解 E5 恢复即沉)。"""
    e = _add_candidate(tmp_store, meta_for, last_active_days=30)
    _add_fillers(tmp_store)
    meta_for("memory").stamp(e, "state",
                             written_at=datetime.now(timezone.utc))
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, mock_client)
    assert e in tmp_store.entries("memory"), "恢复条目不应被老内嵌日期复活年龄"
    assert stat["sunk"] == 0
    assert calls == [], "日期门槛应在 LLM 确认之前生效"


# ---- ③ 冷层查重三态 ------------------------------------------------------

def test_cold_same_no_duplicate_remember(tmp_store, meta_for, monkeypatch):
    """冷层已有 same → 不重复 remember, 只删本地 (计数 sunk)。"""
    e = _add_candidate(tmp_store, meta_for)
    _add_fillers(tmp_store)
    client = RecordingClient(cold_items=[{"content": e, "dense_score": 0.5}])
    _confirm_true(monkeypatch, [])
    stat = _run_tidy(tmp_store, client)
    assert e not in tmp_store.entries("memory"), "冷层已有相同事实 → 删本地"
    assert stat["sunk"] == 1
    assert client.remember_calls == [], "same 级不重复写冷层"


def test_cold_similar_merge_update(tmp_store, meta_for, monkeypatch):
    """冷层已有 similar → merge-update 后删本地, 不 remember。"""
    e = _add_candidate(tmp_store, meta_for)
    _add_fillers(tmp_store)
    cold = "打印机驱动冲突, 卸载重装后解决。"
    client = RecordingClient(cold_items=[{"content": cold, "dense_score": 0.5}])
    _confirm_true(monkeypatch, [])
    stat = _run_tidy(tmp_store, client)
    assert e not in tmp_store.entries("memory"), "similar → 合并更新后删本地"
    assert stat["sunk"] == 1
    assert len(client.update_calls) == 1, "similar 级走 update 合并"
    assert client.remember_calls == [], "similar 级不新写入"
    # update 内容 = _merge_two_entries 输出 (复用 overflow 函数, 不重复实现)
    merged = client.update_calls[0][1]
    assert merged == wm._merge_two_entries(e, cold)


def test_cold_failure_keeps_local(tmp_store, meta_for, monkeypatch):
    """冷层 recall/remember 失败 → 本地保留 (铁律: 冷层写成功才删本地)。"""
    _add_fillers(tmp_store)
    e1 = _add_candidate(tmp_store, meta_for,
                        text=f"{days_ago_str(30)} 已删: 显卡驱动冲突记录。")
    e2 = _add_candidate(tmp_store, meta_for,
                        text=f"{days_ago_str(29)} 已删: 声卡驱动冲突记录。")
    _confirm_true(monkeypatch, [])
    # recall 失败 → 本地保留 + errors
    bad_recall = MockMnemosyneClient(fail_recall=True)
    stat = _run_tidy(tmp_store, bad_recall)
    assert e1 in tmp_store.entries("memory") and e2 in tmp_store.entries("memory"), \
        "recall 失败 → 本地保留"
    assert stat["errors"] >= 1 and stat["sunk"] == 0
    # remember 失败 → 本地保留 + errors
    bad_remember = MockMnemosyneClient(fail_remember=True)
    stat = _run_tidy(tmp_store, bad_remember)
    assert e1 in tmp_store.entries("memory") and e2 in tmp_store.entries("memory"), \
        "remember 失败 → 本地保留"
    assert stat["errors"] >= 1 and stat["sunk"] == 0


# ---- ④ 无 LLM key --------------------------------------------------------

def test_no_llm_key_skips_sink_path(tmp_store, mock_client, meta_for,
                                    monkeypatch):
    """无 LLM_API_KEY → _llm_confirm_sink 返回 False → a) 全部跳过 (保守锁死)。"""
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    e = _add_candidate(tmp_store, meta_for)
    _add_fillers(tmp_store)
    stat = _run_tidy(tmp_store, mock_client)
    assert e in tmp_store.entries("memory"), "无 LLM → a) 全部跳过"
    assert stat["sunk"] == 0 and stat["errors"] == 0
    assert mock_client.stored == [], "无 LLM → 零冷层调用"


# ---- ④b 必改项 2: weekly 硬闸改调 resolver --------------------------------

def test_weekly_gate_respects_file_source_key(tmp_store, mock_client, meta_for,
                                              tmp_path, monkeypatch):
    """env 无 LLM_API_KEY 但文件源 (~/.hermes/.env 白名单) 有 key → b) 合并
    路径必须放行 (原 os.environ.get("LLM_API_KEY") 硬闸会把文件源 key 全拦)。"""
    from memorycore.core import llm_config
    f = tmp_path / "hermes.env"
    f.write_text("DEEPSEEK_API_KEY=sk-file-key\n", encoding="utf-8")
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "1")
    monkeypatch.setattr(llm_config, "ENV_FILE", f)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    llm_config.invalidate_cache()

    a = (f"{days_ago_str(30)} 已删: 打印机驱动冲突处理记录。最终方案: 卸载旧驱动后"
         f"重装官方驱动, 双面打印恢复正常, 故障消除, 相关命令与日志均已归档。")
    b = (f"{days_ago_str(29)} 已删: 打印机驱动冲突处理记录。最终方案: 卸载旧驱动后"
         f"重装官方驱动, 双面打印恢复正常, 故障消除。")
    _add_fillers(tmp_store)
    for t in (a, b):
        _add_candidate(tmp_store, meta_for, text=t, last_active_days=30)

    merge_calls = []
    monkeypatch.setattr(wm, "_llm_confirm_sink", lambda e: False)
    monkeypatch.setattr(wm, "_llm_merge_text",
                        lambda x, y: (merge_calls.append((x, y)), None)[1])

    stat = _run_tidy(tmp_store, mock_client)
    assert merge_calls, ("文件源 key 应使 b) 合并路径放行 (必改项 2); "
                         "硬闸只认 env 时此处会被拦")
    assert stat["merge_skipped"] == 1

    # 对照: 文件来源关闭 + env 无 key → 合并路径整体跳过
    merge_calls.clear()
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "0")
    llm_config.invalidate_cache()
    stat = _run_tidy(tmp_store, mock_client)
    assert merge_calls == [], "无任何 key 来源 → b) 合并路径跳过"
    assert stat["merge_skipped"] == 0


# ---- ⑤ 保护面 ------------------------------------------------------------

def test_protected_redline_not_sunk(tmp_store, mock_client, meta_for,
                                    monkeypatch):
    """protected (红线词) → 不沉 (P0 语义: 不被 smart_tidy 内容判定下沉)。"""
    e = _add_candidate(tmp_store, meta_for,
                       text=f"{days_ago_str(30)} 已删: 打印机驱动冲突。红线: 绝不删除此记录。")
    _add_fillers(tmp_store)
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, mock_client)
    assert e in tmp_store.entries("memory"), "protected 条目不被 tidy 下沉"
    assert stat["sunk"] == 0
    assert calls == [], "保护判定在 LLM 之前"


def test_should_keep_local_pref_not_sunk(tmp_store, mock_client, meta_for,
                                         monkeypatch):
    """should_keep_local=True (用户偏好句) → 不沉 (E4: '不再/放弃' 持续偏好误沉)。"""
    e = _add_candidate(tmp_store, meta_for,
                       text=f"{days_ago_str(30)} 用户偏好: 不再手写 SQL, 一律 ORM。")
    _add_fillers(tmp_store)
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, mock_client)
    assert e in tmp_store.entries("memory"), "用户偏好条目不应被 tidy 下沉"
    assert stat["sunk"] == 0
    assert calls == [], "should_keep_local 豁免在 LLM 之前"


# ---- ⑥ 合并路径查重 ------------------------------------------------------

def test_merge_originals_cold_dedup(tmp_store, meta_for, monkeypatch):
    """合并路径: 两条原文冷层已有 same → 不重复 remember, 仅本地合并。"""
    a = (f"{days_ago_str(30)} 已删: 打印机驱动冲突处理记录。最终方案: 卸载旧驱动后"
         f"重装官方驱动, 双面打印恢复正常, 故障消除, 相关命令与日志均已归档。")
    b = (f"{days_ago_str(31)} 已删: 打印机驱动冲突处理记录。最终方案: 卸载旧驱动后"
         f"重装官方驱动, 双面打印恢复正常, 故障消除。")
    _add_fillers(tmp_store)
    for t in (a, b):
        _add_candidate(tmp_store, meta_for, text=t, last_active_days=30)
    client = RecordingClient(cold_items=[
        {"content": a, "dense_score": 0.5},
        {"content": b, "dense_score": 0.5}])
    monkeypatch.setenv("LLM_API_KEY", "test-key")
    monkeypatch.setattr(wm, "_llm_confirm_sink", lambda e: False)  # a) 不动这两条
    monkeypatch.setattr(wm, "_llm_merge_text",
                        lambda x, y: "合并后的统一打印机驱动冲突处理记录, 要点完整保留。")
    stat = _run_tidy(tmp_store, client)
    ents = tmp_store.entries("memory")
    assert stat["merged"] == 1 and stat["errors"] == 0
    assert client.remember_calls == [], "原文冷层已有 same → 不重复 remember"
    assert any("合并后的统一" in x for x in ents), "热层应有合并文本"
    assert b not in ents and a not in ents


# ---- ⑦ dry-run -----------------------------------------------------------

def test_dry_run_zero_persist(tmp_store, meta_for, monkeypatch):
    """dry-run: store 不变, 冷层零调用, 仅记录拟下沉。"""
    e = _add_candidate(tmp_store, meta_for)
    _add_fillers(tmp_store)
    client = RecordingClient()
    _confirm_true(monkeypatch, [])
    stat = _run_tidy(tmp_store, client, dry=True)
    assert e in tmp_store.entries("memory"), "dry-run 不删本地"
    assert stat["sunk"] == 0 and stat["errors"] == 0
    assert stat["sink_dry"], "dry-run 应记录拟下沉"
    assert client.remember_calls == [] and client.recall_queries == [], \
        "dry-run 零冷层调用"


# ---- ⑧ 上限与保底 --------------------------------------------------------

def test_max_sink_per_run_cap(tmp_store, meta_for, monkeypatch):
    """TIDY_MAX_SINK_PER_RUN=3: 7 条候选只沉 3 条。"""
    for i in range(7):
        _add_candidate(tmp_store, meta_for,
                       text=f"{days_ago_str(30 + i)} 已删: 打印机驱动冲突记录第{i}号。")
    client = RecordingClient()
    _confirm_true(monkeypatch, [])
    stat = _run_tidy(tmp_store, client)
    assert stat["sunk"] == 3, f"上限 3, got {stat['sunk']}"
    assert len(tmp_store.entries("memory")) == 4, "7 - 3 = 4 条保留"


def test_hot_floor_entries_le3_not_drained(tmp_store, meta_for, monkeypatch):
    """热层保底: entries ≤ 3 → 不掏空 (零下沉, 判定在 LLM 之前)。"""
    for i in range(3):
        _add_candidate(tmp_store, meta_for,
                       text=f"{days_ago_str(30 + i)} 已删: 打印机驱动冲突记录第{i}号。")
    client = RecordingClient()
    calls = []
    _confirm_true(monkeypatch, calls)
    stat = _run_tidy(tmp_store, client)
    assert stat["sunk"] == 0 and stat["errors"] == 0
    assert calls == [], "保底判定在 LLM 之前"
    assert len(tmp_store.entries("memory")) == 3, "热层保底不掏空"
    assert client.stored == []
