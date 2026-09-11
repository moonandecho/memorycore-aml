#!/usr/bin/env python3
"""tests/test_e_visible.py — E 节静默降级治理回归测试 (Phase 5)

原则: 降级可以, 但必须可见 (计数 / 日志 / 报告字段 / 该失败就失败)。
覆盖: E3 回收站写失败计数+阻断删除 / E4 活性扫描失败计数 /
      E5 merge 路径嵌入失败对齐 embed_fail / E8 env 开关惰性解析。
"""
import pytest

from memorycore.core import config as config_mod  # noqa: E402
from memorycore.core import overflow as ov_mod  # noqa: E402
from memorycore.core import metadata as meta_mod  # noqa: E402
from conftest import MockMnemosyneClient  # noqa: E402


# ---- E3: 回收站写失败 → 计数 + 不许继续删源 --------------------------------

def test_e3_trash_add_failure_blocks_forget():
    """回收站写失败 → forget 不得执行 (可恢复性保护), stat["trash_fail"] 计数。"""
    from memorycore.core import maintenance as maint

    class FailingTrash:
        def add(self, *a, **k):
            raise OSError("disk full (mock)")

        def get_all(self):
            return []

        def get_expired(self):
            return []

        def count(self):
            return 0

        def remove(self, *a):
            return True

    client = MockMnemosyneClient()
    stat = {}
    entries = [{"id": "x1", "content": "旧条目", "importance": 0.1,
                "last_recalled": "2020-01-01T00:00:00Z"}]
    forgotten, errors = maint._forget_decayed(client, entries,
                                              trash=FailingTrash(), stat=stat)
    assert forgotten == 0
    assert client.forgotten == [], "回收站写失败时不许 forget (删源被阻断)"
    assert stat["trash_fail"] == 1


def test_e3_add_observed_helper(tmp_path):
    from pathlib import Path

    from memorycore.trash_store import TrashStore, add_observed

    class FailingTrash(TrashStore):
        def add(self, *a, **k):
            raise OSError("disk full (mock)")

    stat = {}
    assert add_observed(FailingTrash(), "m1", "content", "r", "s", stat) is False
    assert stat["trash_fail"] == 1

    # 真实写失败场景 (终审低危修复): 原断言传 str 路径, 靠 str 无 with_suffix
    # 的 AttributeError 巧合通过 — 触发方式非预期且语义失真 (并在 /tmp 留垃圾)。
    # 现改为: 父路径是普通文件 → lock 文件 mkdir 抛 FileExistsError (真 OSError),
    # 触发"回收站磁盘写失败"预期语义; tmp_path 自动清理。
    blocker = tmp_path / "blocker"
    blocker.write_text("not a dir")
    ok_stat = {}
    assert add_observed(TrashStore(path=Path(blocker) / "trash.json"),
                        "m2", "content", "r", "s", ok_stat) is False
    assert ok_stat["trash_fail"] == 1


def test_e3_merge_duplicates_reversal_trash_first(monkeypatch):
    """终审 R1 回归: 规则反转分支必须回收站先写成功才许 forget — 写失败 →
    计数 stat["trash_fail"] + 阻断删除 (不再出现先删后备份)。"""
    from memorycore.core import maintenance as maint
    from memorycore.trash_store import TrashStore, add_observed

    entries = [
        {"id": "c0", "content": "沟通时使用中文。",
         "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "c1", "content": "停止沟通时使用中文。",
         "timestamp": "2026-02-01T00:00:00Z"},
    ]
    client = MockMnemosyneClient(cold_items=[
        {"content": entries[0]["content"], "dense_score": 0.9},
        {"content": entries[1]["content"], "dense_score": 0.9},
    ])
    stat = {}
    monkeypatch.setattr(TrashStore, "add",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("disk full (mock)")))
    merged, _, rev, _, to_forget = maint._merge_duplicates(client, entries, stat)
    assert merged == 0
    assert rev == 0
    assert client.forgotten == [], "回收站写失败时反转分支不许 forget (先删后备份禁止)"
    assert not to_forget, "写失败时不得标记已删 (下轮应重试)"
    assert stat["trash_fail"] == 1


def test_e3_merge_duplicates_hashdedup_trash_failure_blocks_forget(monkeypatch):
    """终审 R2 回归: hash-dedup 分支回收站写失败 → add_observed 计数 + 阻断
    forget (victim 保留, 下轮重试; 原裸 add 零计数零告警)。"""
    from memorycore.core import maintenance as maint
    from memorycore.trash_store import TrashStore, add_observed

    entries = [
        {"id": "c0", "content": "沟通时使用中文。",
         "timestamp": "2026-01-01T00:00:00Z"},
        {"id": "c1", "content": "沟通时使用中文。",
         "timestamp": "2026-01-02T00:00:00Z"},
    ]
    client = MockMnemosyneClient(cold_items=[])  # 无向量邻居 → 只走哈希兜底
    stat = {}
    monkeypatch.setattr(TrashStore, "add",
                        lambda *a, **k: (_ for _ in ()).throw(
                            OSError("disk full (mock)")))
    merged, _, rev, _, to_forget = maint._merge_duplicates(client, entries, stat)
    assert merged == 0
    assert client.forgotten == [], "回收站写失败时 hash-dedup 不许 forget"
    assert not to_forget, "写失败时不得标记已删 (下轮应重试)"
    assert stat["trash_fail"] == 1


# ---- E4: 活性扫描失败 → 计数 + 日志 (不再零信号) ---------------------------

def test_e4_activity_scan_failure_counted(tmp_store, mock_client, monkeypatch,
                                          caplog):
    monkeypatch.setattr(ov_mod, "apply_activity_hits",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    tmp_store.add("memory", "规则甲: 用词简洁。")
    stat = ov_mod.run_overflow(tmp_store, mock_client, "memory")
    assert stat["errors"] >= 1, "活性扫描失败必须计数 (原 except: pass 零信号)"
    assert "活性命中扫描失败" in caplog.text


# ---- E5: merge 路径嵌入失败 → 对齐 activity 路径 embed_fail ----------------

def test_e5_merge_embed_fail_counted(monkeypatch):
    monkeypatch.setattr(ov_mod, "_embed_batch", lambda texts: None)
    stat = {}
    merged, count = ov_mod._merge_local_fragments(
        ["规则甲: 用词简洁, 注释用中文。", "规则甲: 用词简洁, 注释中文。"], stat)
    assert stat["embed_fail"] == 1, "merge 路径嵌入失败必须对齐 embed_fail 计数"
    # 无 stat 时静默降级 (向后兼容), 不崩溃
    ov_mod._merge_local_fragments(
        ["规则甲: 用词简洁, 注释用中文。", "规则甲: 用词简洁, 注释中文。"])


# ---- E8: 4 个 env 开关惰性解析 (修 import 冻结) -----------------------------

def test_e8_env_switches_lazy(monkeypatch):
    # conftest autouse 会把 config.ACTIVITY_LOG_ENABLED 钉为实例属性,
    # 先移除让惰性读取回到 env 通道 (monkeypatch 会自动恢复)
    monkeypatch.delattr(config_mod, "ACTIVITY_LOG_ENABLED", raising=False)
    monkeypatch.setenv("ACTIVITY_LOG_ENABLED", "1")
    monkeypatch.setenv("MEMORYCORE_RULE_BUDGET_ENABLED", "1")
    monkeypatch.setenv("MEMORYCORE_HIT_WEAK_MODE", "degraded")
    monkeypatch.setenv("MEMORYCORE_EMBED_BACKEND", "mnemosyne")
    assert ov_mod.ACTIVITY_LOG_ENABLED is True
    assert ov_mod.RULE_BUDGET_ENABLED is True
    assert ov_mod.HIT_WEAK_MODE == "degraded"
    assert ov_mod.EMBED_BACKEND == "mnemosyne"
    assert meta_mod.ACTIVITY_LOG_ENABLED is True

    # 运行中改 env → 即时生效 (import 时冻结模式这里仍是旧值)
    monkeypatch.setenv("ACTIVITY_LOG_ENABLED", "0")
    monkeypatch.setenv("MEMORYCORE_EMBED_BACKEND", "off")
    assert ov_mod.ACTIVITY_LOG_ENABLED is False
    assert ov_mod.EMBED_BACKEND == "off"
    assert meta_mod.ACTIVITY_LOG_ENABLED is False

    # monkeypatch.setattr 覆盖契约不变 (模块实例属性遮蔽惰性读取)
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
    assert ov_mod.ACTIVITY_LOG_ENABLED is True
    assert meta_mod.ACTIVITY_LOG_ENABLED is True
# ---- L1/L3: _trash_cycle remove 失败可见化 (终审低危收尾) -------------------

def _old_trashed_at(days: int = 40) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _trash_warnings(caplog):
    """TRASH: 前缀的 WARNING 记录 (三仓日志文案语言不同, 前缀同源)。"""
    return [r for r in caplog.records
            if r.levelname == "WARNING" and r.message.startswith("TRASH:")]


def test_trash_cycle_expiry_remove_write_failure_counted(monkeypatch, tmp_path,
                                                         caplog):
    """L1: 到期清空 remove 写失败 → 计数 stat["trash_fail"] + 可见告警, 不上抛
    中断整轮治理; 记录保留 (原子写失败=文件未动), 下轮重试成功才计 cleared。"""
    import memorycore.trash_store as ts_mod
    from memorycore.trash_store import TrashStore
    from memorycore.core import maintenance as maint

    monkeypatch.setattr(ts_mod, "TRASH_PATH", tmp_path / "trash.json")
    trash = TrashStore()
    trash.add("m1", "内容一")
    trash.add("m2", "内容二")
    # 写成 40 天前入站 → 已过期 (不依赖 ttl 注入, 三仓同源)
    data = trash._load()
    for e in data["entries"]:
        e["trashed_at"] = _old_trashed_at()
    trash._save(data)

    class Client:
        def __init__(self):
            self.forgotten = []
        def recall_results(self, *a, **k):
            return []
        def forget(self, mid):
            self.forgotten.append(mid)

    client = Client()
    orig_save = TrashStore._save
    calls = {"n": 0}

    def flaky_save(self, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full (mock)")
        return orig_save(self, data)

    monkeypatch.setattr(TrashStore, "_save", flaky_save)
    stat = {}
    revived, cleared = maint._trash_cycle(client, stat)
    assert revived == 0 and cleared == 1, "m1 写失败不计 cleared, m2 成功计 1"
    assert stat["trash_fail"] == 1
    assert len(_trash_warnings(caplog)) == 1, "remove 失败必须有可见告警"
    ids = [e["memory_id"] for e in trash.get_all()]
    assert ids == ["m1"], "写失败 → 记录保留在站 (不会出现记录删了但冷层没删)"
    assert client.forgotten == ["m1", "m2"], "forget 先行且按序 (幂等)"

    # 第二轮: 故障清除 → 重试成功, 记录清空, 不再计数
    stat2 = {}
    revived2, cleared2 = maint._trash_cycle(client, stat2)
    assert cleared2 == 1 and stat2.get("trash_fail", 0) == 0
    assert trash.get_all() == []
    assert client.forgotten == ["m1", "m2", "m1"], "m1 下轮重试 forget (幂等)"


def test_trash_cycle_revive_remove_write_failure_counted(monkeypatch, tmp_path,
                                                         caplog):
    """L1: 召回恢复路径 remove 写失败 → 计数 + 可见告警, 记录保留 (冷层条目
    本就未 forget, 零不一致), 下轮 recall 重判重试。"""
    import memorycore.trash_store as ts_mod
    from memorycore.trash_store import TrashStore
    from memorycore.core import maintenance as maint

    monkeypatch.setattr(ts_mod, "TRASH_PATH", tmp_path / "trash.json")
    trash = TrashStore()
    trash.add("m1", "重要偏好记录")

    class Client:
        def __init__(self):
            self.forgotten = []
        def recall_results(self, *a, **k):
            return [{"id": "m1"}]
        def forget(self, mid):
            self.forgotten.append(mid)

    client = Client()
    monkeypatch.setattr(
        TrashStore, "_save",
        lambda self, data: (_ for _ in ()).throw(OSError("disk full (mock)")))
    stat = {}
    revived, cleared = maint._trash_cycle(client, stat)
    assert revived == 0 and cleared == 0, "写失败不计 revived (记录仍在站)"
    assert stat["trash_fail"] == 1
    assert client.forgotten == [], "恢复路径绝不 forget 冷层"
    assert [e["memory_id"] for e in trash.get_all()] == ["m1"], \
        "写失败 → 记录保留, 零不一致"
    assert len(_trash_warnings(caplog)) == 1


def test_trash_cycle_remove_false_counted(monkeypatch, tmp_path, caplog):
    """L3: remove 返回 False (条目不在站) → 计数 + 可见告警, 返回值不再被
    静默忽略 (同口径可见化)。"""
    import memorycore.trash_store as ts_mod
    from memorycore.trash_store import TrashStore
    from memorycore.core import maintenance as maint

    monkeypatch.setattr(ts_mod, "TRASH_PATH", tmp_path / "trash.json")

    class Client:
        def __init__(self):
            self.forgotten = []
        def recall_results(self, *a, **k):
            return []
        def forget(self, mid):
            self.forgotten.append(mid)

    client = Client()
    # 回收站为空 + get_expired 返回幽灵条目 → remove 必然 False
    monkeypatch.setattr(TrashStore, "get_expired",
                        lambda self: [{"memory_id": "ghost"}])
    stat = {}
    revived, cleared = maint._trash_cycle(client, stat)
    assert revived == 0 and cleared == 0
    assert stat["trash_fail"] == 1
    assert len(_trash_warnings(caplog)) == 1


# ---- L2: LLM 候选轮转游标 (终审低危 D2 长尾饿死修复) ------------------------

def test_llm_rot_rotate_round_robin(monkeypatch, tmp_path):
    """llm_rot: 轮转顺序正确 + 偏移持久推进 + n<=1 恒等不推进。"""
    from memorycore.core import llm_rot

    monkeypatch.setattr(llm_rot, "ROT_PATH", tmp_path / "llm_rot.json")
    items = ["a", "b", "c"]
    assert llm_rot.rotate("ns", items) == ["a", "b", "c"]
    assert llm_rot.rotate("ns", items) == ["b", "c", "a"]
    assert llm_rot.rotate("ns", items) == ["c", "a", "b"]
    assert llm_rot.rotate("ns2", []) == []
    assert llm_rot.rotate("ns2", ["x"]) == ["x"]  # n<=1 恒等, 不推进


def test_llm_rot_corrupt_state_degrades_to_zero(monkeypatch, tmp_path, caplog):
    """llm_rot: 状态文件损坏 → 偏移 0 (修复前行为), 不崩溃。"""
    from memorycore.core import llm_rot

    monkeypatch.setattr(llm_rot, "ROT_PATH", tmp_path / "llm_rot.json")
    (tmp_path / "llm_rot.json").write_text("{corrupt", encoding="utf-8")
    assert llm_rot.rotate("ns", ["a", "b"]) == ["a", "b"]
    assert any("LLMROT" in r.message for r in caplog.records)


def test_overflow_rotation_no_starvation(tmp_store, mock_client, monkeypatch,
                                         tmp_path):
    """L2 回归: 10 条长候选 + LLM 持续"成功但校验不过" (头部永不消解) →
    修复前尾部候选永久饿死 (实测: 2 轮零次尝试); 轮转游标保证 3 轮内全部
    候选至少被尝试一次。"""
    from memorycore.core import llm_rot
    from memorycore.core import llm_config
    from memorycore.core import overflow as ov_mod

    monkeypatch.setattr(llm_rot, "ROT_PATH", tmp_path / "llm_rot.json")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-rotation")
    llm_config.invalidate_cache()
    # 隔离环境嵌入服务 (有 ollama 时条目会被语义聚类合并, 场景失真)
    monkeypatch.setattr(ov_mod, "_embed_batch", lambda texts: None)

    attempted = []

    def fake_chat(cfg, payload, timeout=None):
        import json as _json
        import re as _re
        m = _re.search(r"<entry>(.*?)</entry>",
                       payload["messages"][0]["content"], _re.S)
        attempted.append(m.group(1))
        # 更长 → 校验不过 → 条目保持原样 (成功但不可消解)
        return {"choices": [{"message": {"content": _json.dumps(
            {"compressed": "x" * 300})}}]}

    monkeypatch.setattr(llm_config, "chat", fake_chat)

    entries = []
    for i in range(10):
        entries.append(f"2026-01-01 用户偏好: 编号{i}的专属行文规范是"
                       + chr(0x4E00 + i) * 190)
    for e in entries:
        tmp_store.add("memory", e)

    seen = set()
    for _ in range(3):
        stat = ov_mod.run_overflow(tmp_store, mock_client, "memory")
        assert stat["llm"]["calls"] == 8, "每轮恰好上限 8"
        assert stat["llm"]["skipped_cap"] == 2
        assert stat["compressed"] == 0, "校验不过 → 保持原样 (不可消解场景)"
        seen.update(attempted)
    assert len(seen) == 10, f"3 轮后全部候选必须被尝试过, 实际 {len(seen)}"


def test_cold_dedup_rotation_no_starvation(mock_client, monkeypatch, tmp_path):
    """L2 回归: 10 个模糊去重组 + LLM 持续判 not_duplicate (成功但不消解) →
    修复前尾部组永久饿死; 轮转游标保证 3 轮内每组至少被尝试一次。"""
    from memorycore.core import llm_rot
    from memorycore.core import llm_config
    from memorycore.core import maintenance as maint

    monkeypatch.setattr(llm_rot, "ROT_PATH", tmp_path / "llm_rot.json")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-rotation")
    llm_config.invalidate_cache()

    seen = []

    def fake_chat(cfg, payload, timeout=None):
        import json as _json
        import re as _re
        ids = _re.findall(r"\[(\w+)\]\s",
                          payload["messages"][0]["content"])
        seen.extend(ids[:1])
        return {"choices": [{"message": {"content": _json.dumps(
            {"decision": "not_duplicate"})}}]}

    monkeypatch.setattr(llm_config, "chat", fake_chat)

    groups = [[{"id": f"g{i}a", "content": f"组{i}第一条"},
               {"id": f"g{i}b", "content": f"组{i}第二条"}]
              for i in range(10)]
    for _ in range(3):
        g = llm_config.start_session(stat={}, max_calls=8,
                                     name="test-cold-rot")
        try:
            merged, forgotten = maint._llm_dedup_confirm(
                mock_client, groups, {})
        finally:
            g.close()
        assert merged == 0 and forgotten == set()
    assert len(set(seen)) == 10, f"3 轮后全部模糊组必须被尝试过, 实际 {len(set(seen))}"
