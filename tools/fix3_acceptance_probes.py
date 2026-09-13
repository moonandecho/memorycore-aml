#!/usr/bin/env python3
"""fix3_acceptance_probes.py — F1/F4 主动攻击复跑 (评审 §5.2/§5.3 同源输入)。

只读临时目录; 逐条输出 JSON, 任一条不合格 exit 1。
  F1: 15 条低信息/噪声/闲聊/无关技术查询 → 全文注入必须 0;
  F4-1: assistant 文本含"写" → on_turn_start 不得触发/不得写回;
  F4-2: query='系统' + H 主题匹配 + dense=0.30 → 不得写回全文。

用法:
  .venv/bin/python tools/fix3_acceptance_probes.py --json-out /tmp/fix3_probes.json
"""
from __future__ import annotations

import argparse
import importlib.util
import os
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
_hp = os.environ.get("HERMES_AGENT_DIR")
if _hp and Path(_hp).exists():
    sys.path.insert(0, _hp)

from memorycore.core.metadata import MetaStore  # noqa: E402
from memorycore.core.overflow import _make_stub  # noqa: E402
from memorycore.local_store import LocalStore  # noqa: E402

PLUGIN_PATH = Path(os.environ.get(
    "MEMORYCORE_PLUGIN_PATH",
    str(ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")))



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
    spec = importlib.util.spec_from_file_location("fix3_probe_plugin",
                                                  PLUGIN_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeRecall:
    def __init__(self):
        self.results = []
        self.queries = []

    def recall_results(self, q, top_k=5, bump=True):
        self.queries.append((q, top_k, bump))
        return [dict(r) for r in self.results]


CASES = [
    ("闲聊", "今天天气真不错，晚上吃什么好呢"),
    ("确认", "好的，收到，明白了"),
    ("确认2", "嗯嗯，继续"),
    ("低信息", "在吗"),
    ("低信息2", "谢谢"),
    ("无关技术", "Python 的 GIL 是什么"),
    ("无关技术2", "怎么把 Excel 转成 CSV"),
    ("系统前缀噪声",
     "[IMPORTANT: You have 1 unread message from system] 请继续"),
    ("系统前缀噪声2", "[ASYNC DELEGATION] background task done"),
    ("闲聊带动作词1", "帮我写一首关于春天的诗"),
    ("闲聊带动作词2", "你刚才回复得不错"),
    ("闲聊带动作词3", "我想更新一下头像"),
    ("闲聊带动作词4", "周末打算去爬山，顺便拍点照片"),
    ("确认带动作词", "好的，那就按你说的写吧"),
    ("无关技术带动作词", "怎么安装 Python 包"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--json-out", type=Path, default=None)
    args = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="fix3_probes_"))
    mod = _load_plugin()
    fake = _FakeRecall()
    result = {"f1": [], "f4_assistant": {}, "f4_low_info_h": {}}
    failures = []

    def _provider(store):
        mod.LocalStore = lambda *a, **k: store
        mod.MetaStore = lambda target, **kw: MetaStore(
            target, memory_path=store.memory_path, user_path=store.user_path)
        mod.ColdStoreClient = lambda *a, **k: fake
        p = mod.MemoryCorePrefetchProvider()
        p._hot_norm = ""
        p._injected_ids = set()
        return p

    for tag, q in CASES:
        store = LocalStore(tmp / ("A_%d_M.md" % len(result["f1"])),
                           tmp / ("A_%d_U.md" % len(result["f1"])))
        p = _provider(store)
        fake.results = [{
            "id": "cA",
            "content": "冷层记忆内容：用户要求每次交付后必须通知。",
            "dense_score": 0.95, "keyword_score": 0, "fts_score": 0,
        }]
        out = p.prefetch(q)
        injected = "冷层记忆内容" in out
        row = {"tag": tag, "query": q, "gate": p._is_low_information_query(q),
               "injected": injected}
        result["f1"].append(row)
        if injected:
            failures.append(row)

    # F4-1: assistant 含写, 已在热层的 stub 不得被全文写回。
    store = LocalStore(tmp / "B1_M.md", tmp / "B1_U.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    full = "红线规则: 对外发布前必须通知用户并确认。"
    stub = _make_stub(full)
    store.add("memory", stub)
    ms.stamp(stub, "stub", origin="stub_sink", cold_id="cB1",
             handle="#hB1", weight=0.5)
    p = _provider(store)
    fake.results = [{"id": "cB1", "content": full, "dense_score": 0.95,
                     "keyword_score": 0, "fts_score": 0}]
    p.sync_turn("帮我写一首关于春天的诗", "好的，我这就来写")
    p.on_turn_start(1, "好的，我这就来写", tool_count=0)
    result["f4_assistant"] = {
        "pending_action_query": p._pending_action_query,
        "full_restored_to_hot": full in store.entries("memory"),
        "stub_still_hot": stub in store.entries("memory"),
    }
    if result["f4_assistant"]["full_restored_to_hot"]:
        failures.append(result["f4_assistant"])

    # F4-2: 低信息 H 通道不得写回。
    store = LocalStore(tmp / "H_M.md", tmp / "H_U.md")
    ms = MetaStore("memory", memory_path=store.memory_path,
                   user_path=store.user_path)
    full_h = "系统配置必须使用国内镜像源, 发布前通知用户。"
    stub_h = _make_stub("系统配置必须使用国内镜像源。")
    store.add("memory", stub_h)
    ms.stamp(stub_h, "stub", origin="stub_sink", cold_id="cH",
             handle="#hH", weight=0.5)
    p = _provider(store)
    fake.results = [{"id": "cH", "content": full_h, "dense_score": 0.30,
                     "keyword_score": 0, "fts_score": 0}]
    out = p._recall_sync("系统")
    result["f4_low_info_h"] = {
        "context": out,
        "cold_rpc_calls": len(fake.queries),
        "full_in_hot": full_h in store.entries("memory"),
        "stub_still_hot": stub_h in store.entries("memory"),
    }
    if result["f4_low_info_h"]["full_in_hot"]:
        failures.append(result["f4_low_info_h"])

    result["pass"] = not failures
    text = json.dumps(result, ensure_ascii=False, indent=1)
    print(text)
    if args.json_out:
        args.json_out.write_text(text + "\n", encoding="utf-8")
    print("[fix3-probes] F1 injected=%d/%d  F4 assistant_writeback=%s  "
          "F4 H_writeback=%s  pass=%s" % (
              sum(1 for r in result["f1"] if r["injected"]),
              len(result["f1"]),
              result["f4_assistant"]["full_restored_to_hot"],
              result["f4_low_info_h"]["full_in_hot"],
              result["pass"]))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
