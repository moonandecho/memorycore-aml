#!/usr/bin/env python3
"""tests/test_rule_stale.py — Phase 3 rule 失效信号机制验收 (设计 §8 断言语义)。

隔离模式同 conftest (tmp_store/mock_client/meta_for, 绝不碰生产数据)。
阶梯语义: 基线=实测占用; L1(≥60%): S2 retype / S5 跨层冗余; L2(≥80%): S4 stub。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from memorycore.core import overflow as ov
from memorycore.core import config as config_mod
from memorycore.core import metadata as meta_mod
from memorycore.core.config import MAX_STUB_PER_RUN, STUB_MAX_CHARS, STUB_PREFIX
from memorycore.core.metadata import MetaStore
from memorycore.core.overflow import run_overflow
from conftest import MockMnemosyneClient, days_ago_str


# ---- 工具 ----------------------------------------------------------------

def _fill_to(tmp_store, target, pct):
    """填充一条大 rule 条目把占用推到目标百分比 (rule 型: 溢流不动它)。"""
    limit = 5000
    want = int(limit * pct) + 30
    need = max(0, want - tmp_store.char_count(target))
    if need:
        tmp_store.add(target, "填充条目" + "甲" * need)


def _stamp_rule(meta_for, entry, days, importance=0.8, target="memory",
                weight=None):
    kw = {"updated_at": datetime.now(timezone.utc) - timedelta(days=days),
          "importance": importance}
    if weight is not None:
        kw["weight"] = weight
    meta_for(target).stamp(entry, "rule", **kw)


def _stamp_stub(meta_for, entry, days, target="memory"):
    meta_for(target).stamp(entry, "stub",
                           updated_at=datetime.now(timezone.utc) - timedelta(days=days),
                           origin="stub_sink")


def _setup_activity(monkeypatch, tmp_path, queries):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    for q in queries:
        meta_mod.log_activity_query(q)


def _judge_all_dormant(monkeypatch):
    monkeypatch.setattr(ov, "_llm_judge_dormant",
                        lambda entries, queries: {e: True for e in entries})


# ---- §8.1 L0: 低占用不动 rule, 零冷层调用 -----------------------------------

def test_l0_low_usage_rule_untouched_no_cold_calls(tmp_store, mock_client):
    entry = f"用户偏好({days_ago_str(100)}): 极简选型, Go/Rust 单二进制, 拒绝重依赖"
    tmp_store.add("memory", entry)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["aged_sunk"] == 0 and stat["stubbed"] == 0
    assert mock_client.stored == []
    assert mock_client.recall_queries == [], "低占用不触发 S5 冷层查重"


# ---- §8.2 S3: 同主题合并 (词法) 保留全部独特信息 ------------------------------

def test_s3_same_topic_merge_preserves_info(tmp_store, mock_client):
    base = "汇报系统状态必须结论先行, 判断标准是卡不卡崩不崩。"
    a = base + "给 available/swap 而非 used。"
    b = base + "用大白话分层结论, 不要术语堆砌。"
    tmp_store.add("memory", a)
    tmp_store.add("memory", b)
    _fill_to(tmp_store, "memory", 0.62)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert stat["merged"] >= 1, stat
    merged = [e for e in ents if "结论先行" in e]
    assert len(merged) == 1, "两条同主题应合并为一条"
    assert "available/swap" in merged[0] and "大白话分层结论" in merged[0], \
        "独特信息不得丢失"


def test_s3_embedding_channel_merges_lexical_distant(tmp_store, mock_client,
                                                     monkeypatch):
    """嵌入通道 (ollama 不可用时降级词法, 此处 mock 嵌入) 发现同主题对。"""
    a = "服务器内存参数记录: zram swap 8GB swappiness 60。"
    b = "服务器内存配置说明: 交换分区与压缩调优结论。"
    tmp_store.add("memory", a)
    tmp_store.add("memory", b)
    monkeypatch.setattr(ov, "_embed_batch",
                        lambda texts: {t: [1.0, 0.0] for t in texts})
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert stat["merged"] >= 1, stat
    merged = [e for e in ents if "zram" in e]
    assert len(merged) == 1 and "调优结论" in merged[0]


# ---- §8.3/8.4 S2: 完成态复核 retype (正/负例) --------------------------------

def test_s2_retype_sinks_old_completed_rule(tmp_store, mock_client, meta_for):
    entry = f"{days_ago_str(65)} 安全审计已修两处漏洞, 已禁 rpcbind 服务, 归档完毕"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry not in tmp_store.entries("memory"), "重判 state 后应 TTL 下沉"
    assert stat["aged_sunk"] >= 1 and stat["retyped"] >= 1, stat
    assert entry in mock_client.stored, "冷层必须先写成功"
    m = meta_for("memory").get_entry(entry)
    assert m and m["type"] == "state" and m["origin"] == "retype_overflow"


@pytest.mark.parametrize("entry", [
    f"{days_ago_str(65)} 安全审计已修两处漏洞, 归档完毕",          # 仅 1 完成态词
    f"{days_ago_str(65)} 用户偏好: 已修两处漏洞, 已禁 rpcbind",    # 行为词
    f"{days_ago_str(20)} 安全审计已修漏洞, 已禁服务",              # 日期 < 60 天
])
def test_s2_retype_negative(tmp_store, mock_client, meta_for, entry):
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["retyped"] == 0
    assert meta_for("memory").get_entry(entry)["type"] == "rule"


def test_s2_retype_cold_fail_restores_rule(tmp_store, meta_for):
    """冷层失败 → 条目原样 + 恢复原 rule 章 + errors≥1 (设计 §4.2 回退)。"""
    entry = f"{days_ago_str(65)} 安全审计已修两处漏洞, 已禁 rpcbind 服务"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    assert entry in tmp_store.entries("memory"), "冷层失败必须保留源"
    assert stat["errors"] >= 1 and stat["retyped"] == 0
    m = meta_for("memory").get_entry(entry)
    assert m["type"] == "rule" and m["origin"] == "legacy", "恢复原 rule 章"


# ---- §8.5 S4: stub-sink ---------------------------------------------------

def test_s4_stub_sink_dormant_b_rule(tmp_store, mock_client, meta_for,
                                     tmp_path, monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制, 几十MB, 一行部署。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["帮我写一个 Python 脚本处理 Excel", "今天天气怎么样"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    stubs = [e for e in ents if e.startswith(STUB_PREFIX)]
    assert len(stubs) == 1, "应替换为 stub 指针"
    assert entry not in ents, "原全文应离开热层"
    assert len(stubs[0]) <= STUB_MAX_CHARS
    assert stat["stubbed"] == 1, stat
    assert entry in mock_client.stored, "全文必须先写冷层确认"
    m = meta_for("memory").get_entry(stubs[0])
    assert m and m["type"] == "stub" and m["origin"] == "stub_sink"


def test_s4_stub_cold_fail_keeps_original(tmp_store, meta_for, tmp_path,
                                          monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    assert entry in tmp_store.entries("memory"), "冷层失败保留原条目"
    assert stat["errors"] >= 1 and stat["stubbed"] == 0


# ---- §8.6 S4 保护线 (2026-08-26 方案 B 修正): A 类/红线/importance -----------
# 铁律"热层无永久保留": protected 参与挤权, 但权重 ×3.0 更难挤 (非豁免)。
# 原断言 "A 类绝不 stub" 已废弃 — 改为验证: ①同等权重下非 protected 先被挤;
# ②只有 protected 时也可退役。

@pytest.mark.parametrize("entry", [
    "行为准则: 汇报系统状态必须结论先行, 给 available/swap 而非 used。",
    "红线: 删除强制走回收站, 绝不能用 rm。",
])
def test_s4_protected_harder_to_evict(tmp_store, mock_client, meta_for,
                                     entry):
    """protected 规则更难被挤: 预算缺口只够挤一条时, 非 protected 先走。

    直接调 enforce_rule_budget (不走 run_overflow 的 S4 干扰), 精确控制
    rule_chars 缺口: 两条规则 ~180 字, filler 填到预算 + 90 字 → 只够挤一条。
    """
    from memorycore.core.overflow import enforce_rule_budget
    from memorycore.core.config import RULE_BUDGET_CHARS
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=100, weight=1.0)  # protected (行为词)
    other = "普通规则: 某技术方案选型记录。"
    tmp_store.add("memory", other)
    _stamp_rule(meta_for, other, days=100, weight=1.0)  # 非 protected
    # filler (rule 型, 超驻留期): rule_chars = 预算 + 90 (只够挤一条 ~90 字)
    fill = "填充甲" + "乙" * (RULE_BUDGET_CHARS - len(entry) - len(other) + 90)
    tmp_store.add("memory", fill)
    _stamp_rule(meta_for, fill, days=100)
    stat = {}
    enforce_rule_budget(tmp_store, mock_client, "memory", meta_for("memory"), stat)
    ents = tmp_store.entries("memory")
    assert entry in ents, "protected ×3.0 更难挤 → 非 protected 优先"
    assert other not in ents, "非 protected 同权重先被挤"
    assert any(e.startswith(STUB_PREFIX) for e in ents)


def test_s4_importance_protected_harder_to_evict(tmp_store, mock_client, meta_for):
    """importance≥0.9 (protected) 更难挤: 预算缺口只够挤一条时, 非 protected 先走。"""
    from memorycore.core.overflow import enforce_rule_budget
    from memorycore.core.config import RULE_BUDGET_CHARS
    entry = "项目偏好: 团队沟通一律用邮件。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=100, importance=0.95, weight=1.0)
    other = "普通规则: 某技术方案选型记录。"
    tmp_store.add("memory", other)
    _stamp_rule(meta_for, other, days=100, importance=0.8, weight=1.0)
    fill = "填充甲" + "乙" * (RULE_BUDGET_CHARS - len(entry) - len(other) + 90)
    tmp_store.add("memory", fill)
    _stamp_rule(meta_for, fill, days=100)
    stat = {}
    enforce_rule_budget(tmp_store, mock_client, "memory", meta_for("memory"), stat)
    ents = tmp_store.entries("memory")
    assert entry in ents, "importance≥0.9 更难挤 → 非 protected 优先"
    assert other not in ents


def test_protected_only_can_be_evicted(tmp_store, mock_client, meta_for,
                                       tmp_path, monkeypatch):
    """无永久保留铁律: 只有 protected 规则超预算时, 它也可被挤 (×3.0 只是更难)。"""
    entry = "红线: 删除强制走回收站, 绝不能用 rm。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=200, weight=0.1)  # 权重极低 (长期失活)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    # 无其他候选 (filler 非 rule) → protected 也可退役 (权重 ×3.0 后仍最低)
    assert any(e.startswith(STUB_PREFIX) for e in ents) or entry not in ents


# ---- §8.7 S4 降级: 日志关闭 / LLM 失败 / 词法活跃 → 不 stub ------------------

def test_s4_disabled_when_log_disabled(tmp_store, mock_client, meta_for,
                                       tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", False)
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _judge_all_dormant(monkeypatch)  # 即使 judge 判休眠也不出手
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["stubbed"] == 0


def test_s4_llm_fail_no_stub(tmp_store, mock_client, meta_for, tmp_path,
                             monkeypatch):
    """2026-08-26 机制演进: 候选筛选纯词法, 不再依赖 LLM 判定。

    旧语义 (LLM 失败 → 全活跃 → 不 stub) 已废弃 — 挤权用 sb≥2 词法 + 权重,
    无 LLM 依赖; 挤错有冷层全文 + 召回恢复兜底 (设计铁律 2)。
    本测试验证: 无关查询 (词法不命中) 时规则仍可被挤, LLM 失败不影响。
    """
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50, weight=0.5)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])  # 词法不命中
    monkeypatch.setattr(ov, "_llm_judge_dormant",
                        lambda entries, queries: {})  # 失败 → 全活跃 (不影响挤权)
    stat = run_overflow(tmp_store, mock_client, "memory")
    # 词法不活跃 + 超预算 → 可被挤 (stub-sink 或 S4); LLM 失败不再保护
    assert any(e.startswith(STUB_PREFIX) for e in tmp_store.entries("memory")) \
        or entry not in tmp_store.entries("memory")


def test_s4_lexical_active_no_llm_call(tmp_store, mock_client, meta_for,
                                       tmp_path, monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["自托管选型偏好是什么"])
    called = []
    monkeypatch.setattr(
        ov, "_llm_judge_dormant",
        lambda entries, queries: called.append(1) or {e: True for e in entries})
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["stubbed"] == 0
    assert called == [], "词法活跃应零成本快筛, 不调 LLM"


# ---- §8.8 S5: 跨层冗余清除 --------------------------------------------------

def test_s5_cross_layer_dedup_removes_hot_copy(tmp_store, meta_for):
    entry = "服务器事实: /tmp 是 tmpfs 重启即清空。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=40)
    _fill_to(tmp_store, "memory", 0.62)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    assert entry not in tmp_store.entries("memory"), "冷层已有等价全文 → 删本地"
    assert stat["overflowed"] >= 1, stat
    assert cold.stored == [], "冷层已有 → 不重复写"


def test_s5_skips_recent_rule(tmp_store, meta_for):
    """闲置 < 30 天不查冷层 (历史冗余面向, 省 recall 开销)。"""
    entry = "服务器事实: /tmp 是 tmpfs 重启即清空。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=5)
    _fill_to(tmp_store, "memory", 0.62)
    cold = MockMnemosyneClient(cold_items=[{"content": entry, "dense_score": 0.95}])
    stat = run_overflow(tmp_store, cold, "memory")
    assert entry in tmp_store.entries("memory")
    assert cold.recall_queries == [], "闲置不足不查冷层"


# ---- §8.9 阶梯 greedy 停止 ---------------------------------------------------

def test_l2_greedy_stops_below_hard(tmp_store, mock_client, meta_for,
                                    tmp_path, monkeypatch):
    """阶梯渐进: 每轮 stub ≤ MAX_STUB_PER_RUN, 且预算制收敛 (不单轮抽空)。"""
    e1 = ("自托管选型偏好: 极轻极简单二进制, 几十MB一行部署。"
          + "部署在服务器上的应用保持单二进制形态, 一行命令启动和维护, "
            "不引入额外依赖与守护进程。")
    e2 = ("购买偏好: 不追新只买需要的, 按需求短板升级。"
          + "只有实际遇到瓶颈时才考虑采购, 不为降价或囤货消费, "
            "硬件预算投向内存与硬盘。")
    e3 = ("硬件选型偏好: 中端芯片加够用内存就是黄金档。"
          + "不打游戏不跑本地大模型, 轻量模型跑轻量设备, "
            "旗舰性能属于浪费, 散热噪音也要考量。")
    for e in (e1, e2, e3):
        tmp_store.add("memory", e)
        _stamp_rule(meta_for, e, days=50)
    _fill_to(tmp_store, "memory", 0.81)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat = run_overflow(tmp_store, mock_client, "memory")
    # 2026-08-26 机制演进: 预算制 (LRU) 取代 S4 的"停在硬线下"语义 —
    # 挤权按 RULE_BUDGET_CHARS 收敛, 不再按 80% 硬线停; 渐进性由
    # MAX_EVICT_PER_RUN 保证 (每轮 ≤3, 多轮收敛, 不单轮抽空)。
    assert 1 <= stat.get("stubbed", 0) <= MAX_STUB_PER_RUN, stat
    assert stat.get("lru_evicted", 0) <= MAX_STUB_PER_RUN, stat
    ents = tmp_store.entries("memory")
    assert any(e.startswith(STUB_PREFIX) for e in ents), "至少产生 stub 指针"
    # 冷层有全部被挤条目的全文 (信息零丢失)
    assert len(mock_client.stored) >= 1


# ---- §8.10 stub GC: 最老优先, 冷层零调用 -------------------------------------

def test_stub_gc_removes_oldest_pointers(tmp_store, mock_client, meta_for):
    topics = ["自托管选型", "服务器运维", "写作风格", "安全审计", "硬件采购"]
    stubs = []
    for i, kw in enumerate(topics):
        s = f"{STUB_PREFIX}{kw}→recall(\"{kw}\")"
        tmp_store.add("memory", s)
        _stamp_stub(meta_for, s, days=10 + i)  # 年龄 10..14 天
        stubs.append(s)
    _fill_to(tmp_store, "memory", 0.82)
    stat = run_overflow(tmp_store, mock_client, "memory")
    ents = tmp_store.entries("memory")
    assert 1 <= stat["stub_gc"] <= MAX_STUB_PER_RUN, stat
    assert stubs[4] not in ents, "最老的指针优先回收"
    assert stubs[0] in ents, "最年轻的指针保留"
    assert mock_client.forgotten == [], "stub GC 只删指针, 冷层零调用"
    assert mock_client.stored == []


# ---- §8.11 stub 驻留 (低占用不 GC 不压缩) -------------------------------------

def test_stub_kept_at_low_usage(tmp_store, mock_client, meta_for):
    s = f"{STUB_PREFIX}主题X→recall(\"主题X\")"
    tmp_store.add("memory", s)
    _stamp_stub(meta_for, s, days=30)
    stat = run_overflow(tmp_store, mock_client, "memory")
    assert s in tmp_store.entries("memory")
    assert stat["stub_gc"] == 0 and stat["stubbed"] == 0
    assert mock_client.stored == []


# ---- §8.12 安全路径 + §8.13 幂等 ---------------------------------------------

def test_rule_ladder_cold_fail_keeps_all(tmp_store, meta_for):
    """S2+S4 全链冷层失败 → 条目原样 + errors≥1 (安全路径铁律)。"""
    entry = f"{days_ago_str(65)} 安全审计已修两处漏洞, 已禁 rpcbind 服务"
    tmp_store.add("memory", entry)
    meta_for("memory").stamp(entry, "rule", origin="legacy")
    _fill_to(tmp_store, "memory", 0.62)
    bad = MockMnemosyneClient(fail_remember=True)
    stat = run_overflow(tmp_store, bad, "memory")
    assert entry in tmp_store.entries("memory")
    assert stat["errors"] >= 1


def test_idempotent_second_run_no_side_effects(tmp_store, mock_client, meta_for,
                                               tmp_path, monkeypatch):
    entry = "自托管选型偏好: 极轻极简, Go/Rust 单二进制。"
    tmp_store.add("memory", entry)
    _stamp_rule(meta_for, entry, days=50)
    _fill_to(tmp_store, "memory", 0.82)
    _setup_activity(monkeypatch, tmp_path, ["无关查询"])
    _judge_all_dormant(monkeypatch)
    stat1 = run_overflow(tmp_store, mock_client, "memory")
    assert stat1["stubbed"] == 1
    stat2 = run_overflow(tmp_store, mock_client, "memory")
    assert stat2["stubbed"] == 0 and stat2["stub_gc"] == 0, "第二次无副作用"


# ---- 采集面: activity 日志 (metadata 单元) -----------------------------------

def test_activity_log_roundtrip_and_truncate(tmp_path, monkeypatch):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    meta_mod.log_activity_query("  测试查询内容  ")
    qs = meta_mod.load_recent_queries(days=1)
    assert any("测试查询内容" in q for q in qs)
    meta_mod.log_activity_query("长" * 300)
    qs = meta_mod.load_recent_queries(days=1)
    assert max(len(q) for q in qs) <= 200, "日志截前 200 字"
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", False)
    meta_mod.log_activity_query("不应写入")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    qs = meta_mod.load_recent_queries(days=1)
    assert all("不应写入" not in q for q in qs), "关闭时不得落盘" 


def test_activity_log_compaction(tmp_path, monkeypatch):
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "act.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_MAX_BYTES", 4096)
    for i in range(300):
        meta_mod.log_activity_query(f"查询{i} " + "x" * 40)
    assert os.path.getsize(tmp_path / "act.jsonl") <= 4096, "超限滚动压缩"


def test_prefetch_logs_activity(tmp_path, monkeypatch):
    """插件烟测: prefetch 每轮写 activity 日志 (模块级替换, 零生产副作用)。"""
    import importlib.util
    plugin_path = (Path(__file__).resolve().parent.parent
               / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")
    spec = importlib.util.spec_from_file_location("memorycore_prefetch_log_smoke",
                                                  plugin_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "act.jsonl")
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    mod.MnemosyneClient = lambda **k: MockMnemosyneClient()
    provider = mod.MemoryCorePrefetchProvider()
    provider._recall_sync("自托管选型调查")
    qs = meta_mod.load_recent_queries(days=1)
    assert any("自托管选型调查" in q for q in qs)


# ---- server: store_fact importance 透传 + audit stub 展示 ---------------------

def test_store_fact_stamps_importance(tmp_store, mock_client, meta_for):
    from memorycore import server
    server._store = tmp_store
    server._client = mock_client
    r = json.loads(server.memorycore_store_fact("用户偏好: 极简选型", importance=0.95))
    assert r["status"] == "stored", r
    m = meta_for("memory").get_entry("用户偏好: 极简选型")
    assert m and m["importance"] == 0.95


def test_audit_shows_stub_plan(tmp_store, mock_client):
    from memorycore import server
    server._store = tmp_store
    server._client = mock_client
    s = f"{STUB_PREFIX}主题Y→recall(\"主题Y\")"
    tmp_store.add("memory", s)
    MetaStore("memory", memory_path=tmp_store.memory_path,
              user_path=tmp_store.user_path).stamp(s, "stub", origin="stub_sink")
    r = json.loads(server.memorycore_memory_audit("memory"))
    row = r["memory"]["rows"][0]
    assert row["type"] == "stub"
    assert "stub" in row["plan"]
    assert row["keep"] is True
