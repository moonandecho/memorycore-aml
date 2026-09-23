#!/usr/bin/env python3
"""aml_server.py — AML (Agent Memory Leaderboard) HTTP adapter for MemoryCore

差异化路线：记忆治理在线化（ActiveMemoryIndex 式路线，MemoryCore 自己的治理机制）。

HTTP endpoints (plain REST, mounted on the MCP server streamable-http app):
  POST /add     AML write: messages → fact fragments → stale filter →
                semantic dedup/merge → cold tier (author_id = user_id)
  POST /search  AML recall: author-scoped recall → decay ranking → AML format
  GET  /health  liveness (2xx)

Isolation (hard constraint): user_id maps 1:1 to the cold tier's author_id.
Every write and every recall carries author_id; a user can never see another
user's memories (enforced in SQL by the cold tier).

Auth: optional via env AML_API_KEY. Unset → open (AML smoke mode).
  Accepted schemes: "Bearer <key>" / "Token <key>" in Authorization,
  or raw key in X-Api-Key.

Env:
  MNEMOSYNE_DATA_DIR    — dedicated SQLite dir (isolate from production data)
  MEMORYCORE_EMBED_URL  — embedding API base (default ollama localhost)
  MEMORYCORE_EMBED_MODEL— embedding model (default qwen3-embedding:0.6b)
  AML_API_KEY           — optional shared key for Add/Search
"""
import asyncio
import hashlib
import json
import logging
import math
import os
import re
import tempfile
import secrets
import sqlite3
import contextlib
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

# Capture the caller's environment before importing core.config, which still
# fills in a legacy ~/.memorycore/data default for backwards compatibility.
# AML must never silently write to a directory the operator did not choose.
_PROCESS_MNEMOSYNE_DATA_DIR_WAS_SET = "MNEMOSYNE_DATA_DIR" in os.environ

try:
    from mcp.server.mcpserver import MCPServer  # noqa: E402  # mcp 2.x 官方高级 API (替代第三方 fastmcp)
except ImportError as _e:
    # 依赖前置检查: 把 mcp 大版本变更变成可读中文提示, 不裸抛 traceback (任务 E)
    import importlib.metadata as _imd
    import sys as _sys
    try:
        _mcp_ver = _imd.version("mcp")
    except Exception:
        _mcp_ver = "未知"
    _sys.stderr.write(
        f"[memorycore] 启动失败: 当前 mcp 版本 {_mcp_ver} 不含 MCPServer (属 mcp 大版本变更)。\n"
        "[memorycore] 建议: 用独立 venv 安装 memorycore; 或 pip install \"mcp>=2,<3\"; "
        "不要与其它工具 (如 Hermes) 共用同一个环境。\n"
        f"[memorycore] 原始错误: {_e}\n"
    )
    _sys.exit(1)

from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from .cold_store_client import ColdStoreClient, StorageBusyError  # noqa: E402
from .core.classifier import classify, STALE  # noqa: E402
from .core.overflow import _find_best_match, _merge_two_entries  # noqa: E402
from .core.decay import _apply_decay  # noqa: E402
from .core.config import COLD_SOFT_LIMIT, COLD_HARD_LIMIT  # noqa: E402
from . import __version__ as SERVICE_VERSION  # noqa: E402

# ---- AML write-side constants (governance online) -------------------------

_FACT_IMPORTANCE = 0.6     # 事实默认重要度: 不触发热关键词 → 冷层
_DEDUP_RECALL_TOP_K = 5    # 写入前查重召回数 (比 overflow 的 3 更宽, 候选池更全)
_MAX_FRAGMENT_CHARS = 300  # 长消息按句切分后的片段上限
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；;\n]")
_SENTENCE_SPLIT_KEEP_RE = re.compile(r"([。！？；;\n])")
_MAX_TOP_K = 100           # AML 协议固定 top_k 上限
# spec line 1428: "up to 10 MiB decoded per image and 30 MiB per Add request."
_MAX_IMAGE_DECODED_BYTES = 10 * 1024 * 1024
_MAX_TOTAL_IMAGE_DECODED_BYTES = 30 * 1024 * 1024
# Recall candidates must be wider than top_k so decay+truncation is honest.
_RECALL_CANDIDATE_POOL_MIN = 30
_SUPPORTED_IMAGE_MIMES = {"jpeg", "jpg", "png", "webp"}
_BASE64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")

# request_id idempotency ledger bounds (persistent SQLite, restart-safe)
_LEDGER_MAX_ROWS = 5000
_LEDGER_TTL_SECONDS = 30 * 24 * 3600
_LEDGER_INFLIGHT_STALE_SECONDS = 300
_LEDGER_LOCK = threading.Lock()


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_float(name: str, default: float, *, minimum: float = 0.0) -> float:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(minimum, value)


def _env_switch(name: str, default: str = "0") -> bool:
    """Truthy env switch: 1/true/yes/on (case-insensitive); anything else off."""
    raw = os.environ.get(name, default)
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


# Row-level speaker label (opt-in, default off).
# 平台 Add 请求本就带 role(user/assistant)，但落库行文本只保留了日期前缀，
# 说话人归属在写入时被丢弃。打开后行前缀追加 "user: " / "assistant: "，
# 不合并、不拆片、不改行粒度；关闭时逐字节等同旧行为
# （tests/test_row_role_label.py 断言）。
_ROW_ROLE_LABEL = _env_switch("AML_ROW_ROLE_LABEL")


# P0 concurrency / capacity knobs (all env-configurable; defaults chosen for
# the 64-Add platform shape and the MX150 GPU model).
AML_WORKERS = _env_int("AML_WORKERS", 4, minimum=1)
AML_EMBED_CONCURRENCY = _env_int("AML_EMBED_CONCURRENCY", 4, minimum=1)
AML_EMBED_BATCH_SIZE = _env_int("AML_EMBED_BATCH_SIZE", 16, minimum=1)
AML_REQUEST_BUDGET_S = _env_float("AML_REQUEST_BUDGET_S", 600.0, minimum=0.1)
AML_MAX_INFLIGHT = _env_int("AML_MAX_INFLIGHT", 128, minimum=1)
AML_MAX_QUEUE = _env_int("AML_MAX_QUEUE", 256, minimum=0)
AML_MAX_INFLIGHT_BYTES = _env_int(
    "AML_MAX_INFLIGHT_BYTES", 256 * 1024 * 1024, minimum=1)

# L3(2026-09-19): 召回覆盖实验开关 —— 默认值与现状完全一致，改由 env 开启。
# 动机：实测 LoCoMo 拒答题里 88% 是"该证据根本没被召回"（覆盖问题），
# 而 pool_k 此前被 _MAX_TOP_K 卡在 100，等于按存储层排序取前 100，没有二次挑选空间。
_RECALL_POOL_MULT = _env_int("AML_RECALL_POOL_MULT", 1, minimum=1)
_RECALL_POOL_CAP = _env_int("AML_RECALL_POOL_CAP", 500, minimum=100)
_RECALL_MULTI_QUERY = min(1, _env_int("AML_RECALL_MULTI_QUERY", 0, minimum=0))

# E1(2026-09-19): 跨查询路由 RRF 融合。默认 off = 现状（多路并集 + decay），
# 仅显式 AML_RECALL_FUSION=rrf 时启用；未知值一律 off（fail-safe，不引入隐式行为）。
_RECALL_FUSION_MODE = os.environ.get("AML_RECALL_FUSION", "off").strip().lower()
_RECALL_FUSION_ON = _RECALL_FUSION_MODE == "rrf"
_RRF_K = _env_int("AML_RRF_K", 60, minimum=1)
_RECALL_FUSION_MIN_DENSE = _env_float(
    "AML_RECALL_FUSION_MIN_DENSE", 0.3, minimum=0.0)

# E3(2026-09-19): 字面/关键词信号重排 opt-in。默认 off = 现状；仅显式真值开启。
# 权重默认按「字面优先、dense 保底」给：keyword=0.4 / fts=0.4 / dense=0.2。
# 归一化可用 AML_RERANK_NORM=minmax|rank 选择（默认 minmax）。
_RERANK_LEXICAL = os.environ.get(
    "AML_RERANK_LEXICAL", "0").strip().lower() in ("1", "true", "yes", "on")
_RERANK_W_DENSE = _env_float("AML_RERANK_W_DENSE", 0.2, minimum=0.0)
_RERANK_W_KEYWORD = _env_float("AML_RERANK_W_KEYWORD", 0.4, minimum=0.0)
_RERANK_W_FTS = _env_float("AML_RERANK_W_FTS", 0.4, minimum=0.0)
_RERANK_NORM = os.environ.get("AML_RERANK_NORM", "minmax").strip().lower()
if _RERANK_NORM not in ("minmax", "rank"):
    _RERANK_NORM = "minmax"
# E3 字面 token 扩路：多跳问题常因多 token 相关性门禁而整段漏召；
# E3 开启时用查询中的实词分别召回（最多 N 路），只对 opt-in 流量生效。
_RERANK_TOKEN_ROUTES = min(
    _env_int("AML_RERANK_TOKEN_ROUTES", 5, minimum=0), 5)
_RERANK_POOL_MIN_MULT = 2   # E3 开启时默认深挖到 2×top_k（仍受 _RECALL_POOL_CAP 约束）
_RERANK_TOKEN_POOL_K = _env_int("AML_RERANK_TOKEN_POOL_K", 150, minimum=1)
# C③: 单 token 扩路是覆盖补充，不是主路替代。所有 token 路的 RRF 权重
# 之和上限不得超过主路权重的该倍数，避免同一候选出现在多条窄 token 路时
# 叠加压过高 dense 主路证据。默认 0.5 = 最多只能接近但压不过主路 rank1。
_RERANK_TOKEN_RRF_TOTAL_MULT = min(
    _env_float("AML_RERANK_TOKEN_RRF_TOTAL_MULT", 0.5, minimum=0.0), 1.0)
_RECALL_STOPWORDS = frozenset(
    "a an the is are was were be been do does did what when where which who"
    " whom whose why how many much of in on at to for with about and or but if"
    " it its this that these those i you he she they we me my your his her their"
    " s t".split())


def _keyword_query(query: str) -> str:
    """L3: 去掉疑问词/停用词后的实词查询，用于多路召回。"""
    toks = [t for t in re.findall(r"[A-Za-z0-9']+", (query or "").lower())
            if len(t) > 2 and t not in _RECALL_STOPWORDS]
    return " ".join(toks)


def _literal_token_queries(query: str) -> List[str]:
    """E3: 查询中的实词 token（保序去重），用于单 token 扩路。"""
    out: List[str] = []
    seen = set()
    for tok in re.findall(r"[A-Za-z0-9']+", (query or "").lower()):
        if len(tok) <= 2 or tok in _RECALL_STOPWORDS or tok in seen:
            continue
        seen.add(tok)
        out.append(tok)
    return out


def _select_literal_token_queries(query: str, limit: int) -> List[str]:
    """E3: 从查询实词里选最多 ``limit`` 个 token 作为独立召回路。

    选择方式为均匀位置采样（首/中/尾优先），避免只取队列前几个 token
    总落在专名上；token 数量 ≤ limit 时全部使用。
    """
    tokens = _literal_token_queries(query)
    if limit <= 0 or not tokens:
        return []
    if len(tokens) <= limit:
        return tokens
    # 长词通常更具体、候选面更窄；按长度降序取前 limit 个，
    # 再交给 1/候选数 加权的 RRF（单 token 窄路权重更高）。
    return sorted(tokens, key=lambda t: -len(t))[:limit]
# Spec §1428 caps decoded image bytes at 30 MiB/Add.  Base64 overhead is
# 4/3 plus the data-URI/JSON envelope, so the raw HTTP cap must be wider than
# 30 MiB or a legal boundary image would be rejected by 413 (review §2.6).
AML_MAX_BODY_BYTES = _env_int(
    "AML_MAX_BODY_BYTES", 48 * 1024 * 1024, minimum=1)
# AML response hard guard (review condition B).  Spec allows 30 MiB per
# Search response; 28 MiB leaves 2 MiB safety for JSON envelope/headers and
# future fields.  This is a *body* byte budget, enforced at assembly time.
_SEARCH_RESPONSE_MAX_BYTES = _env_int(
    "AML_SEARCH_RESPONSE_MAX_BYTES", 28 * 1024 * 1024, minimum=1024)
AML_HEALTH_PROBE_INTERVAL_S = _env_float(
    "AML_HEALTH_PROBE_INTERVAL_S", 5.0, minimum=0.2)
AML_STARTUP_SELF_CHECK = os.environ.get(
    "AML_STARTUP_SELF_CHECK", "1").strip().lower() not in (
        "0", "false", "no", "off")

_EMBED_GATE = threading.BoundedSemaphore(AML_EMBED_CONCURRENCY)

_EXECUTOR: Optional[ThreadPoolExecutor] = None
_EXECUTOR_LOCK = threading.Lock()


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    with _EXECUTOR_LOCK:
        if _EXECUTOR is None:
            _EXECUTOR = ThreadPoolExecutor(
                max_workers=AML_WORKERS, thread_name_prefix="aml-worker")
        return _EXECUTOR


class _BudgetExceeded(RuntimeError):
    """Raised by the cooperative deadline checks before any write starts."""


class _EmbeddingUnavailable(RuntimeError):
    """Raised when the pre-write warm-up proves the embedding API is unusable."""


# Counters surfaced through /health; never used for response bodies.
_P0_LOCK = threading.Lock()
_P0_COUNTS: Dict[str, int] = {
    "backpressure_503": 0,
    "queue_timeout_503": 0,
    "budget_503": 0,
    "embedding_unavailable_503": 0,
    "storage_busy_503": 0,
    "body_too_large_413": 0,
    "search_response_truncated": 0,
    "health_probe_ok": 0,
    "health_probe_error": 0,
}


def _p0_inc(name: str, delta: int = 1) -> None:
    with _P0_LOCK:
        _P0_COUNTS[name] = int(_P0_COUNTS.get(name, 0)) + int(delta)


def _p0_snapshot() -> Dict[str, int]:
    with _P0_LOCK:
        return dict(_P0_COUNTS)

_MNEMOSYNE_DATA_DIR_REQUIRED_MSG = (
    "MNEMOSYNE_DATA_DIR must be explicitly set; "
    "必须显式设置 MNEMOSYNE_DATA_DIR（拒绝回退到 ~/.memorycore/data）"
)

# In-process governance observability (spec §06 business errors vs policy
# filtering).  Policy outcomes are not exposed in the Add response; they are
# logged and aggregated for /health.
_GOVERNANCE_LOCK = threading.Lock()
_GOVERNANCE_COUNTS: Dict[str, int] = {
    "requests": 0,
    "fragments": 0,
    "stored": 0,
    "updated": 0,
    "duplicate": 0,
    "stale": 0,
    "filtered": 0,
    "error": 0,
    "textless_requests": 0,
    "image_parts": 0,
    "oversize_image_parts": 0,
    "aggregate_image_parts": 0,
    "invalid_image_parts": 0,
}

logger = logging.getLogger("memorycore.aml")

AML_API_KEY = os.environ.get("AML_API_KEY", "").strip()

mcp = MCPServer("memorycore-aml")

# Lazy singleton: LocalBackend init probes the embedding API and can be
# slow/failing at import time — /health must answer even when the embedding
# service is down (the service is alive, storage is degraded).
_client: Optional[ColdStoreClient] = None
_client_error: Optional[str] = None
_client_lock = threading.Lock()


def _require_data_dir() -> str:
    """Return MNEMOSYNE_DATA_DIR or fail explicitly.

    T3 / fix-round2: no silent fallback to ~/.memorycore/data.  The check is
    captured at import time so core.config's backwards-compatible default
    cannot mask a missing operator setting.
    """
    if not _PROCESS_MNEMOSYNE_DATA_DIR_WAS_SET:
        raise RuntimeError(_MNEMOSYNE_DATA_DIR_REQUIRED_MSG)
    data_dir = (os.environ.get("MNEMOSYNE_DATA_DIR") or "").strip()
    if not data_dir:
        raise RuntimeError(_MNEMOSYNE_DATA_DIR_REQUIRED_MSG)
    return data_dir


def _validate_data_dir() -> str:
    """Fail closed unless MNEMOSYNE_DATA_DIR is creatable and truly writable.

    G4 / fix-round3a: an os.access() check is not sufficient (root, read-only
    mounts and /proc pseudo paths can lie).  Perform a real create + write +
    fsync + unlink probe before the HTTP listener is allowed to start.
    """
    data_dir = _require_data_dir()
    try:
        os.makedirs(data_dir, exist_ok=True)
    except Exception as e:
        raise RuntimeError(
            f"MNEMOSYNE_DATA_DIR not creatable: {data_dir!r} ({e})"
        ) from e
    if not os.path.isdir(data_dir):
        raise RuntimeError(
            f"MNEMOSYNE_DATA_DIR is not a directory: {data_dir!r}"
        )

    probe_path = None
    try:
        fd, probe_path = tempfile.mkstemp(
            prefix=".aml_write_probe_", dir=data_dir)
        with os.fdopen(fd, "wb") as f:
            f.write(b"memorycore-aml-write-probe")
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        raise RuntimeError(
            f"MNEMOSYNE_DATA_DIR not writable: {data_dir!r} ({e})"
        ) from e
    finally:
        if probe_path:
            try:
                os.unlink(probe_path)
            except OSError:
                pass
    return data_dir


def _get_client() -> Optional[ColdStoreClient]:
    global _client, _client_error
    with _client_lock:
        if _client is None:
            try:
                _validate_data_dir()
                _client = ColdStoreClient()
                _client_error = None
            except Exception as e:  # embedding unreachable etc.
                _client_error = str(e)
                return None
        # P0.5 identity guard: recover/rebuild if the data dir/DB was replaced.
        checker = getattr(_client, "check_data_identity", None)
        if callable(checker):
            try:
                checker()
            except Exception as e:
                _client_error = f"data directory identity recovery failed: {e}"
                return None
        return _client


# ---- P0 admission / deadline / health snapshot -----------------------------
#
# The event loop only does protocol parsing and admission.  All blocking work
# runs in a bounded ThreadPoolExecutor.  Health never touches _get_client(),
# SQLite, or subprocesses; it only reads the snapshot maintained by a separate
# daemon probe thread.

class _AdmissionController:
    """Byte-weighted in-flight/queue limiter for business requests.

    Health is deliberately not a client of this controller.  Queue waits are
    part of the caller's request deadline, so a request cannot queue forever
    and then still get a full processing budget.
    """

    def __init__(self, max_inflight: int, max_queue: int, max_bytes: int):
        self.max_inflight = max(1, int(max_inflight))
        self.max_queue = max(0, int(max_queue))
        self.max_bytes = max(1, int(max_bytes))
        self._lock = asyncio.Lock()
        self._cond = asyncio.Condition(self._lock)
        self.inflight = 0
        self.queued = 0
        self.inflight_bytes = 0

    def _fits(self, nbytes: int) -> bool:
        return (self.inflight < self.max_inflight
                and self.inflight_bytes + max(0, nbytes) <= self.max_bytes)

    async def acquire(self, nbytes: int, deadline: float):
        """Return (ok, reason). reason is overload/budget/queue_timeout."""
        nbytes = max(0, int(nbytes))
        waited = False
        async with self._cond:
            while True:
                now = time.monotonic()
                if now >= deadline:
                    return False, ("queue_timeout" if waited else "budget")
                if self._fits(nbytes):
                    self.inflight += 1
                    self.inflight_bytes += nbytes
                    return True, ""
                if self.queued >= self.max_queue:
                    return False, "overload"
                self.queued += 1
                waited = True
                try:
                    remaining = max(0.01, deadline - time.monotonic())
                    await asyncio.wait_for(
                        self._cond.wait(),
                        timeout=min(0.25, remaining))
                except asyncio.TimeoutError:
                    pass
                finally:
                    self.queued -= 1
                    # Wake one waiter in case acquire capacity changed.
                    self._cond.notify_all()

    async def release(self, nbytes: int) -> None:
        async with self._cond:
            self.inflight = max(0, self.inflight - 1)
            self.inflight_bytes = max(
                0, self.inflight_bytes - max(0, int(nbytes)))
            self._cond.notify_all()


_ADMISSION = _AdmissionController(
    AML_MAX_INFLIGHT, AML_MAX_QUEUE, AML_MAX_INFLIGHT_BYTES)


# Per-user serialisation: the storage engine is not re-entrant across a
# single user's plan/write/merge sequence even though individual SQL calls
# are serialised.  Different users still run in parallel.
_USER_LOCKS: Dict[str, threading.Lock] = {}
_USER_LOCKS_GUARD = threading.Lock()
_AML_MAX_USER_LOCKS = _env_int("AML_MAX_USER_LOCKS", 4096, minimum=32)


def _user_lock(user_id: str) -> threading.Lock:
    key = str(user_id or "")
    with _USER_LOCKS_GUARD:
        lock = _USER_LOCKS.pop(key, None)
        if lock is None:
            lock = threading.Lock()
        _USER_LOCKS[key] = lock  # refresh insertion order (LRU-ish)
        if len(_USER_LOCKS) > _AML_MAX_USER_LOCKS:
            for old_key, old_lock in list(_USER_LOCKS.items()):
                if old_key == key:
                    continue
                if not old_lock.locked():
                    del _USER_LOCKS[old_key]
                    break
        return lock


@contextlib.contextmanager
def _user_guard(user_id: str):
    with _user_lock(user_id):
        yield


def _remaining(deadline: float) -> float:
    return max(0.0, float(deadline) - time.monotonic())


def _check_deadline(deadline: float) -> None:
    if time.monotonic() > float(deadline):
        raise _BudgetExceeded(
            f"request budget exceeded ({AML_REQUEST_BUDGET_S:.0f}s)")


def _acquire_embed_gate(deadline: float) -> None:
    """Blocking embedding semaphore acquire with deadline awareness."""
    while True:
        _check_deadline(deadline)
        remaining = _remaining(deadline)
        if remaining <= 0:
            raise _BudgetExceeded("request budget exceeded while waiting for embed gate")
        if _EMBED_GATE.acquire(timeout=min(0.25, remaining)):
            return


def _release_embed_gate() -> None:
    try:
        _EMBED_GATE.release()
    except ValueError:
        pass


async def _run_in_worker(fn, *args):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_executor(), fn, *args)


class _BodyTooLarge(ValueError):
    """Raised when the streamed body exceeds the raw HTTP byte cap."""


async def _read_limited_body(request: Request, max_bytes: int) -> bytes:
    """Read request bytes without ever letting an unbounded body into memory."""
    chunks: List[bytes] = []
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > int(max_bytes):
            raise _BodyTooLarge(
                f"request body exceeds {int(max_bytes)} bytes")
        chunks.append(bytes(chunk))
    return b"".join(chunks)


async def _prepare_json_body(request: Request, deadline: float
                             ) -> Tuple[Optional[Dict[str, Any]],
                                        Optional[JSONResponse], int]:
    """Admit+read+parse a JSON body before any request.json()/decoded image parse.

    Review §2.6 / §4.7: the byte cap and admission must happen *before* the
    body is materialised and JSON-decoded.  Content-Length is used as the
    admission reservation when present; chunked bodies reserve the maximum
    raw cap and are then stream-counted.

    Returns (body, error_response, admitted_bytes).  On success the caller
    owns the admission slot and must release `admitted_bytes` in a finally.
    On error the slot has already been released (0 is returned).
    """
    raw_length = request.headers.get("content-length")
    declared: Optional[int] = None
    if raw_length is not None and str(raw_length).strip() != "":
        try:
            declared = int(str(raw_length).strip())
        except (TypeError, ValueError):
            return None, _bad(400, "invalid Content-Length"), 0
        if declared < 0:
            return None, _bad(400, "invalid Content-Length"), 0
        if declared > AML_MAX_BODY_BYTES:
            _p0_inc("body_too_large_413")
            return None, _bad(413, "request body too large"), 0

    reserved = declared if declared is not None else AML_MAX_BODY_BYTES
    admitted, reason = await _ADMISSION.acquire(reserved, deadline)
    if not admitted:
        if reason == "overload":
            _p0_inc("backpressure_503")
            return None, JSONResponse(
                {"detail": {"reason": "server overloaded; queue is full"}},
                status_code=503, headers={"Retry-After": "1"}), 0
        if reason == "queue_timeout":
            _p0_inc("queue_timeout_503")
            return None, JSONResponse(
                {"detail": {"reason": "request queue wait timed out"}},
                status_code=503, headers={"Retry-After": "1"}), 0
        _p0_inc("budget_503")
        return None, JSONResponse(
            {"detail": {"reason": "request budget exceeded while queued"}},
            status_code=503, headers={"Retry-After": "1"}), 0

    try:
        raw = await _read_limited_body(request, AML_MAX_BODY_BYTES)
    except _BodyTooLarge:
        await _ADMISSION.release(reserved)
        _p0_inc("body_too_large_413")
        return None, _bad(413, "request body too large"), 0
    except Exception:
        await _ADMISSION.release(reserved)
        return None, _bad(400, "invalid request body"), 0

    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        await _ADMISSION.release(reserved)
        return None, _bad(400, "invalid JSON body"), 0
    if not isinstance(body, dict):
        await _ADMISSION.release(reserved)
        return None, _bad(400, "body must be a JSON object"), 0
    return body, None, reserved


# Snapshot fields are intentionally plain JSON types.  The health route must
# never call into the storage/embedding layers or spawn subprocesses.
_HEALTH_LOCK = threading.Lock()
_HEALTH_SNAPSHOT: Dict[str, Any] = {
    "status": "starting",
    "storage": "starting",
    "updated_at": 0.0,
    "health_probe_epoch": 0,
    "embedding_model": os.environ.get(
        "MEMORYCORE_EMBED_MODEL",
        os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "")),
    "embedding_url": os.environ.get("MNEMOSYNE_EMBEDDING_API_URL", ""),
    "embedding_gpu_resident": None,
    "embedding_gpu_status": "unknown",
    "embed_cache": {},
    "governance": {},
    "backpressure": {},
    "db_identity": {},
    "db_identity_mismatch": False,
    "db_identity_mismatch_count": 0,
    "db_identity_last_mismatch_at": None,
    "error": None,
}
_HEALTH_THREAD: Optional[threading.Thread] = None
_HEALTH_STOP = threading.Event()
_GPU_STARTUP_STATUS: Dict[str, Any] = {
    "model": os.environ.get(
        "MEMORYCORE_EMBED_MODEL",
        os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "")),
    "resident": None,
    "detail": "not_checked",
    "checked_at": 0.0,
}


def _health_set(**kwargs) -> None:
    with _HEALTH_LOCK:
        _HEALTH_SNAPSHOT.update(kwargs)


def _health_snapshot() -> Dict[str, Any]:
    with _HEALTH_LOCK:
        snap = dict(_HEALTH_SNAPSHOT)
    snap["governance"] = _governance_snapshot()
    snap["backpressure"] = _p0_snapshot()
    snap["version"] = SERVICE_VERSION
    snap["commit"] = _COMMIT_CACHE or "unknown"
    return snap


def _embed_cache_health() -> Dict[str, Any]:
    try:
        from mnemosyne.core import embeddings as _emb  # noqa: E402
        if hasattr(_emb, "cache_stats"):
            return _emb.cache_stats()
    except Exception as e:
        return {"enabled": False, "error": str(e)}
    return {"enabled": False, "error": "cache_stats unavailable"}


def _probe_health_once() -> None:
    """Synchronous snapshot refresh; runs only on the daemon probe thread."""
    now = time.time()
    try:
        client = _get_client()
        if client is None:
            _health_set(
                status="degraded", storage="unavailable",
                updated_at=now, health_probe_epoch=now,
                error=_client_error)
            _p0_inc("health_probe_error")
            return
        identity_info: Dict[str, Any] = {}
        checker = getattr(client, "check_data_identity", None)
        if callable(checker):
            try:
                got = checker()
                if isinstance(got, dict):
                    identity_info = got
            except Exception as e:
                identity_info = {"error": str(e)}
        try:
            storage = client.stats(all_sessions=True)
        except StorageBusyError as e:
            storage = {"error": f"storage busy: {e}"}
        except Exception as e:
            storage = {"error": str(e)}
        gpu = dict(_GPU_STARTUP_STATUS)
        # Best-effort live ollama ps check inside the probe thread only.
        try:
            gpu.update(_ollama_ps_status(gpu.get("model") or ""))
        except Exception:
            pass
        configured_model = os.environ.get(
            "MEMORYCORE_EMBED_MODEL",
            os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", gpu.get("model", "")))
        probe_status = "ok"
        if isinstance(storage, dict) and storage.get("error"):
            probe_status = "degraded"
        gpu_warning = None
        if configured_model and gpu.get("resident") is not True:
            # 2026-09-19: 模型"未驻留"（闲置未加载 / 被其它模型挤出）不是服务故障——
            # 首次调用会自动加载（实测冷启动单条 Add 0.57 s），规范也只要求 health 返回 2xx。
            # 因此只记 capacity_warning，不再把 status 标成 degraded（旧行为会误导监控与告警）。
            gpu_warning = ("embedding model not GPU-resident (loads on first call): "
                           + str(gpu.get("detail", "unknown")))
        unified = {
            "status": probe_status,
            "storage": storage,
            "updated_at": now,
            "health_probe_epoch": now,
            "embedding_model": configured_model,
            "embedding_url": os.environ.get("MNEMOSYNE_EMBEDDING_API_URL", ""),
            "embedding_gpu_resident": gpu.get("resident"),
            "embedding_gpu_status": gpu.get("detail", "unknown"),
            "capacity_warning": gpu_warning,
            "embed_cache": _embed_cache_health(),
            "db_identity": dict(identity_info.get("db_identity") or {}),
            "db_identity_mismatch": bool(
                identity_info.get("identity_mismatch", False)),
            "db_identity_mismatch_count": int(
                identity_info.get("identity_mismatch_count", 0) or 0),
            "db_identity_last_mismatch_at": identity_info.get(
                "identity_last_mismatch_at"),
            "error": None,
        }
        _health_set(**unified)
        _p0_inc("health_probe_ok")
    except Exception as e:
        _health_set(status="degraded", storage="unavailable",
                    updated_at=now, health_probe_epoch=now, error=str(e))
        _p0_inc("health_probe_error")


def _health_probe_loop() -> None:
    while not _HEALTH_STOP.is_set():
        _probe_health_once()
        _HEALTH_STOP.wait(AML_HEALTH_PROBE_INTERVAL_S)


def _start_health_probe_thread() -> None:
    global _HEALTH_THREAD
    with _HEALTH_LOCK:
        if _HEALTH_THREAD is not None and _HEALTH_THREAD.is_alive():
            return
        _HEALTH_STOP.clear()
        _HEALTH_THREAD = threading.Thread(
            target=_health_probe_loop, name="aml-health-probe", daemon=True)
        _HEALTH_THREAD.start()


# ---- helpers ---------------------------------------------------------------

def _iso_z(ts: Optional[str]) -> Optional[str]:
    """Normalize an ISO timestamp to UTC 'Z' form (AML created_at)."""
    if not ts:
        return None
    try:
        t = ts.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(t)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError):
        return None


def _finite_number(value: Any, default: float = 0.0) -> float:
    """Coerce a candidate score to a finite float (C4: never 500/NaN)."""
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _sanitize_candidates(results: Any) -> List[Dict[str, Any]]:
    """Copy candidate dicts into a score-safe shape before decay/ranking.

    NaN/Infinity/huge-int scores become their defaults, and non-string
    timestamp fields become None so _apply_decay can never raise on them.
    """
    safe: List[Dict[str, Any]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        rr = dict(r)
        rr["dense_score"] = _finite_number(rr.get("dense_score", 0.0), 0.0)
        rr["importance"] = _finite_number(rr.get("importance", 0.5), 0.5)
        for key in ("last_recalled", "timestamp"):
            if rr.get(key) is not None and not isinstance(rr.get(key), str):
                rr[key] = None
        safe.append(rr)
    return safe



_EVENT_TIME_PREFIX_RE = re.compile(
    r"^\[(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?\]")


def _event_time_from_content(content: Any) -> Optional[datetime]:
    """E2: 从 L1 证据前缀 `[YYYY-MM-DD HH:MM]` 解析事件时间（UTC）。

    解析失败返回 None；调用方回退写入时间（与 core.decay._apply_decay 同口径）。
    """
    if not isinstance(content, str):
        return None
    m = _EVENT_TIME_PREFIX_RE.match(content)
    if not m:
        return None
    try:
        year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3))
        hour, minute = int(m.group(4)), int(m.group(5))
        second = int(m.group(6)) if m.group(6) is not None else 0
        return datetime(year, month, day, hour, minute, second,
                        tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _decay_days_from_iso(value: Any, now: datetime) -> int:
    """与 core.decay._apply_decay 逐字同口径：ISO 解析、UTC 补齐、clamp>=0。

    解析失败/类型不对返回 365（core 的保守 fallback）。
    """
    if not value:
        return 365
    try:
        normalized = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalized)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((now - dt).days, 0)
    except (ValueError, TypeError, AttributeError):
        return 365


def _fallback_write_time_days(r: Dict[str, Any], now: datetime) -> int:
    """事件时间缺失时的现行为：last_recalled 优先，其次写入 timestamp，最后 365 天。

    注意 core 语义：last_recalled 存在但不可解析时直接用 365，不再回退 timestamp；
    timestamp 存在但不可解析时同样 365。
    """
    last_str = r.get("last_recalled")
    if last_str:
        return _decay_days_from_iso(last_str, now)
    ts_str = r.get("timestamp")
    if ts_str:
        return _decay_days_from_iso(ts_str, now)
    return 365




def _dedup_candidates(results: Any) -> List[Dict[str, Any]]:
    """Keep first occurrence of each candidate id, preserving source order."""
    seen = set()
    out: List[Dict[str, Any]] = []
    for r in results or []:
        if not isinstance(r, dict):
            continue
        rid = r.get("id")
        if rid in seen:
            continue
        seen.add(rid)
        out.append(r)
    return out


def _rrf_fuse(
        paths: Any, k: int = 60, min_dense: float = 0.3,
        size_weights: bool = False,
        path_weights: Optional[List[float]] = None) -> List[Dict[str, Any]]:
    """E1: 跨查询路由的 Reciprocal Rank Fusion（纯函数）。

    ``paths`` 是每路的**有序候选列表**（rank 1 = 列表首位）。对每个唯一
    candidate id 求 RRF 分 = Σ_path 1/(k + rank)。语义：

    - 同一路内重复 id 只取第一次出现；
    - 单路内 ``dense_score``（有限化后）< ``min_dense`` 的候选**不再删除**：
      它们保留候选身份但融合分记 0，且不占名次（通过门槛的候选按 1..n
      重新排名）。C8 修复：只要任一路有过门槛候选，旧实现会让所有低于
      门槛的候选整段消失；现在返回集合始终 ⊇ 各路原始候选 id 并集；
    - 缺 id / 非 dict / 空 id 跳过；
    - 返回新的 dict 列表，按 RRF 分降序（分数相同保序：先出现的路先）；
    - **不修改入参**，也不删 candidate 的业务字段。

    k 默认 60（RRF 常用值，冻结为 AML_RRF_K 默认），min_dense 默认 0.3
    （与 AML_RECALL_FUSION_MIN_DENSE 默认一致）；只使用名次。
    ``size_weights=True`` 时每路贡献再乘 ``1/该路候选数``（只由 E3
    开启时传入）：单 token 字面扩路命中面窄、候选数少，应该比主查询
    的宽路有更高的话语权；默认 False = 旧 RRF 逐字节行为。
    ``path_weights`` 可选显式每路权重，优先于 ``size_weights``；E3
    用它给主路保留宽度权重、同时把 token 路权重总和限死在主路权重之下。
    """
    try:
        kk = float(k)
    except (TypeError, ValueError):
        kk = 60.0
    if not math.isfinite(kk) or kk <= 0:
        kk = 60.0
    try:
        threshold = float(min_dense)
    except (TypeError, ValueError):
        threshold = 0.0
    if not math.isfinite(threshold) or threshold < 0:
        threshold = 0.0

    scores: Dict[str, float] = {}
    first: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for path_idx, path in enumerate(paths or []):
        path_items = list(path or [])
        # E3 的 size_weights：路径候选数越少，单条命中越珍贵。
        # path_weights（C③ 的 token 路上限）优先于 size_weights。
        if path_weights is not None:
            try:
                path_weight = float(path_weights[path_idx])
            except (IndexError, TypeError, ValueError):
                path_weight = 0.0
            if not math.isfinite(path_weight) or path_weight < 0.0:
                path_weight = 0.0
        else:
            path_weight = (1.0 / max(1, len(path_items))
                           if size_weights else 1.0)
        seen_in_path = set()
        rank = 0
        for cand in path_items:
            if not isinstance(cand, dict):
                continue
            rid = cand.get("id")
            if rid is None or str(rid) == "":
                continue
            key = str(rid)
            # C8: 所有候选先登记，低于门槛者只是拿不到正 RRF 贡献
            #（分记 0），不再从返回集合消失。
            if key not in first:
                item = dict(cand)
                item["rrf_score"] = 0.0
                first[key] = item
                order.append(key)
            if key in seen_in_path:
                continue
            seen_in_path.add(key)
            dense = _finite_number(cand.get("dense_score", 0.0), 0.0)
            if dense < threshold:
                continue
            rank += 1
            scores[key] = scores.get(key, 0.0) + path_weight / (kk + rank)
    for key, item in first.items():
        item["rrf_score"] = scores.get(key, 0.0)
    # 稳定排序：分数相同的候选保持首次出现顺序；低于门槛者排正分者之后。
    ordered = [first[key] for key in order]
    ordered.sort(key=lambda c: c.get("rrf_score", 0.0), reverse=True)
    return ordered


def _finite_signal(value: Any) -> Optional[float]:
    """E3: 返回有限 float；字段缺失/非法/NaN/Inf 返回 None。"""
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _unit_minmax(values: List[float]) -> List[float]:
    """把一组分数 min-max 到 [0,1]；无变化时返回全 0（不贡献排序）。

    C② 修复：极端有限分（例如 [-1e308, 1e308]）的 ``high-low`` 会溢出为
    ``inf``，原实现随后产生 NaN 并把整条字面信号静默丢掉。此时必须保留
    单调顺序，因此退回平均名次归一化；信号不会消失。
    """
    if not values:
        return []
    low = min(values)
    high = max(values)
    if high <= low:
        return [0.0] * len(values)
    span = high - low
    if not math.isfinite(span):
        return _unit_rank(values)
    return [(v - low) / span for v in values]


def _unit_rank(values: List[float]) -> List[float]:
    """把一组分数做平均名次归一化：最高 1.0，最低 0.0，同分同值。"""
    n = len(values)
    if n <= 1:
        return [0.0] * n
    order = sorted(range(n), key=lambda i: (-values[i], i))
    out = [0.0] * n
    i = 0
    while i < n:
        j = i
        base = values[order[i]]
        while j + 1 < n and values[order[j + 1]] == base:
            j += 1
        avg_pos = (i + j) / 2.0
        score = (n - 1 - avg_pos) / (n - 1)
        for k in range(i, j + 1):
            out[order[k]] = score
        i = j + 1
    return out


def _signal_units(
        rows: List[Dict[str, Any]], key: str,
        norm: Optional[str] = None,
) -> Optional[List[float]]:
    """E3: 取 ``rows`` 的一条分数字段并归一化。

    返回 None 表示该字段在所有候选上都没有有效数值（旧数据缺字段 →
    调用方应退回 dense-only）；返回全 0 表示字段存在但无区分度。
    """
    units, _present = _signal_units_present(rows, key, norm=norm)
    return units


def _signal_units_present(
        rows: List[Dict[str, Any]], key: str,
        norm: Optional[str] = None,
) -> Tuple[Optional[List[float]], List[bool]]:
    """E3: 与 ``_signal_units`` 同口径，但额外返回有效值掩码。

    返回 ``(None, present)`` 表示该字段在所有候选上都没有有效数值；
    ``present[pos]`` 标明该行是否真的有此字段，供 C① 局部缺失回退使用。
    """
    raw: List[Optional[float]] = []
    present: List[bool] = []
    for row in rows:
        val = None
        if isinstance(row, dict):
            val = _finite_signal(row.get(key))
        # dense_score 是旧路径必有的排序基准；缺失按 0，保证 dense-only 回退。
        if val is None and key == "dense_score":
            val = 0.0
        present.append(val is not None)
        raw.append(val)
    if not any(present):
        return None, present
    # 归一化分母沿用旧口径：缺失行按 0 参与 min/max，保持与未修复版本的
    # 相对尺度稳定；但后续加权时缺字段的行不会被该项惩罚。
    vals = [0.0 if v is None else float(v) for v in raw]
    if max(vals) <= min(vals):
        return [0.0] * len(vals), present
    if (norm or _RERANK_NORM) == "rank":
        return _unit_rank(vals), present
    return _unit_minmax(vals), present


def _lexical_weighted_scores(
        rows: List[Dict[str, Any]], *,
        norm: Optional[str] = None,
        weights: Optional[Tuple[float, float, float]] = None,
) -> List[float]:
    """E3: dense/keyword/fts 归一化加权分（纯函数，不修改入参）。

    - 默认权重 (dense, keyword, fts) = (0.2, 0.4, 0.4)；
    - 只对「在所有候选间有区分度」的信号计入；整列缺字段/全零字面
      信号时自动回退为只按 dense（旧数据兼容）；
    - C① 局部缺失回退：某一行的 keyword/fts 缺失时，该行只按实际存在
      的信号加权，不把缺失当 0 分惩罚；dense 始终作为保底信号；
    - 返回长度与 ``rows`` 一致，全部信号都无区分度时返回全 0。
    """
    rows = list(rows or [])
    n = len(rows)
    if n == 0:
        return []
    if weights is None:
        weights = (_RERANK_W_DENSE, _RERANK_W_KEYWORD, _RERANK_W_FTS)
    try:
        wd, wk, wf = (max(0.0, float(x)) for x in weights)
    except (TypeError, ValueError):
        wd, wk, wf = (0.2, 0.4, 0.4)
    if wd + wk + wf <= 0.0:
        wd, wk, wf = (0.2, 0.4, 0.4)

    units: List[Tuple[float, List[float], List[bool]]] = []
    for key, weight in (("dense_score", wd), ("keyword_score", wk),
                        ("fts_score", wf)):
        if weight <= 0.0:
            continue
        unit, present = _signal_units_present(rows, key, norm=norm)
        if unit is None:
            continue
        if max(abs(v) for v in unit) <= 1e-12:
            continue
        units.append((weight, unit, present))
    if not units:
        return [0.0] * n
    scores: List[float] = []
    for pos in range(n):
        num = 0.0
        den = 0.0
        for weight, unit, present in units:
            if present[pos]:
                num += weight * unit[pos]
                den += weight
        scores.append(num / den if den > 0.0 else 0.0)
    return scores


def _rerank_path_lexical(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """E3: 用加权字面分 × 既有衰减对一路内部重排。

    先把归一化加权分临时放进 ``dense_score``，复用 ``_apply_decay``
    （含 E2 事件时间开关）得到该路顺序；再恢复原始 dense_score，因为
    ``_rrf_fuse`` 的 min_dense 门槛仍必须按存储层语义判断。加权分只在
    pre-RRF 路径内部决定名次，最终分值仍由 E1 的 RRF×decay 统一计算。
    """
    rows = list(rows or [])
    if len(rows) <= 1:
        return rows
    scores = _lexical_weighted_scores(rows)
    scaled: List[Dict[str, Any]] = []
    for row, score in zip(rows, scores):
        if not isinstance(row, dict):
            continue
        rr = dict(row)
        rr["_e3_dense_raw"] = _finite_number(row.get("dense_score", 0.0), 0.0)
        rr["_e3_weighted_score"] = score
        # 临时把加权分放进 dense_score，仅为了复用既有 decay 排序；
        # 排序后再恢复原始 dense_score，RRF 门槛读 _rrf_gate_score。
        rr["dense_score"] = score
        scaled.append(rr)
    if not scaled:
        return []
    decayed = _apply_decay(scaled)
    for rr in decayed:
        # 恢复原始 dense_score：RRF 的单路 min_dense 门槛仍按存储层
        # 语义判断；E3 加权分只决定该路内部名次。
        rr["dense_score"] = rr.pop("_e3_dense_raw", 0.0)
    return decayed


def _candidate_pool_size(top_k: int) -> int:
    """Recall candidate pool: wider than top_k but bounded by the AML cap.

    L3: AML_RECALL_POOL_MULT > 1 时内部候选池可超过 AML 的 top_k 上限
    （100 条上限是**响应**约束，不是我们内部池子的约束）。默认 1 = 原行为。
    """
    base = min(max(int(top_k), _RECALL_CANDIDATE_POOL_MIN), _MAX_TOP_K)
    if _RECALL_POOL_MULT <= 1:
        return base
    return min(base * _RECALL_POOL_MULT, _RECALL_POOL_CAP)


def _search_candidate_pool_size(top_k: int) -> int:
    """E3 开启时给检索至少 5×top_k 的深池（仍受 AML_RECALL_POOL_CAP 约束）。

    只有 AML_RERANK_LEXICAL=1 才生效；关闭时逐字节等于 ``_candidate_pool_size``。
    """
    base = _candidate_pool_size(top_k)
    if not _RERANK_LEXICAL:
        return base
    deep = min(_MAX_TOP_K * _RERANK_POOL_MIN_MULT, _RECALL_POOL_CAP)
    return max(base, deep)


def _search_path_weights(paths: Any, main_path_count: int) -> List[float]:
    """C③: 给 RRF 各路计算权重，限制 token 扩路叠加。

    主路（原 query / keyword / options 路，共 ``main_path_count`` 条）继续
    沿用 E3 的 ``1/候选数`` 权重；token 扩路权重之和最多为主路权重的
    ``_RERANK_TOKEN_RRF_TOTAL_MULT`` 倍。这样同一候选即使同时出现在多条
    窄 token 路，也不能叠加压过主路的同 rank 高分证据。
    """
    weights = [1.0 / max(1, len(path or [])) for path in paths or []]
    if not weights or main_path_count <= 0 or main_path_count >= len(weights):
        return weights
    primary_weight = max(0.0, weights[0])
    cap = primary_weight * _RERANK_TOKEN_RRF_TOTAL_MULT
    token_total = sum(weights[main_path_count:])
    if cap <= 0.0:
        for idx in range(main_path_count, len(weights)):
            weights[idx] = 0.0
    elif token_total > cap:
        scale = cap / token_total
        for idx in range(main_path_count, len(weights)):
            weights[idx] *= scale
    return weights


def _json_compact_bytes(obj: Any) -> bytes:
    """Exact JSONResponse body encoding used by Starlette (compact JSON)."""
    return json.dumps(
        obj, ensure_ascii=False, allow_nan=False,
        separators=(",", ":")).encode("utf-8")


def _bounded_search_data(
        items: List[Dict[str, Any]], max_bytes: int,
) -> Tuple[List[Dict[str, Any]], int]:
    """B: keep a JSON-body prefix of ``items`` within ``max_bytes`` bytes.

    Truncation is tail-first: the highest-ranked items are preserved.  Returns
    ``(kept_items, dropped_count)``.  The body length calculation mirrors
    ``JSONResponse.render`` exactly so the caller can guarantee the byte cap
    before constructing the response.
    """
    limit = max(0, int(max_bytes))
    empty_body = _json_compact_bytes({"data": []})
    if limit < len(empty_body):
        return [], len(items)
    prefix = b'{"data":['
    suffix = b']}'
    kept: List[Dict[str, Any]] = []
    total = len(prefix)
    for pos, item in enumerate(items):
        item_bytes = _json_compact_bytes(item)
        comma = 1 if kept else 0
        if total + comma + len(item_bytes) + len(suffix) > limit:
            return kept, len(items) - pos
        total += comma + len(item_bytes)
        kept.append(item)
    return kept, 0


def _event_date_prefix(ts: Any) -> str:
    """L1(2026-09-19): 把消息 timestamp(毫秒) 变成证据文本里的日期前缀。

    规范 Add 的 message 可带 timestamp，此前被丢弃 → 时间类问题（维度 C）的证据里
    看不到任何日期。这里只把"事件时间"带进证据文本；**不改**存储层 timestamp（写入
    时间）与衰减语义，避免影响既有排序行为。缺失/非法一律返回空串。
    """
    if ts is None or isinstance(ts, bool):
        return ""
    try:
        ms = float(ts)
    except (TypeError, ValueError):
        return ""
    if ms != ms or ms in (float("inf"), float("-inf")) or ms <= 0:  # NaN/Inf/负
        return ""
    secs = ms / 1000.0 if ms > 1e11 else ms   # 秒/毫秒自动判定
    try:
        dt = datetime.fromtimestamp(secs, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return dt.strftime("[%Y-%m-%d %H:%M] ")
def _with_role_label(prefix: str, role: str) -> str:
    """Append a speaker label to ``prefix`` when AML_ROW_ROLE_LABEL is on.

    Fail-safe: an unknown/empty role keeps the prefix untouched, so a caller
    that passes something unexpected can never produce a bogus label.
    """
    if not _ROW_ROLE_LABEL:
        return prefix
    who = (role or "").strip().lower()
    if who not in ("user", "assistant"):
        return prefix
    return prefix + who + ": "


def _split_fragments(content: str, max_chars: int = _MAX_FRAGMENT_CHARS) -> List[str]:
    """Split one message into fact fragments at sentence boundaries.

    2026-09-19 round-3B: previous implementation stripped separators and
    re-joined parts with ``"。"``, which corrupted code/commands and produced
    ``。。`` when the source already used Chinese periods.  This version keeps
    each boundary character attached to the preceding piece, so joining all
    fragments reproduces the original text byte-for-byte.  Pieces are then
    packed without exceeding ``max_chars``.
    """
    text = content or ""
    if not text.strip():
        return []
    max_chars = max(1, int(max_chars))
    if len(text) <= max_chars:
        return [text]

    parts = _SENTENCE_SPLIT_KEEP_RE.split(text)
    pieces: List[str] = []
    for i in range(0, len(parts), 2):
        body = parts[i]
        delim = parts[i + 1] if i + 1 < len(parts) else ""
        if body or delim:
            pieces.append(body + delim)
    if not pieces:
        pieces = [text]

    frags: List[str] = []
    buf = ""
    for piece in pieces:
        if len(piece) > max_chars:
            if buf:
                frags.append(buf)
                buf = ""
            for i in range(0, len(piece), max_chars):
                frags.append(piece[i:i + max_chars])
            continue
        if buf and len(buf) + len(piece) > max_chars:
            frags.append(buf)
            buf = ""
        buf += piece
    if buf:
        frags.append(buf)
    return frags or [text]

def _dedup_query_text(content: str) -> str:
    """Query text used by the author-scoped dedup recall.

    查询用首句截断 (CJK ≤16 字 / 英文 ≤6 词): mnemosyne 的词法门禁对长查询
    更严 (≥4 token → min_relevance 0.3, 实测多一个 "替代 Redis" 尾巴就会
    把命中从 1 变 0), 完整片段作为 query 反而召回不到候选; 短查询命中率
    更高, 候选池再交给 _find_best_match + _aml_match_level 精判。
    """
    first = re.split(_SENTENCE_SPLIT_RE, content)[0].strip()
    if re.search(r"[\u4e00-\u9fff]", first):
        query = first[:16].rstrip(" ：:，,。！？")
    else:
        words = first.split()
        query = " ".join(words[:6])
    if not query:
        query = content[:16]
    return query


def _dedup_recall(client: ColdStoreClient, content: str,
                  user_id: str) -> List[Dict[str, Any]]:
    """Author-scoped dedup recall (isolation hard constraint)."""
    return client.recall_results(
        _dedup_query_text(content), top_k=_DEDUP_RECALL_TOP_K,
        author_id=user_id, bump=False)


def _duplicate_fingerprint(text: str) -> str:
    """Normalize to a near-verbatim fingerprint: case/width/space/punctuation
    insensitive, but entity, number and unique-marker characters remain.
    """
    normalized = unicodedata.normalize("NFKC", text or "").casefold()
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


_UPDATE_MARKERS = (
    "改为", "改成", "更新为", "修正为", "替换为", "换成", "改用",
    "不再", "停止使用", "弃用", "替代",
)


def _aml_match_level(content: str, cand_content: str, dense: float) -> Optional[str]:
    """AML 侧合并门禁 (2026-09-19 round-3B 收紧).

    duplicate 只对“接近逐字重复”生效: 原文 strip 后相等, 或 NFKC/casefold/
    去空白标点后的 fingerprint 相等。不同实体/数字/唯一标记会改变
    fingerprint, 因此不会判 duplicate。显式更新/反转措辞 (如 A→改为B)
    继续走 similar → merge, 保住更新语义。
    """
    a = (content or "").strip()
    b = (cand_content or "").strip()
    if not a or not b:
        return None
    if a == b:
        return "same"
    fa = _duplicate_fingerprint(a)
    fb = _duplicate_fingerprint(b)
    if fa and fa == fb:
        return "same"

    # Explicit update/reversal language preserves the old A → 改为 B merge
    # contract.  The keyword has to be in the newer content; otherwise two
    # same-template records with different unique markers fall through to a
    # fresh write instead of being merged/duplicated.
    if any(marker in a for marker in _UPDATE_MARKERS):
        if (dense or 0) >= 0.50:
            return "similar"
    return None


def _plan_fragment(client: ColdStoreClient, content: str,
                   user_id: str) -> Dict[str, Any]:
    """Precompute one fragment's governance decision without any write.

    This mirrors _store_fragment's decision tree exactly, but only reads
    (classify + read-only dedup recall).  It is safe to call before the
    request's write phase; a budget deadline may fire here and return a
    retryable 5xx with zero side effects.
    """
    decision = classify(content, importance=_FACT_IMPORTANCE)
    if decision["decision"] == STALE:
        return {"action": "stale",
                "result": {"status": "stale",
                           "detail": decision.get("reason", "")}}

    try:
        existing = _dedup_recall(client, content, user_id)
    except Exception:
        existing = []

    if existing:
        matched = _find_best_match(content, existing)
        if matched:
            level = _aml_match_level(content, matched["content"],
                                     matched.get("dense_score", 0))
            if level == "same":
                return {"action": "duplicate",
                        "memory_id": matched["id"],
                        "result": {"status": "duplicate",
                                   "memory_id": matched["id"]}}
            if level == "similar":
                merged = _merge_two_entries(content, matched["content"])
                if (isinstance(merged, str)
                        and merged.strip() != matched["content"].strip()):
                    return {"action": "update",
                            "memory_id": matched["id"],
                            "text": merged,
                            "result": None}
    return {"action": "insert", "text": content, "result": None}


def _plan_doc_texts(plans: List[Dict[str, Any]]) -> List[str]:
    """Document texts a plan may embed during its write path."""
    texts: List[str] = []
    for plan in plans or []:
        action = plan.get("action")
        if action == "insert":
            text = plan.get("text") or ""
            if text:
                texts.append(text)
        elif action == "update":
            text = plan.get("text") or ""
            if text:
                texts.append(text)
            fallback = plan.get("fallback_text") or ""
            if fallback:
                texts.append(fallback)
    return texts


def _assert_plan_embeddings_cached(client: ColdStoreClient,
                                   plans: List[Dict[str, Any]]) -> None:
    """Fail before a write if any plan text is not already cache-resident.

    This is the P0.5 write-phase guard requested by review §4.4: Phase A owns
    all cache misses; Phase B must be a short SQLite-only section.  Clients
    without the optional probe (test doubles, remote backend) are skipped so
    the contract is unchanged for them.
    """
    checker = getattr(client, "assert_embeddings_cached", None)
    if not callable(checker):
        return
    texts = _plan_doc_texts(plans)
    if not texts:
        return
    try:
        ok = bool(checker("doc", texts))
    except Exception:
        ok = False
    if not ok:
        raise _EmbeddingUnavailable(
            "write-phase embedding cache miss; Phase A must precompute all "
            "plan texts before entering the write phase")


def _find_exact_memory(client: ColdStoreClient, content: str,
                       user_id: str) -> Optional[str]:
    """Return an existing same-content memory id if the backend can do so.

    The P0.5 crash-window fix must not depend on recall recall-quality gates:
    a fragment committed just before a crash can be absent from the dedup
    recall candidate set (review probe_ledger_crash).  LocalBackend therefore
    exposes a direct exact-content lookup used only before an insert/update.
    """
    finder = getattr(client, "find_exact", None)
    if callable(finder):
        try:
            found = finder(content, author_id=user_id)
        except Exception:
            found = None
        if found:
            return str(found)
    # Best-effort fallback for in-memory test/legacy backends that expose a
    # plain row list but not the optional find_exact protocol.  Production
    # LocalBackend uses its SQL find_exact above; this branch keeps crash-replay
    # idempotency testable without depending on recall quality gates.
    rows = getattr(client, "rows", None)
    if not isinstance(rows, (list, tuple)):
        rows = getattr(client, "memories", None)
    if isinstance(rows, (list, tuple)):
        for row in rows:
            if not isinstance(row, dict):
                continue
            if row.get("content") != content:
                continue
            row_author = row.get("author_id")
            if user_id is None or row_author in (None, user_id):
                rid = row.get("id")
                if rid is not None:
                    return str(rid)
    return None


def _execute_plan(client: ColdStoreClient, plan: Dict[str, Any],
                  content: str, user_id: str,
                  source: str = "conversation") -> Dict[str, Any]:
    """Execute a precomputed plan; fall back to a fresh write exactly as before."""
    action = plan.get("action")
    if action in ("stale", "duplicate"):
        return dict(plan.get("result") or {})
    if action == "update":
        planned_text = plan.get("text") or content
        # Crash replay guard: if the merged text is already present, the
        # previous attempt committed the update but crashed before its ledger
        # checkpoint.  Do not bump/rewrite it.
        existing = _find_exact_memory(client, planned_text, user_id)
        if existing:
            return {"status": "duplicate", "memory_id": existing}
        _assert_plan_embeddings_cached(client, [plan])
        try:
            r = client.update(plan["memory_id"], plan["text"],
                              author_id=user_id)
            if r.get("status") == "updated":
                return {"status": "updated",
                        "memory_id": plan["memory_id"],
                        "detail": "merged into existing entry"}
        except Exception:
            pass  # update failed → fall through to fresh write
        # Fallback write uses the original fragment text; it was warmed in
        # Phase A specifically for this case.
        existing = _find_exact_memory(client, content, user_id)
        if existing:
            return {"status": "duplicate", "memory_id": existing}
    elif action == "insert":
        # Crash replay guard independent of recall gates.
        existing = _find_exact_memory(client, content, user_id)
        if existing:
            return {"status": "duplicate", "memory_id": existing}
        _assert_plan_embeddings_cached(client, [plan])
    r = client.remember(content, importance=_FACT_IMPORTANCE, scope="global",
                        author_id=user_id, source=source)
    if r.get("status") == "stored":
        return {"status": "stored", "memory_id": r.get("memory_id")}
    if r.get("status") == "filtered":
        return {"status": "filtered", "detail": r.get("detail", "")}
    return {"status": "error", "detail": str(r)}


def _store_fragment(client: ColdStoreClient, content: str, user_id: str,
                    source: str = "conversation") -> Dict[str, Any]:
    """Write one fact fragment with online governance (差异化核心).

    1. stale filter  — 过时状态记录 (≤80字含"已修复"式标记) 不写入
    2. semantic dedup/merge — 同一事实跳过; 相似事实合并进同一条
       ("方案A" + "方案A改为B" → 一条, 不是两条)
    3. remember with author_id = user_id (隔离硬约束)
    """
    plan = _plan_fragment(client, content, user_id)
    return _execute_plan(client, plan, content, user_id, source)


def _warm_embedding_cache(client: ColdStoreClient, kind: str,
                          texts: List[str], deadline: float) -> int:
    """Warm the mnemosyne embedding cache in bounded batches.

    returns the number of texts submitted.  Raises _EmbeddingUnavailable when
    a real backend explicitly returns an empty list for non-empty input, which
    is the library's "embedding call failed" signal.  Test doubles that do not
    expose the warm-up method are treated as "unknown, continue" so contract
    tests keep their deterministic local fakes.
    """
    if not texts:
        return 0
    # Preserve order while removing duplicates (same text => same cache key).
    seen = set()
    ordered: List[str] = []
    for text in texts:
        if text in seen:
            continue
        seen.add(text)
        ordered.append(text)
    prefetch = getattr(client, "prefetch_embeddings", None)
    use_prefetch = callable(prefetch)
    fallback_fn_name = "embed_queries" if kind == "query" else "embed_texts"
    fallback_fn = getattr(client, fallback_fn_name, None)
    if not use_prefetch and not callable(fallback_fn):
        return 0
    submitted = 0
    batch_size = max(1, AML_EMBED_BATCH_SIZE)
    for start in range(0, len(ordered), batch_size):
        _check_deadline(deadline)
        chunk = ordered[start:start + batch_size]
        _acquire_embed_gate(deadline)
        try:
            if use_prefetch:
                result = prefetch(kind, chunk)
            else:
                result = fallback_fn(chunk)
        finally:
            _release_embed_gate()
        if isinstance(result, list) and not result:
            fn_name = "prefetch_embeddings" if use_prefetch else fallback_fn_name
            raise _EmbeddingUnavailable(
                f"{fn_name} returned an empty result for {len(chunk)} text(s)")
        submitted += len(chunk)
        _check_deadline(deadline)
    return submitted


def _precompute_add_plans(client: ColdStoreClient,
                         fragments: List[Tuple[str, str]],
                         user_id: str,
                         deadline: float) -> List[Dict[str, Any]]:
    """Phase A: query/doc batch warm-up + per-fragment decisions (no writes).

    P0.5 changes:
      * classify before warming so STALE fragments issue no embeddings;
      * only warm query/doc texts the plan may execute;
      * every returned plan is verified cache-resident before Phase B.
    """
    if not fragments:
        return []

    # Pure, read-only stale pre-screen first.  classify() has no DB writes.
    decisions: List[Dict[str, Any]] = []
    query_texts: List[str] = []
    for _role, text in fragments:
        _check_deadline(deadline)
        decision = classify(text, importance=_FACT_IMPORTANCE)
        decisions.append(decision)
        if decision["decision"] != STALE:
            query_texts.append(_dedup_query_text(text))
    _warm_embedding_cache(client, "query", query_texts, deadline)

    plans: List[Dict[str, Any]] = []
    doc_texts: List[str] = []
    for (_role, text), decision in zip(fragments, decisions):
        _check_deadline(deadline)
        if decision["decision"] == STALE:
            plan = {"action": "stale",
                    "result": {"status": "stale",
                               "detail": decision.get("reason", "")}}
        else:
            # Reuse the pure classification decision; _plan_fragment mirrors
            # this decision tree exactly for non-STALE content.
            plan = _plan_fragment(client, text, user_id)
        _check_deadline(deadline)
        action = plan.get("action")
        if action == "insert":
            doc_texts.append(plan.get("text") or text)
        elif action == "update":
            # The write path may update, or fall back to remember() using the
            # original text; warm both so the write phase is cache-only.
            plan["fallback_text"] = text
            doc_texts.append(plan.get("text") or "")
            doc_texts.append(text)
        plans.append(plan)
    _warm_embedding_cache(
        client, "doc", [t for t in doc_texts if t], deadline)
    _assert_plan_embeddings_cached(client, plans)
    _check_deadline(deadline)
    return plans


# ---- helpers ---------------------------------------------------------------
def _check_auth(request: Request) -> bool:
    if not AML_API_KEY:
        return True  # smoke mode: no auth configured
    headers = request.headers
    candidate = headers.get("x-api-key", "")
    if not candidate:
        auth = headers.get("authorization", "")
        if auth.startswith("Bearer "):
            candidate = auth[7:].strip()
        elif auth.startswith("Token "):
            candidate = auth[6:].strip()
        else:
            candidate = auth.strip()
    return bool(candidate) and secrets.compare_digest(candidate, AML_API_KEY)


def _bad(status: int, detail: str) -> JSONResponse:
    # AML business errors use {"detail":{"reason":"..."}} (spec §06).
    return JSONResponse({"detail": {"reason": detail}}, status_code=status)


def _validation_error(reason: str) -> JSONResponse:
    return _bad(422, reason)


def _require_str(body: Dict[str, Any], field: str) -> Optional[str]:
    v = body.get(field)
    if v is None or not isinstance(v, str) or not v.strip():
        return None
    return v


class _MalformedImagePart(ValueError):
    """Structural image_url error -> request validation 422 (not skip)."""


def _parse_inline_image_url(url: Any) -> Tuple[str, int, str]:
    """Parse an inline data:image Base64 URL without decoding the payload.

    Returns (normalized_mime, estimated_decoded_bytes, payload_status) where
    payload_status is "valid" or "invalid".  Structural problems (remote URL,
    unsupported mime, missing ;base64,) raise _MalformedImagePart so the caller
    can return 422; only a malformed *payload* is a tolerant skip.
    """
    if not isinstance(url, str) or len(url) < 12:
        raise _MalformedImagePart(
            "must be a non-empty inline data:image/...;base64 URL")
    lower = url.lower()
    if not lower.startswith("data:image/"):
        raise _MalformedImagePart(
            "must be an inline data:image/...;base64 URL")
    sep = lower.find(";base64,")
    if sep < 0:
        raise _MalformedImagePart(
            "must be an inline data:image/...;base64 URL")
    mime = lower[11:sep].strip().lower()
    if mime not in _SUPPORTED_IMAGE_MIMES:
        raise _MalformedImagePart(
            f"uses unsupported image mime {mime!r}; expected JPEG, PNG or WebP")
    payload = re.sub(r"\s+", "", url[sep + len(";base64,"):])
    normalized_mime = f"image/{mime}"
    if not payload or len(payload) % 4 != 0 or not _BASE64_RE.fullmatch(payload):
        # Review round 3 / C2: a technically inline data URI with a bad
        # Base64 payload must be skipped and counted, not rejected with 4xx.
        return normalized_mime, 0, "invalid"
    padding = payload.count("=")
    decoded = (len(payload) // 4) * 3 - padding
    if decoded < 0:
        return normalized_mime, 0, "invalid"
    return normalized_mime, decoded, "valid"


def _estimate_inline_image_bytes(url: Any) -> Optional[int]:
    """Backward-compatible size helper; invalid payloads return None."""
    try:
        _mime, size, status = _parse_inline_image_url(url)
    except _MalformedImagePart:
        return None
    if status != "valid":
        return None
    return size


def _scan_content_parts(
        content: Any, where: str
) -> Tuple[List[str], List[Dict[str, Any]], Optional[str]]:
    """Extract ordered texts and image-part metadata from content.

    Structural errors are returned as error_reason (422).  Image payloads are
    classified as valid or invalid; no original image bytes are retained and no
    Base64 payload is decoded.
    """
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return [], [], f"{where} must be a non-empty string or content array"
        return [text], [], None

    if not isinstance(content, list) or not content:
        return [], [], f"{where} must be a non-empty string or content array"

    texts: List[str] = []
    image_events: List[Dict[str, Any]] = []
    for idx, part in enumerate(content):
        if not isinstance(part, dict):
            return [], image_events, f"{where}[{idx}] must be an object"
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                return [], image_events, (
                    f"{where}[{idx}].text must be a non-empty string")
            texts.append(text.strip())
        elif part_type == "image_url":
            image = part.get("image_url")
            if not isinstance(image, dict):
                return [], image_events, (
                    f"{where}[{idx}].image_url must be an object")
            try:
                mime, decoded, payload_status = _parse_inline_image_url(
                    image.get("url"))
            except _MalformedImagePart as e:
                return [], image_events, (
                    f"{where}[{idx}].image_url.url {e}")
            image_events.append({
                "mime": mime,
                "decoded_bytes": decoded,
                "payload_status": payload_status,
                "part_index": idx,
            })
        else:
            return [], image_events, (
                f"{where}[{idx}].type must be 'text' or 'image_url'")
    return texts, image_events, None


def _empty_image_counts() -> Dict[str, int]:
    return {
        "accepted": 0,
        "oversize": 0,
        "aggregate": 0,
        "invalid": 0,
        "cumulative_bytes": 0,
    }


def _apply_image_limits(
        image_events: List[Dict[str, Any]], cumulative_bytes: int = 0
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Classify image events under the 10 MiB/image + 30 MiB/request limits.

    The aggregate counter includes every valid decoded payload in source order;
    skipped-but-valid parts still count toward the request total so the limit is
    enforced even when earlier images were individually oversize.
    """
    annotated: List[Dict[str, Any]] = []
    counts = _empty_image_counts()
    running = max(int(cumulative_bytes or 0), 0)
    for raw in image_events or []:
        ev = dict(raw)
        if ev.get("payload_status") != "valid":
            ev["kind"] = "invalid"
            counts["invalid"] += 1
            annotated.append(ev)
            continue
        size = max(int(ev.get("decoded_bytes") or 0), 0)
        running += size
        if size > _MAX_IMAGE_DECODED_BYTES:
            ev["kind"] = "oversize"
            counts["oversize"] += 1
        elif running > _MAX_TOTAL_IMAGE_DECODED_BYTES:
            ev["kind"] = "aggregate"
            counts["aggregate"] += 1
        else:
            ev["kind"] = "accepted"
            counts["accepted"] += 1
        annotated.append(ev)
    counts["cumulative_bytes"] = running
    return annotated, counts


def _extract_texts(content: Any, where: str) -> Tuple[List[str], int, int, Optional[str]]:
    """Extract ordered text parts from Add/Search content (string or array).

    Returns (texts, accepted_image_count, skipped_image_count, error_reason).
    Image parts are counted only; no visual processing, no original image
    retention.  Single images above 10 MiB, request totals above 30 MiB, and
    malformed Base64 payloads are skipped and counted while all text is kept.
    """
    texts, image_events, err = _scan_content_parts(content, where)
    if err:
        return [], 0, 0, err
    _annotated, counts = _apply_image_limits(image_events, 0)
    skipped = counts["oversize"] + counts["aggregate"] + counts["invalid"]
    return texts, counts["accepted"], skipped, None


def _placeholder_content(session_id: str,
                         image_events: List[Dict[str, Any]]) -> str:
    """Metadata-only placeholder for legal Add requests without text parts.

    G4/B1: image-only (including all-skipped-image) requests must have a
    durable, immediately searchable record instead of returning a zero-write
    200.  No original image bytes, OCR output, or image evidence are retained.
    """
    chosen = None
    for ev in image_events or []:
        if ev.get("mime"):
            chosen = ev
            break
    if chosen is None:
        return (f"[image-only message] mime=image/unknown decoded_bytes=0 "
                f"session={session_id}")
    mime = chosen.get("mime") or "image/unknown"
    decoded = int(chosen.get("decoded_bytes") or 0)
    return (f"[image-only message] mime={mime} decoded_bytes={decoded} "
            f"session={session_id}")


def _write_placeholder(client: ColdStoreClient, user_id: str, session_id: str,
                       image_events: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Persist one placeholder metadata record for a no-text legal Add.

    Route through the module-level _store_fragment so governance and test
    double hooks stay consistent.  duplicate/updated are acceptable: the
    record already exists and is searchable, so the 200 is still truthful.
    """
    content = _placeholder_content(session_id, image_events)
    try:
        result = _store_fragment(
            client, content, user_id, source="image-only")
    except Exception as e:
        return {"status": "error", "detail": str(e)}
    status = str(result.get("status", "error"))
    if status == "stored":
        return {"status": "stored", "memory_id": result.get("memory_id")}
    if status in ("duplicate", "updated"):
        return {"status": status, "memory_id": result.get("memory_id")}
    if status in ("stale", "filtered"):
        return {"status": "error",
                "detail": f"placeholder metadata record was {status}: "
                          f"{result.get('detail', '')}"}
    return {"status": "error", "detail": str(result)}


def _log_multimodal_counts(accepted_count: int, skipped_count: int) -> None:
    """One aggregate count log per request; payload bytes are never logged."""
    if accepted_count or skipped_count:
        logger.info(
            "AML multimodal content: image_parts=%d skipped_parts=%d "
            "(no original image / no OCR / no image evidence returned)",
            accepted_count, skipped_count,
        )


def _governance_snapshot() -> Dict[str, int]:
    with _GOVERNANCE_LOCK:
        return dict(_GOVERNANCE_COUNTS)


def _record_governance(request_id: str, parsed_messages: List[Tuple[str, str]],
                       write_results: List[Dict[str, Any]],
                       media_counts: Dict[str, int]) -> None:
    """Aggregate policy outcomes for observability, never for the response.

    Governance decisions (stale / filtered / duplicate / stored / updated)
    are successful Add outcomes per fix-round2 I-1 and must not be turned
    into a 4xx contract error.  The request body is never logged.
    """
    statuses = [str(r.get("status", "error")) for r in write_results]
    with _GOVERNANCE_LOCK:
        _GOVERNANCE_COUNTS["requests"] += 1
        _GOVERNANCE_COUNTS["fragments"] += len(statuses)
        _GOVERNANCE_COUNTS["image_parts"] += int(
            media_counts.get("accepted", 0))
        _GOVERNANCE_COUNTS["oversize_image_parts"] += int(
            media_counts.get("oversize", 0))
        _GOVERNANCE_COUNTS["aggregate_image_parts"] += int(
            media_counts.get("aggregate", 0))
        _GOVERNANCE_COUNTS["invalid_image_parts"] += int(
            media_counts.get("invalid", 0))
        if not parsed_messages:
            _GOVERNANCE_COUNTS["textless_requests"] += 1
        for status in statuses:
            if status in _GOVERNANCE_COUNTS:
                _GOVERNANCE_COUNTS[status] += 1
    logger.info(
        "AML governance request_id=%s text_parts=%d fragments=%d "
        "statuses=%s textless_images=%d media=%s",
        request_id, len(parsed_messages), len(statuses), statuses,
        int(media_counts.get("accepted", 0)), media_counts,
    )


def _ledger_path() -> str:
    return os.path.join(_require_data_dir(), "aml_request_ledger.sqlite3")


def _ledger_connect() -> "sqlite3.Connection":
    path = _ledger_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
    # Schema creation/migration is a process-wide setup step; serialise it so
    # concurrent first requests cannot both issue the same ALTER TABLE.
    with _LEDGER_LOCK:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aml_request_ledger (
                request_id TEXT PRIMARY KEY,
                body_hash TEXT NOT NULL,
                status_code INTEGER NOT NULL DEFAULT 0,
                response_json TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            )
            """
        )
        # P0 migration: phase/checkpoint are additive and safe on existing ledgers.
        cols = {row[1] for row in conn.execute(
            "PRAGMA table_info(aml_request_ledger)").fetchall()}
        if "phase" not in cols:
            conn.execute(
                "ALTER TABLE aml_request_ledger "
                "ADD COLUMN phase TEXT NOT NULL DEFAULT 'reserved'")
        if "checkpoint" not in cols:
            conn.execute(
                "ALTER TABLE aml_request_ledger "
                "ADD COLUMN checkpoint TEXT NOT NULL DEFAULT '{}'")
        conn.commit()
    return conn


def _ledger_parse_checkpoint(raw: Any) -> Dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _body_hash(body: Dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _ledger_begin(request_id: str, body_hash: str):
    """Reserve/replay/resume a request_id atomically in SQLite.

    Returns (state, row) where state is new/replay/resume/conflict/busy.
    A resume row carries a non-zero checkpoint; the caller skips that many
    already-committed fragments instead of replaying them.
    """
    conn = _ledger_connect()
    try:
        with _LEDGER_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_id, body_hash, status_code, response_json, "
                "updated_at, phase, checkpoint "
                "FROM aml_request_ledger WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row[1] != body_hash:
                    conn.execute("ROLLBACK")
                    return "conflict", row
                phase = str(row[5] or "reserved")
                checkpoint = _ledger_parse_checkpoint(row[6])
                completed = max(0, int(checkpoint.get("completed", 0) or 0))
                if phase == "done" and row[2] and row[3]:
                    conn.execute("ROLLBACK")
                    return "replay", row
                if phase == "retryable_partial":
                    conn.execute("ROLLBACK")
                    return "resume", row
                if phase in ("reserved", "planned", "writing"):
                    age = time.time() - float(row[4] or 0)
                    if age < _LEDGER_INFLIGHT_STALE_SECONDS:
                        conn.execute("ROLLBACK")
                        return "busy", row
                    if completed > 0:
                        # Crashed mid-write: resume from the saved checkpoint.
                        conn.execute("ROLLBACK")
                        return "resume", row
                    conn.execute(
                        "DELETE FROM aml_request_ledger WHERE request_id = ?",
                        (request_id,))
                elif row[2] and row[3]:
                    conn.execute("ROLLBACK")
                    return "replay", row
                else:
                    conn.execute(
                        "DELETE FROM aml_request_ledger WHERE request_id = ?",
                        (request_id,))
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO aml_request_ledger "
                "(request_id, body_hash, status_code, response_json, "
                "updated_at, phase, checkpoint) "
                "VALUES (?, ?, 0, '', ?, 'reserved', '{}')",
                (request_id, body_hash, now),
            )
            conn.execute(
                "DELETE FROM aml_request_ledger WHERE updated_at < ?",
                (now - _LEDGER_TTL_SECONDS,),
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM aml_request_ledger").fetchone()[0]
            if count > _LEDGER_MAX_ROWS:
                conn.execute(
                    "DELETE FROM aml_request_ledger WHERE request_id IN ("
                    "SELECT request_id FROM aml_request_ledger "
                    "ORDER BY updated_at ASC LIMIT ?)",
                    (count - _LEDGER_MAX_ROWS,),
                )
            conn.commit()
        return "new", None
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise
    finally:
        conn.close()


def _ledger_save_phase(request_id: str, body_hash: str, phase: str,
                       checkpoint: Optional[Dict[str, Any]] = None,
                       status_code: Optional[int] = None,
                       response: Optional[Dict[str, Any]] = None) -> None:
    """Persist the request phase/checkpoint.  Small, bounded SQLite write."""
    conn = _ledger_connect()
    try:
        with _LEDGER_LOCK:
            sets = ["phase = ?", "updated_at = ?"]
            params: List[Any] = [phase, time.time()]
            if checkpoint is not None:
                sets.append("checkpoint = ?")
                params.append(json.dumps(checkpoint, ensure_ascii=False))
            if status_code is not None:
                sets.append("status_code = ?")
                params.append(int(status_code))
            if response is not None:
                sets.append("response_json = ?")
                params.append(json.dumps(response, ensure_ascii=False))
            params.extend([request_id, body_hash])
            conn.execute(
                f"UPDATE aml_request_ledger SET {', '.join(sets)} "
                "WHERE request_id = ? AND body_hash = ?",
                tuple(params),
            )
            conn.commit()
    finally:
        conn.close()


def _ledger_mark_planned(request_id: str, body_hash: str, total: int) -> None:
    _ledger_save_phase(
        request_id, body_hash, "planned",
        {"total": int(total), "completed": 0})


def _ledger_mark_writing(request_id: str, body_hash: str,
                         completed: int, total: int) -> None:
    _ledger_save_phase(
        request_id, body_hash, "writing",
        {"total": int(total), "completed": int(completed)})


def _ledger_mark_retryable_partial(request_id: str, body_hash: str,
                                   completed: int, total: int,
                                   detail: str) -> None:
    _ledger_save_phase(
        request_id, body_hash, "retryable_partial",
        {"total": int(total), "completed": int(completed),
         "detail": str(detail)[:500]})


def _ledger_finalize(request_id: str, body_hash: str, status_code: int,
                     response: Dict[str, Any]) -> None:
    _ledger_save_phase(
        request_id, body_hash, "done",
        {"completed": None}, status_code=status_code, response=response)


def _ledger_abort(request_id: str, body_hash: str) -> None:
    """Delete only zero-side-effect reservations.

    A request that reached the write phase must never use this path; it keeps
    its checkpoint so a retry can resume rather than replay committed work.
    """
    conn = _ledger_connect()
    try:
        with _LEDGER_LOCK:
            row = conn.execute(
                "SELECT status_code, checkpoint FROM aml_request_ledger "
                "WHERE request_id = ? AND body_hash = ?",
                (request_id, body_hash),
            ).fetchone()
            if row is None:
                return
            if int(row[0] or 0) != 0:
                return
            checkpoint = _ledger_parse_checkpoint(row[1])
            if int(checkpoint.get("completed", 0) or 0) > 0:
                return
            conn.execute(
                "DELETE FROM aml_request_ledger "
                "WHERE request_id = ? AND body_hash = ? AND status_code = 0",
                (request_id, body_hash),
            )
            conn.commit()
    finally:
        conn.close()


def _ledger_response(row) -> JSONResponse:
    return JSONResponse(json.loads(row[3]), status_code=int(row[2]))


# ---- version / commit ------------------------------------------------------

_COMMIT_CACHE: Optional[str] = None


def _git_commit() -> str:
    global _COMMIT_CACHE
    if _COMMIT_CACHE is not None:
        return _COMMIT_CACHE
    env_commit = os.environ.get("AML_GIT_COMMIT", "").strip()
    if env_commit:
        _COMMIT_CACHE = env_commit
        return _COMMIT_CACHE
    try:
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        proc = subprocess.run(
            ["git", "-C", repo_root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=2.0, check=False,
        )
        commit = proc.stdout.strip() if proc.returncode == 0 else ""
        _COMMIT_CACHE = commit or "unknown"
    except Exception:
        _COMMIT_CACHE = "unknown"
    return _COMMIT_CACHE


# ---- AML endpoints ---------------------------------------------------------

@mcp.custom_route("/add", methods=["POST"])
async def aml_add(request: Request) -> JSONResponse:
    """AML Add: bounded admission/parse, then bounded-worker execution.

    The event loop only reads/admits bytes and parses JSON.  Blocking
    storage+embedding work runs in the worker pool; health is independent.
    """
    if not _check_auth(request):
        return _bad(401, "unauthorized")

    deadline = time.monotonic() + AML_REQUEST_BUDGET_S
    body, err, admitted_bytes = await _prepare_json_body(request, deadline)
    if err is not None:
        return err
    assert body is not None
    try:
        return await _run_in_worker(_aml_add_sync, body, deadline)
    finally:
        await _ADMISSION.release(admitted_bytes)

def _aml_add_sync(body: Dict[str, Any], deadline: float) -> JSONResponse:
    """Per-user serialisation wrapper for the blocking Add body."""
    user_id = _require_str(body, "user_id") or ""
    with _user_guard(user_id):
        return _aml_add_sync_locked(body, deadline)


def _aml_add_sync_locked(body: Dict[str, Any], deadline: float) -> JSONResponse:
    """Blocking Add body: validate → two-phase plan → checkpointed write."""
    request_id = _require_str(body, "request_id")
    user_id = _require_str(body, "user_id")
    session_id = _require_str(body, "session_id")
    if not request_id or not user_id or not session_id:
        return _bad(400, "request_id / user_id / session_id are required strings")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _validation_error("messages must be a non-empty array")

    # Validate every message before any write, so one bad message cannot leave
    # a partially-written request behind.
    parsed_messages: List[Tuple[str, str]] = []
    message_date_prefixes: List[str] = []  # L1: 每条消息的事件日期前缀
    image_events_all: List[Dict[str, Any]] = []
    media_counts = _empty_image_counts()
    cumulative_image_bytes = 0
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return _validation_error(f"messages[{idx}] must be an object")
        role = msg.get("role")
        # Spec line 1440: "The message role is user or assistant."
        if not isinstance(role, str) or role.strip() not in ("user", "assistant"):
            return _validation_error(
                f"messages[{idx}].role must be 'user' or 'assistant'")
        texts, image_events, err = _scan_content_parts(
            msg.get("content"), f"messages[{idx}].content")
        if err:
            return _validation_error(err)
        annotated, counts = _apply_image_limits(
            image_events, cumulative_image_bytes)
        cumulative_image_bytes = counts["cumulative_bytes"]
        for key in ("accepted", "oversize", "aggregate", "invalid"):
            media_counts[key] += counts[key]
        media_counts["cumulative_bytes"] = cumulative_image_bytes
        image_events_all.extend(annotated)
        date_prefix = _event_date_prefix(msg.get("timestamp"))  # L1
        for text in texts:
            parsed_messages.append((role.strip(), text))
            message_date_prefixes.append(date_prefix)

    _log_multimodal_counts(
        media_counts["accepted"],
        media_counts["oversize"] + media_counts["aggregate"]
        + media_counts["invalid"],
    )

    body_hash = _body_hash(body)
    try:
        state, row = _ledger_begin(request_id, body_hash)
    except Exception as e:
        return _bad(500, f"idempotency ledger unavailable: {e}")

    if state == "conflict":
        return _bad(409, "request_id already used with a different request body")
    if state == "busy":
        return _bad(409, "request_id is currently being processed; retry the same body")
    if state == "replay":
        return _ledger_response(row)

    resume_completed = 0
    checkpoint: Dict[str, Any] = {}
    if state == "resume":
        checkpoint = _ledger_parse_checkpoint(row[6] if len(row) > 6 else "{}")
        resume_completed = max(0, int(checkpoint.get("completed", 0) or 0))

    # Flatten into (role, fragment) pairs exactly like the old per-fragment loop.
    fragments: List[Tuple[str, str]] = []
    if parsed_messages:
        for _mi, (role, text) in enumerate(parsed_messages):
            _prefix = message_date_prefixes[_mi] if _mi < len(message_date_prefixes) else ""
            _prefix = _with_role_label(_prefix, role)
            for frag in _split_fragments(text):
                fragments.append((role, _prefix + frag))
    total_fragments = len(fragments)
    # A legal image-only Add persists one placeholder record.
    effective_total = total_fragments if parsed_messages else 1
    if state == "resume":
        checkpoint_total = max(0, int(checkpoint.get("total", 0) or 0))
        if checkpoint_total:
            effective_total = checkpoint_total
    resume_completed = min(resume_completed, effective_total)

    # Review §4.2: a fully committed request must finalize/replay without
    # touching _get_client(), Phase A, or an embedding service that may be down.
    if state == "resume" and resume_completed >= effective_total:
        response = {
            "success": True,
            "request_id": request_id,
            "user_id": user_id,
            "session_id": session_id,
        }
        try:
            _ledger_finalize(request_id, body_hash, 200, response)
        except Exception as e:
            logger.error("AML resume finalize failed: %s", e)
        return JSONResponse(response, status_code=200)

    try:
        _check_deadline(deadline)
    except _BudgetExceeded as e:
        if resume_completed == 0:
            _ledger_abort(request_id, body_hash)
        _p0_inc("budget_503")
        return _bad(503, str(e))

    client = _get_client()
    if client is None:
        if resume_completed == 0:
            _ledger_abort(request_id, body_hash)
        return _bad(500, "storage backend unavailable (embedding service down?)")

    write_results: List[Dict[str, Any]] = []
    try:
        if not parsed_messages:
            # Image-only request: run the same two-phase plan/execute path so
            # its placeholder embedding is warmed before any write.
            _check_deadline(deadline)
            placeholder = _placeholder_content(session_id, image_events_all)
            remaining = [("image-only", placeholder)] if resume_completed == 0 else []
            plans = _precompute_add_plans(client, remaining, user_id, deadline)
            if resume_completed == 0:
                _ledger_mark_planned(request_id, body_hash, 1)
                _ledger_mark_writing(request_id, body_hash, 0, 1)
                plan = plans[0] if plans else {
                    "action": "insert", "text": placeholder}
                result = _execute_plan(
                    client, plan, placeholder, user_id, source="image-only")
                status = str(result.get("status", "error"))
                if status in ("stale", "filtered"):
                    result = {"status": "error",
                              "detail": f"placeholder metadata record was "
                                        f"{status}: {result.get('detail', '')}"}
                if result.get("status") == "error":
                    _ledger_abort(request_id, body_hash)
                    return _bad(500, f"placeholder write failed: "
                                     f"{result.get('detail', result)}")
                write_results.append(result)
                _ledger_save_phase(
                    request_id, body_hash, "writing",
                    {"total": 1, "completed": 1})
        else:
            # Phase A: no writes.  Only remaining fragments are precomputed.
            remaining = fragments[resume_completed:]
            plans = _precompute_add_plans(client, remaining, user_id, deadline)
            if len(plans) != len(remaining):
                raise RuntimeError(
                    f"plan count mismatch: {len(plans)} != {len(remaining)}")
            if resume_completed == 0:
                _ledger_mark_planned(request_id, body_hash, total_fragments)
            _ledger_mark_writing(
                request_id, body_hash, resume_completed, total_fragments)
            # Phase B: short write phase; no budget checks (safe checkpointed
            # resume if a storage error aborts it).  Reuse the Phase A plans.
            for offset, ((role, frag), plan) in enumerate(
                    zip(remaining, plans)):
                idx = resume_completed + offset
                result = _execute_plan(
                    client, plan, frag, user_id, source=role)
                if result.get("status") == "error":
                    completed = idx
                    if completed > 0:
                        _ledger_mark_retryable_partial(
                            request_id, body_hash, completed,
                            total_fragments, str(result.get("detail", result)))
                        return _bad(503, f"write failed; retry to resume: "
                                         f"{result.get('detail', result)}")
                    _ledger_abort(request_id, body_hash)
                    return _bad(500, f"write failed: "
                                     f"{result.get('detail', result)}")
                write_results.append(result)
                _ledger_mark_writing(
                    request_id, body_hash, idx + 1, total_fragments)
    except _BudgetExceeded as e:
        # Pre-write phase only; safe to abort/replay.
        if resume_completed == 0:
            _ledger_abort(request_id, body_hash)
        _p0_inc("budget_503")
        return _bad(503, str(e))
    except _EmbeddingUnavailable as e:
        if resume_completed == 0:
            _ledger_abort(request_id, body_hash)
        _p0_inc("embedding_unavailable_503")
        return _bad(503, f"embedding service unavailable: {e}")
    except StorageBusyError as e:
        completed = resume_completed + len(write_results)
        if completed > 0:
            _ledger_mark_retryable_partial(
                request_id, body_hash, completed, total_fragments, str(e))
            _p0_inc("storage_busy_503")
            return _bad(503, f"storage busy; retry to resume: {e}")
        if resume_completed == 0:
            _ledger_abort(request_id, body_hash)
        _p0_inc("storage_busy_503")
        return _bad(503, f"storage busy: {e}")
    except Exception as e:
        completed = resume_completed + len(write_results)
        if completed > 0:
            _ledger_mark_retryable_partial(
                request_id, body_hash, completed, total_fragments, str(e))
            return _bad(503, f"write interrupted; retry to resume: {e}")
        if resume_completed == 0:
            _ledger_abort(request_id, body_hash)
        return _bad(500, f"write failed: {e}")

    # I-1: policy filtering is not a malformed request.  A legal Add returns
    # 200 success=true even when governance writes zero memories.
    _record_governance(request_id, parsed_messages, write_results,
                       media_counts)

    response = {
        "success": True,
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
    }
    try:
        _ledger_finalize(request_id, body_hash, 200, response)
    except Exception as e:
        logger.error("AML request_id ledger finalize failed: %s", e)
    return JSONResponse(response, status_code=200)


@mcp.custom_route("/search", methods=["POST"])
async def aml_search(request: Request) -> JSONResponse:
    """AML Search: bounded admission/parse, then bounded-worker execution."""
    if not _check_auth(request):
        return _bad(401, "unauthorized")

    deadline = time.monotonic() + AML_REQUEST_BUDGET_S
    body, err, admitted_bytes = await _prepare_json_body(request, deadline)
    if err is not None:
        return err
    assert body is not None
    try:
        return await _run_in_worker(_aml_search_sync, body, deadline)
    finally:
        await _ADMISSION.release(admitted_bytes)

def _aml_search_sync(body: Dict[str, Any], deadline: float) -> JSONResponse:
    """Per-user serialisation wrapper for the blocking Search body."""
    user_id = _require_str(body, "user_id") or ""
    with _user_guard(user_id):
        return _aml_search_sync_locked(body, deadline)


def _aml_search_sync_locked(body: Dict[str, Any], deadline: float) -> JSONResponse:
    """Blocking Search body."""
    user_id = _require_str(body, "user_id")
    if not user_id:
        return _bad(400, "query / user_id are required strings")

    query_texts, img_count, oversize_count, qerr = _extract_texts(
        body.get("query"), "query")
    if qerr:
        return _validation_error(qerr)
    _log_multimodal_counts(img_count, oversize_count)
    query = " ".join(query_texts)

    # Spec §05: top_k is required; the response count must never exceed it.
    if "top_k" not in body:
        return _validation_error("top_k is required and must be an integer")
    raw_top_k = body["top_k"]
    if isinstance(raw_top_k, bool) or not isinstance(raw_top_k, int):
        return _validation_error("top_k must be an integer")
    if raw_top_k < 0:
        return _validation_error("top_k must be a non-negative integer")
    top_k = min(raw_top_k, _MAX_TOP_K)
    if top_k == 0:
        return JSONResponse({"data": []}, status_code=200)
    if not query_texts:
        return JSONResponse({"data": []}, status_code=200)

    try:
        _check_deadline(deadline)
    except _BudgetExceeded as e:
        _p0_inc("budget_503")
        return _bad(503, str(e))
    client = _get_client()
    if client is None:
        return _bad(500, "storage backend unavailable (embedding service down?)")

    pool_k = _search_candidate_pool_size(top_k)
    try:
        primary_results = client.recall_results(
            query, top_k=pool_k, author_id=user_id, bump=False)
        _check_deadline(deadline)
        # E1: 每路召回独立保存为有序列表；路数硬上限 3
        # （原 query / 实词 keyword / query+options 拼接）。
        paths: List[List[Dict[str, Any]]] = [list(primary_results or [])]
        if _RECALL_MULTI_QUERY:
            _kw = _keyword_query(query)
            if _kw and _kw != (query or "").strip().lower():
                paths.append(list(client.recall_results(
                    _kw, top_k=pool_k, author_id=user_id, bump=False) or []))
                _check_deadline(deadline)
        options = body.get("options")
        if isinstance(options, list) and options:
            opt_text = " ".join(str(o) for o in options if isinstance(o, str))
            if opt_text:
                # Phase 0a fix: append, never reassign -- otherwise the
                # multi-query keyword path above is silently discarded.
                paths.append(list(client.recall_results(
                    query + " " + opt_text, top_k=pool_k,
                    author_id=user_id, bump=False) or []))
                _check_deadline(deadline)
        paths = paths[:3]
        main_path_count = len(paths)

        if _RERANK_LEXICAL and _RERANK_TOKEN_ROUTES > 0:
            # E3 字面扩路：多 token 查询的存储层相关性门禁会漏掉只含
            # 单个问题实词的金标依赖；单 token 召回门禁更宽，再交给
            # dense/keyword/fts 归一化重排与跨路 RRF。
            for tok in _select_literal_token_queries(
                    query, _RERANK_TOKEN_ROUTES):
                if tok == (query or "").strip().lower():
                    continue
                token_results = client.recall_results(
                    tok, top_k=min(pool_k, _RERANK_TOKEN_POOL_K),
                    author_id=user_id, bump=False)
                paths.append(list(token_results or []))
                _check_deadline(deadline)

        safe_paths = [_sanitize_candidates(path) for path in paths]
        if _RERANK_LEXICAL:
            # E3: 先按 dense/keyword/fts 归一化加权分 × 既有衰减，对每路
            # 内部重排；再交给 E1 的跨路 RRF。RERANK=off 时逐字节现状。
            safe_paths = [_rerank_path_lexical(path) for path in safe_paths]

        if _RECALL_FUSION_ON:
            lane_weights = (
                _search_path_weights(safe_paths, main_path_count)
                if _RERANK_LEXICAL else None)
            fused = _rrf_fuse(
                safe_paths, k=_RRF_K,
                min_dense=_RECALL_FUSION_MIN_DENSE,
                path_weights=lane_weights)
            for cand in fused:
                # 保留既有衰减语义：复用 core.decay._apply_decay，
                # final_score = RRF 分 × 0.5^(days/90)。原始 dense 分
                # 留在 _dense_score_raw 以便诊断/回滚比较。
                cand["_dense_score_raw"] = cand.get("dense_score", 0.0)
                cand["dense_score"] = _finite_number(
                    cand.get("rrf_score"), 0.0)
            results = _apply_decay(fused)
        else:
            # 默认路径：与改前逐字节一致的「并集去重 + sanitize + decay」；
            # E3 开启但未开 RRF 时，对并集做同一套字面加权 × 衰减排序。
            candidates = _dedup_candidates(
                [c for path in safe_paths for c in path])
            if _RERANK_LEXICAL:
                results = _rerank_path_lexical(candidates)
            else:
                results = _apply_decay(candidates)

        results = results[:top_k]
    except _BudgetExceeded as e:
        _p0_inc("budget_503")
        return _bad(503, str(e))
    except StorageBusyError as e:
        _p0_inc("storage_busy_503")
        return _bad(503, f"storage busy: {e}")
    except Exception as e:
        return _bad(500, f"recall failed: {e}")

    data = []
    for r in results:
        if not isinstance(r, dict):
            continue
        raw_content = r.get("content")
        content = raw_content.strip() if isinstance(raw_content, str) else ""
        rid = r.get("id")
        if not content or not rid:
            continue
        score = _finite_number(
            r.get("final_score", r.get("dense_score", 0.0)), 0.0)
        data.append({
            "id": str(rid),
            "content": content,
            "score": round(score, 6),
            "created_at": _iso_z(r.get("timestamp")),
        })
    data = data[:top_k]
    data, dropped = _bounded_search_data(data, _SEARCH_RESPONSE_MAX_BYTES)
    if dropped:
        _p0_inc("search_response_truncated", 1)
        logger.warning(
            "AML search response truncated: dropped %d tail candidate(s), "
            "kept %d, byte_limit=%d",
            dropped, len(data), _SEARCH_RESPONSE_MAX_BYTES)
    return JSONResponse({"data": data}, status_code=200)


def _ollama_ps_status(model: str) -> Dict[str, Any]:
    """Read `ollama ps` (read-only) and classify GPU residency for *model*."""
    if not model:
        return {"resident": None, "detail": "model_env_unset", "checked_at": time.time()}
    bin_path = os.environ.get("OLLAMA_BIN", "").strip()
    if not bin_path:
        candidate = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "ollama-bin", "ollama")
        bin_path = candidate if os.path.exists(candidate) else "ollama"
    try:
        proc = subprocess.run(
            [bin_path, "ps"], capture_output=True, text=True,
            timeout=3.0, check=False)
        output = (proc.stdout or "") + "\n" + (proc.stderr or "")
    except Exception as e:
        return {"resident": None, "detail": f"ollama_ps_failed: {e}",
                "checked_at": time.time()}
    if proc.returncode != 0:
        return {"resident": None,
                "detail": f"ollama_ps_exit_{proc.returncode}",
                "checked_at": time.time()}
    target = model.strip()
    for line in output.splitlines():
        if target and target not in line:
            # ollama ps may display "model:latest" while env has bare tag
            if not target.startswith(line.split()[0] if line.split() else ""):
                continue
        lowered = line.lower()
        if "100% gpu" in lowered:
            return {"resident": True, "detail": "100% GPU",
                    "checked_at": time.time(), "line": line.strip()}
        if "cpu/gpu" in lowered or "gpu" in lowered:
            return {"resident": False, "detail": line.strip(),
                    "checked_at": time.time(), "line": line.strip()}
    return {"resident": False,
            "detail": "model not resident in ollama ps (will load on first call)",
            "checked_at": time.time()}


def _run_startup_gpu_selfcheck() -> None:
    """Warn/degrade in logs+health if the configured model is not 100% GPU."""
    if not AML_STARTUP_SELF_CHECK:
        return
    model = os.environ.get(
        "MEMORYCORE_EMBED_MODEL",
        os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "")).strip()
    status = _ollama_ps_status(model)
    with _HEALTH_LOCK:
        _GPU_STARTUP_STATUS.update(status)
        _GPU_STARTUP_STATUS["model"] = model
    if status.get("resident") is not True:
        logger.warning(
            "AML GPU self-check: model=%r is not 100%% GPU (%s). "
            "Embedding will still be used, but throughput may degrade and "
            "/health is marked degraded until the model is resident on GPU. "
            "Hint: set MEMORYCORE_EMBED_MODEL / MNEMOSYNE_EMBEDDING_MODEL=%s.",
            model, status.get("detail"), model or "qwen3-embedding-aml-ctx1024")
        _health_set(embedding_gpu_resident=status.get("resident"),
                    embedding_gpu_status=status.get("detail", "unknown"),
                    capacity_warning=("embedding model not GPU-resident "
                                      "(loads on first call): "
                                      + str(status.get("detail", "unknown"))))
    else:
        _health_set(embedding_gpu_resident=True,
                    embedding_gpu_status="100% GPU")


@mcp.custom_route("/health", methods=["GET"])
async def aml_health(request: Request) -> JSONResponse:
    """AML Health: fast memory-snapshot only (never touches SQLite/embedding)."""
    payload = _health_snapshot()
    # 2026-09-19: status 只反映真故障（存储不可用 / 数据目录身份不一致 / 探测异常）；
    # 模型未驻留是容量提示，见 payload["capacity_warning"]（规范：health 返回 2xx 即为正常）。
    return JSONResponse(payload, status_code=200)


def main() -> None:
    # T3: fail closed before binding the port when the operator did not set
    # the data directory explicitly.
    try:
        _validate_data_dir()
    except RuntimeError as e:
        sys.stderr.write(f"[memorycore] 启动失败: {e}\n")
        raise SystemExit(2)
    # Precompute commit/subprocess once; /health must never spawn git.
    _git_commit()
    _start_health_probe_thread()
    _run_startup_gpu_selfcheck()
    host = os.environ.get("AML_HOST", "0.0.0.0")
    port = int(os.environ.get("AML_PORT", "8000"))
    import uvicorn
    uvicorn.run(mcp.streamable_http_app(), host=host, port=port)


if __name__ == "__main__":
    main()
