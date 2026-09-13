#!/usr/bin/env python3
"""fix4_acceptance_probes.py — FIX4 P0 双向验收 (A/B/C 组 + 原 15 噪声)。

逐条输出:
  * A 组 31 条真实规则自然提问: gate=false, 冷层真实规则可注入;
  * B 组 3 条 snapshot 真实规则提问: 同上;
  * C 组 5 条强动作+H/K 共识: gate 让位, 冷层 RPC>0, 全文写回;
  * 原 15 条噪声: gate=true, 全文注入=0。

用法:
  .venv/bin/python tools/fix4_acceptance_probes.py --json-out /tmp/fix4-p0.json
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parent
_hp = os.environ.get("HERMES_AGENT_DIR")
if _hp and Path(_hp).exists():
    sys.path.insert(0, _hp)
sys.path.insert(0, str(ROOT))
from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import _make_stub  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402

A_QUERIES = [
    "天气服务配置改了必须先通知我", "周末批量发布任务必须提前通知团队",
    "放假前必须冻结发布并通知值班人", "旅游保险服务更新前要通知我",
    "爬山俱乐部活动报名服务上线前必须通知我",
    "拍照检测任务部署前要经过审批", "头像上传服务停机维护要通知用户",
    "壁纸网站采集任务上线前必须通知我", "办公全家桶安装脚本更新前必须通知我",
    "游戏机回收流程变更前要通知我", "电影票接口服务发布前必须通知我",
    "奶茶店订单服务配置修改前必须通知我", "咖啡机固件更新前必须通知我",
    "健身数据服务配置修改前先通知我", "散步路径推荐服务上线前必须通知我",
    "逛街比价插件发布前必须通知我", "Python 服务更新前必须先通知我",
    "Excel 导出任务失败必须通知我", "CSV 导入规则修改前先确认回滚",
    "把 GIL 相关服务升级前必须通知我", "一首诗生成服务发布前必须通知用户",
    "吃什么推荐服务配置修改前必须通知我",
    "回复得不错自动回复模板更新前必须通知我",
    "主播怎么样看板发布前必须通知我",
    "好的，那就按对外发布必须通知用户的规则来",
    "收到，就按系统配置改动必须通知我的规则执行",
    "可以，那就按继续执行前确认回滚点的规则办",
    "明白，按你说的发送前必须通知审核人的规则来",
    "嗯，就按回复前检查红线规则的流程处理",
    "好的，继续按写周报要附带服务状态的规则执行",
    "[IMPORTANT: 运维] 对外发布必须通知用户",
]
B_QUERIES = [
    "做 PPT 前用哪个 skill 必须问用户吗，还是直接走 python-pptx",
    "demo-host 游戏机定位和性能优先偏好是否覆盖通用省电策略",
    "MemoryCore 周治理是否已经用 python 脚本编排并降低 cron 耦合",
]
C_QUERIES = [
    "更新 Python 服务配置并通知我", "周末对外发布上线前通知用户",
    "电影服务发布前必须通知用户", "头像服务停机维护前通知用户",
    "准备对外发布通知用户",
]
NOISE_QUERIES = [
    "今天天气真不错，晚上吃什么好呢", "好的，收到，明白了", "嗯嗯，继续",
    "在吗", "谢谢", "Python 的 GIL 是什么", "怎么把 Excel 转成 CSV",
    "[IMPORTANT: You have 1 unread message from system] 请继续",
    "[ASYNC DELEGATION] background task done", "帮我写一首关于春天的诗",
    "你刚才回复得不错", "我想更新一下头像",
    "周末打算去爬山，顺便拍点照片", "好的，那就按你说的写吧",
    "怎么安装 Python 包",
]


class FakeRecall:
    def __init__(self, results=None):
        self.results = list(results or [])
        self.queries = []

    def recall_results(self, q, top_k=5, bump=True):
        self.queries.append((q, top_k, bump))
        return [dict(r) for r in self.results]



def _ensure_agent_mock() -> None:
    """Standalone run without Hermes: inject the same read-only mock as tests/conftest."""
    if "agent.memory_provider" in sys.modules:
        return
    try:
        import agent.memory_provider  # noqa: F401
        return
    except Exception:
        pass
    import re as _re
    import types as _types
    _agent_pkg = _types.ModuleType("agent")
    _agent_pkg.__path__ = []  # type: ignore[attr-defined]
    _provider_mod = _types.ModuleType("agent.memory_provider")

    class _MemoryProvider:
        def __init__(self, *a, **k):
            pass

    _TRIVIAL_PROMPT_RE = _re.compile(
        r"^(?:yes|no|ok|okay|sure|thanks|hi|hey|hello|continue|"
        "got it|done|next|lgtm|k)[\\s!?.:;,\"']*$", _re.IGNORECASE)

    def _is_trivial_prompt(text):
        q = (text or "").strip()
        if not q or q.startswith("/"):
            return True
        return bool(_TRIVIAL_PROMPT_RE.match(q))

    _provider_mod.MemoryProvider = _MemoryProvider
    _provider_mod.TRIVIAL_PROMPT_RE = _TRIVIAL_PROMPT_RE
    _provider_mod.is_trivial_prompt = _is_trivial_prompt
    sys.modules["agent"] = _agent_pkg
    sys.modules["agent.memory_provider"] = _provider_mod


def _load_plugin():
    _ensure_agent_mock()
    path = Path(os.environ.get(
        "MEMORYCORE_PLUGIN_PATH",
        str(ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")))
    spec = importlib.util.spec_from_file_location("fix4_probe_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.log_activity_query = lambda q: None
    return mod


def _new_provider(mod, root: Path, tag: str):
    store = LocalStore(root / f"{tag}_M.md", root / f"{tag}_U.md")
    metas: Dict[str, MetaStore] = {}

    def meta_for(target, **kw):
        if target not in metas:
            metas[target] = MetaStore(
                target, memory_path=store.memory_path,
                user_path=store.user_path)
        return metas[target]
    mod.LocalStore = lambda *a, **k: store
    mod.MetaStore = meta_for
    return store, metas, meta_for


def _plain_case(mod, root: Path, tag: str, q: str,
                rule: str, force_gate: Any = None):
    store, metas, meta_for = _new_provider(mod, root, tag)
    fake = FakeRecall([{
        "id": "c-rule", "content": rule, "dense_score": 0.95,
        "keyword_score": 1, "fts_score": 0}])
    mod.ColdStoreClient = lambda *a, **k: fake
    provider = mod.MemoryCorePrefetchProvider()
    provider._hot_norm = ""
    provider._injected_ids = set()
    old_gate = mod.MemoryCorePrefetchProvider.__dict__[
        "_is_low_information_query"]
    if force_gate is not None:
        # 关闸对照: 同时覆盖 _recall_sync 与 _preprocess_query 的类级入口。
        mod.MemoryCorePrefetchProvider._is_low_information_query = \
            staticmethod(lambda _q: force_gate)
    try:
        gate = provider._is_low_information_query(q)
        out = provider.prefetch(q)
    finally:
        mod.MemoryCorePrefetchProvider._is_low_information_query = old_gate
    return {
        "query": q,
        "gate": gate,
        "injected": rule in out,
        "cold_rpc": len(fake.queries),
    }


def _action_case(mod, root: Path, q: str):
    full = "对外发布前必须通知用户并确认回滚点。"
    store, metas, _ = _new_provider(mod, root, "C")
    ms = metas.setdefault("memory", MetaStore(
        "memory", memory_path=store.memory_path,
        user_path=store.user_path))
    stub = _make_stub(full)
    store.add("memory", stub)
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="c1",
             handle="#h1", weight=0.5)
    fake = FakeRecall([{
        "id": "c1", "content": full, "dense_score": 0.95,
        "keyword_score": 1, "fts_score": 0}])
    mod.ColdStoreClient = lambda *a, **k: fake
    provider = mod.MemoryCorePrefetchProvider()
    provider._hot_norm = ""
    provider.sync_turn(q, "assistant")
    provider.on_turn_start(1, "assistant only", tool_count=0)
    return {
        "query": q,
        "action_trigger": provider._action_trigger_hit(q),
        "pending_gate": provider._is_low_information_query(
            provider._pending_action_query or ""),
        "cold_rpc": len(fake.queries),
        "writeback": full in store.entries("memory"),
        "stub_gone": stub not in store.entries("memory"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()
    mod = _load_plugin()
    tmp = Path(tempfile.mkdtemp(prefix="fix4_p0_"))
    out: Dict[str, Any] = {"groups": {}}

    rows_a = []
    for i, q in enumerate(A_QUERIES):
        rule = f"{q[:24]} 对应的真实规则"
        row = _plain_case(mod, tmp, f"A{i}", q, rule)
        row["gate_off_injected"] = _plain_case(
            mod, tmp, f"A{i}_off", q, rule, force_gate=False)["injected"]
        row["case_id"] = i + 1
        row["group"] = "A_legit_topic_words"
        rows_a.append(row)
    rows_b = []
    for i, q in enumerate(B_QUERIES):
        rule = f"snapshot rule {i}: {q[:20]}"
        row = _plain_case(mod, tmp, f"B{i}", q, rule)
        row["gate_off_injected"] = _plain_case(
            mod, tmp, f"B{i}_off", q, rule, force_gate=False)["injected"]
        row["case_id"] = i + 1
        row["group"] = "B_real_snapshot_rule"
        rows_b.append(row)
    rows_c = []
    for i, q in enumerate(C_QUERIES):
        row = _action_case(mod, tmp, q)
        row["case_id"] = i + 1
        row["group"] = "C_action_consensus"
        rows_c.append(row)
    rows_n = []
    noise_rule = "冷层记忆内容：用户要求每次交付后必须通知。"
    for i, q in enumerate(NOISE_QUERIES):
        row = _plain_case(mod, tmp, f"N{i}", q, noise_rule)
        row["gate_off_injected"] = _plain_case(
            mod, tmp, f"N{i}_off", q, noise_rule,
            force_gate=False)["injected"]
        row["case_id"] = i + 1
        row["group"] = "noise_must_not_inject"
        rows_n.append(row)

    out["groups"]["A"] = rows_a
    out["groups"]["B"] = rows_b
    out["groups"]["C"] = rows_c
    out["groups"]["noise"] = rows_n
    out["summary"] = {
        "A_count": len(rows_a),
        "A_gate": sum(r["gate"] for r in rows_a),
        "A_injected": sum(r["injected"] for r in rows_a),
        "B_count": len(rows_b),
        "B_gate": sum(r["gate"] for r in rows_b),
        "B_injected": sum(r["injected"] for r in rows_b),
        "C_count": len(rows_c),
        "C_pending_gate": sum(r["pending_gate"] for r in rows_c),
        "C_writeback": sum(r["writeback"] for r in rows_c),
        "noise_count": len(rows_n),
        "noise_gate": sum(r["gate"] for r in rows_n),
        "noise_injected": sum(r["injected"] for r in rows_n),
    }
    out["pass"] = bool(
        out["summary"]["A_gate"] == 0
        and out["summary"]["A_injected"] == len(rows_a)
        and out["summary"]["B_gate"] == 0
        and out["summary"]["B_injected"] == len(rows_b)
        and out["summary"]["C_pending_gate"] == 0
        and out["summary"]["C_writeback"] == len(rows_c)
        and out["summary"]["noise_gate"] == len(rows_n)
        and out["summary"]["noise_injected"] == 0
    )
    text = json.dumps(out, ensure_ascii=False, indent=1)
    print(text)
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print("[fix4-probes] A gate=%d injected=%d | B gate=%d injected=%d | "
          "C pending_gate=%d writeback=%d | noise gate=%d injected=%d | "
          "pass=%s" % (
              out["summary"]["A_gate"], out["summary"]["A_injected"],
              out["summary"]["B_gate"], out["summary"]["B_injected"],
              out["summary"]["C_pending_gate"], out["summary"]["C_writeback"],
              out["summary"]["noise_gate"], out["summary"]["noise_injected"],
              out["pass"]))
    return 0 if out["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
