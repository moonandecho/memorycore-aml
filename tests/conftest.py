#!/usr/bin/env python3
"""tests/conftest.py — 测试隔离基础设施 (tempfile + mock, 绝不碰生产数据)。

跑法: cd <repo> && .venv/bin/python -m pytest tests/ -v
(必须用 memorycore 自己的 venv: mcp 2.2.0, 走官方 mcp.server.mcpserver
 的 MCPServer 高级 API, 不依赖第三方 fastmcp;
 hermes-agent venv 是 mcp 2.0.0, 两边版本不同, 勿混用)
"""
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Release test isolation (hard requirement): never read/write production
# ~/.hermes or ~/.memorycore.  Point hot-tier and local-cold SQLite at a
# per-session temp runtime before any memorycore import binds paths.
_RUNTIME_TMP = Path(tempfile.mkdtemp(prefix="origin-memorycore-test-runtime-"))
os.environ.setdefault("MEMORY_DIR", str(_RUNTIME_TMP / "memories"))
os.environ.setdefault("MNEMOSYNE_DATA_DIR", str(_RUNTIME_TMP / "mnemosyne"))

# Plugin tests load hermes-plugin/memorycore-prefetch; it depends on the
# Hermes host runtime (agent.memory_provider).  Prefer an explicitly
# provided real runtime via HERMES_AGENT_DIR; otherwise inject a minimal
# read-only mock so plugin tests are self-contained (production plugin
# still imports the real Hermes runtime).
PLUGIN_PATH = Path(os.environ.get(
    "MEMORYCORE_PLUGIN_PATH",
    str(REPO_ROOT / "hermes-plugin" / "memorycore-prefetch" / "__init__.py")))

_hermes_candidates = [os.environ.get("HERMES_AGENT_DIR")]
_hermes_found = False
for _hp in _hermes_candidates:
    if _hp and Path(_hp).is_dir():
        sys.path.insert(0, _hp)
        _hermes_found = True
        break
if not _hermes_found and "agent.memory_provider" not in sys.modules:
    import re as _re
    import types as _types
    _agent_pkg = _types.ModuleType("agent")
    _agent_pkg.__path__ = []  # type: ignore[attr-defined]
    _provider_mod = _types.ModuleType("agent.memory_provider")

    class _MemoryProvider:  # minimal local test double
        def __init__(self, *args, **kwargs):
            pass

    _TRIVIAL_PROMPT_RE = _re.compile(
        r"^(?:yes|no|ok|okay|sure|thanks|hi|hey|hello|continue|"
        "got it|done|next|lgtm|k)[\\s!?.:;,\"']*$",
        _re.IGNORECASE,
    )

    def _is_trivial_prompt(text):
        q = (text or "").strip()
        if not q or q.startswith("/"):
            return True
        return bool(_TRIVIAL_PROMPT_RE.match(q))

    _provider_mod.MemoryProvider = _MemoryProvider
    _provider_mod.TRIVIAL_PROMPT_RE = _TRIVIAL_PROMPT_RE
    _provider_mod.is_trivial_prompt = _is_trivial_prompt
    sys.modules.setdefault("agent", _agent_pkg)
    sys.modules["agent.memory_provider"] = _provider_mod

from memorycore.local_store import LocalStore  # noqa: E402
from memorycore.core.metadata import MetaStore  # noqa: E402


def days_ago_str(n: int) -> str:
    """n 天前的日期字符串 (内嵌日期测试锚点)。"""
    from datetime import datetime, timedelta
    return (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")


class MockMnemosyneClient:
    """mock 冷层: 记录调用; 可注入冷层已有条目与故障模式。"""

    def __init__(self, cold_items=None, fail_remember=False, fail_recall=False):
        self._cold = list(cold_items or [])
        self._fail_remember = fail_remember
        self._fail_recall = fail_recall
        self.recall_queries = []
        self.stored = []
        self.updated = []
        self.forgotten = []
        self._next_id = 0

    def recall_results(self, query, top_k=5, bump=True):
        if self._fail_recall:
            raise RuntimeError("cold unreachable (mock)")
        self.recall_queries.append(query)
        self.recall_bumps = getattr(self, "recall_bumps", []) + [bump]
        return [{"id": f"c{i}", "content": c.get("content"),
                 "dense_score": c.get("dense_score", 0.0)}
                for i, c in enumerate(self._cold)]

    def remember(self, content, importance=0.6, scope="global"):
        if self._fail_remember:
            return {"status": "error", "error": "cold write failed (mock)"}
        self._next_id += 1
        self.stored.append(content)
        self._cold.append({"content": content, "dense_score": 0.5})
        return {"status": "stored", "memory_id": f"m{self._next_id}"}

    def update(self, memory_id, content, importance=None):
        self.updated.append((memory_id, content))
        return {"status": "updated"}

    def forget(self, memory_id):
        self.forgotten.append(memory_id)
        return {"status": "ok"}

    def stats(self):
        return {"total": len(self._cold)}

    def embed_texts(self, texts):
        """默认模拟 embedding 不可用 (返回 None → 调用方降级纯词法)。

        测试需要语义命中时 monkeypatch 此方法返回固定向量。
        """
        return None


@pytest.fixture
def tmp_store(tmp_path):
    """临时目录 LocalStore (MEMORY.md / USER.md 双文件, 隔离生产)。"""
    return LocalStore(tmp_path / "MEMORY.md", tmp_path / "USER.md")


@pytest.fixture
def meta_for(tmp_store):
    """按 target 构造跟随 tmp_store 路径的 MetaStore。"""
    def _make(target):
        return MetaStore(target, memory_path=tmp_store.memory_path,
                         user_path=tmp_store.user_path)
    return _make


@pytest.fixture
def mock_client():
    return MockMnemosyneClient()


@pytest.fixture(autouse=True)
def _isolate_llm_rot(tmp_path, monkeypatch):
    """隔离 LLM 轮转游标状态 (2026-09-12, 终审低危 D2 饿死修复)。

    测试绝不读写生产 ~/.hermes/memorycore/llm_rot.json (轮转偏移逐用例
    复位, 不串扰生产轮转进度)。
    """
    from memorycore.core import llm_rot
    monkeypatch.setattr(llm_rot, "ROT_PATH", tmp_path / "llm_rot.json")


@pytest.fixture(autouse=True)
def _isolate_llm_config(tmp_path, monkeypatch):
    """隔离 LLM 配置 (2026-09-12, Phase 1): 测试绝不读生产 ~/.hermes/.env /
    config.yaml, 绝不外呼真实端点。

    文件来源关闭 + LLM env 清空 + ENV_FILE/CONFIG_YAML 指向 tmp 路径,
    resolve 缓存清空 (防跨用例串扰)。
    """
    from memorycore.core import llm_config
    monkeypatch.setenv("MEMCORE_LLM_FILE_SOURCES", "0")
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("MEMCORE_LLM_ENABLED", raising=False)
    monkeypatch.setattr(llm_config, "ENV_FILE", tmp_path / "hermes.env")
    monkeypatch.setattr(llm_config, "CONFIG_YAML", tmp_path / "config.yaml")
    llm_config.invalidate_cache()


@pytest.fixture(autouse=True)
def _isolate_activity(tmp_path, monkeypatch):
    """隔离 activity.jsonl — 默认空文件 (绝不读生产日志)。

    Phase 4 活性信号 (apply_activity_hits / enforce) 依赖 activity 查询;
    测试无查询 = 词法不活跃, 行为确定。需要查询的测试自行 monkeypatch
    ACTIVITY_LOG_FILE + log_activity_query。
    """
    from memorycore.core import config as config_mod
    from memorycore.core import metadata as meta_mod
    monkeypatch.setattr(meta_mod, "ACTIVITY_LOG_FILE", tmp_path / "activity.jsonl")
    # E8: ACTIVITY_LOG_ENABLED 消费方经 _cfg 委托 config; patch 目标改 config
    monkeypatch.setattr(config_mod, "ACTIVITY_LOG_ENABLED", True)
