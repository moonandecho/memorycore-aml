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
import hashlib
import json
import logging
import os
import re
import secrets
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

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

from .cold_store_client import ColdStoreClient  # noqa: E402
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
_MAX_TOP_K = 100           # AML 协议固定 top_k 上限
_MAX_IMAGE_DECODED_BYTES = 10 * 1024 * 1024  # spec: 10 MiB decoded per image
_SUPPORTED_IMAGE_MIMES = {"jpeg", "jpg", "png", "webp"}

# request_id idempotency ledger bounds (persistent SQLite, restart-safe)
_LEDGER_MAX_ROWS = 5000
_LEDGER_TTL_SECONDS = 30 * 24 * 3600
_LEDGER_INFLIGHT_STALE_SECONDS = 300
_LEDGER_LOCK = threading.Lock()

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


def _get_client() -> Optional[ColdStoreClient]:
    global _client, _client_error
    with _client_lock:
        if _client is None:
            try:
                _client = ColdStoreClient()
                _client_error = None
            except Exception as e:  # embedding unreachable etc.
                _client_error = str(e)
                return None
        return _client


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


def _split_fragments(content: str, max_chars: int = _MAX_FRAGMENT_CHARS) -> List[str]:
    """Split one message into fact fragments at sentence boundaries.

    Short messages stay whole; long messages are split on sentence
    terminators and re-packed into chunks ≤ max_chars so each fragment is
    a self-contained factual unit for dedup/merge.
    """
    text = (content or "").strip()
    if not text:
        return []
    if len(text) <= max_chars:
        return [text]
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    if not parts:
        return [text]
    frags: List[str] = []
    buf = ""
    for p in parts:
        if buf and len(buf) + len(p) + 1 > max_chars:
            frags.append(buf)
            buf = p
        else:
            buf = f"{buf}。{p}" if buf else p
    if buf:
        frags.append(buf)
    return frags or [text]


def _dedup_recall(client: ColdStoreClient, content: str,
                  user_id: str) -> List[Dict[str, Any]]:
    """Author-scoped dedup recall (isolation hard constraint).

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
    return client.recall_results(query, top_k=_DEDUP_RECALL_TOP_K,
                                 author_id=user_id)


def _aml_match_level(content: str, cand_content: str, dense: float) -> Optional[str]:
    """AML 侧合并门禁 (比 overflow._find_best_match 的 level 更严)。

    实测: qwen3 对同词汇域中文短句的 dense 分数普遍虚高 (0.9+),
    两个无关事实 ("缓存方案" vs "部署方案") 也能拿到 0.95 ——
    语义信号不可靠, 以字面相似度为主信号:
      ratio >= 0.75                → "same"    (几乎逐字相同, 跳过)
      ratio >= 0.45 且 dense >= 0.5 → "similar" (同主题不同细节, 合并)
      否则                          → None      (不匹配, 新写)

    注意不能直接用 _find_best_match 的 level: 它的 combined>=0.8 门限会把
    "方案A" 与 "方案A改为B" 判成 same 而跳过, 丢失更新。
    """
    ratio = SequenceMatcher(None, content, cand_content).ratio()
    if ratio >= 0.75:
        return "same"
    if ratio >= 0.45 and (dense or 0) >= 0.50:
        return "similar"
    return None


def _store_fragment(client: ColdStoreClient, content: str, user_id: str,
                    source: str = "conversation") -> Dict[str, Any]:
    """Write one fact fragment with online governance (差异化核心).

    1. stale filter  — 过时状态记录 (≤80字含"已修复"式标记) 不写入
    2. semantic dedup/merge — 同一事实跳过; 相似事实合并进同一条
       ("方案A" + "方案A改为B" → 一条, 不是两条)
    3. remember with author_id = user_id (隔离硬约束)
    """
    decision = classify(content, importance=_FACT_IMPORTANCE)
    if decision["decision"] == STALE:
        return {"status": "stale", "detail": decision.get("reason", "")}

    try:
        existing = _dedup_recall(client, content, user_id)
    except Exception:
        existing = []

    if existing:
        # 复用 _find_best_match 选候选 (combined 打分), level 用 AML 侧严门禁重判
        matched = _find_best_match(content, existing)
        if matched:
            level = _aml_match_level(content, matched["content"],
                                     matched.get("dense_score", 0))
            if level == "same":
                return {"status": "duplicate", "memory_id": matched["id"]}
            if level == "similar":
                merged = _merge_two_entries(content, matched["content"])
                if isinstance(merged, str) and merged.strip() != matched["content"].strip():
                    try:
                        r = client.update(matched["id"], merged, author_id=user_id)
                        if r.get("status") == "updated":
                            return {"status": "updated",
                                    "memory_id": matched["id"],
                                    "detail": "merged into existing entry"}
                    except Exception:
                        pass  # update failed → fall through to fresh write

    r = client.remember(content, importance=_FACT_IMPORTANCE, scope="global",
                        author_id=user_id, source=source)
    if r.get("status") == "stored":
        return {"status": "stored", "memory_id": r.get("memory_id")}
    if r.get("status") == "filtered":
        return {"status": "filtered", "detail": r.get("detail", "")}
    return {"status": "error", "detail": str(r)}


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


def _estimate_inline_image_bytes(url: Any) -> Optional[int]:
    """Approximate decoded bytes for an inline data:image/...;base64 URL.

    Returns None when the URL is not an accepted inline image.  Base64 is never
    decoded, so an oversize image part cannot trigger a decode/OOM.
    """
    if not isinstance(url, str) or len(url) < 12:
        return None
    if url[:11].lower() != "data:image/":
        return None
    sep = url.find(";base64,")
    if sep < 0:
        return None
    mime = url[11:sep].strip().lower()
    if mime not in _SUPPORTED_IMAGE_MIMES:
        return None
    payload_start = sep + len(";base64,")
    payload_len = len(url) - payload_start
    if payload_len <= 0:
        return None
    # 3 bytes per 4 base64 chars, minus padding.  No decode / no payload copy.
    padding = url.count("=", payload_start)
    return max((payload_len * 3) // 4 - padding, 0)


def _extract_texts(content: Any, where: str) -> Tuple[List[str], int, int, Optional[str]]:
    """Extract ordered text parts from Add/Search content (string or array).

    Returns (texts, image_count, oversize_image_count, error_reason).
    Image parts are counted only; no visual processing, no original image
    retention.  Images estimated to decode above 10 MiB are skipped while all
    text parts are preserved.
    """
    if isinstance(content, str):
        text = content.strip()
        if not text:
            return [], 0, 0, f"{where} must be a non-empty string or content array"
        return [text], 0, 0, None

    if not isinstance(content, list) or not content:
        return [], 0, 0, f"{where} must be a non-empty string or content array"

    texts: List[str] = []
    image_count = 0
    oversize_count = 0
    for idx, part in enumerate(content):
        if not isinstance(part, dict):
            return [], image_count, oversize_count, f"{where}[{idx}] must be an object"
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str) or not text.strip():
                return [], image_count, oversize_count, (
                    f"{where}[{idx}].text must be a non-empty string")
            texts.append(text.strip())
        elif part_type == "image_url":
            image = part.get("image_url")
            if not isinstance(image, dict):
                return [], image_count, oversize_count, (
                    f"{where}[{idx}].image_url must be an object")
            size = _estimate_inline_image_bytes(image.get("url"))
            if size is None:
                return [], image_count, oversize_count, (
                    f"{where}[{idx}].image_url.url must be an inline "
                    "data:image/...;base64 URL")
            if size > _MAX_IMAGE_DECODED_BYTES:
                oversize_count += 1
            else:
                image_count += 1
        else:
            return [], image_count, oversize_count, (
                f"{where}[{idx}].type must be 'text' or 'image_url'")
    # Spec ContentPart[]: every part must have a valid type; type=text must
    # be non-empty.  There is no requirement that the array contain a text
    # part, so an image-only array is valid input.  Callers decide what a
    # textless request means (Add: valid/no write; Search: valid/no results).
    return texts, image_count, oversize_count, None


def _log_multimodal_counts(image_count: int, oversize_count: int) -> None:
    """One aggregate count log per request; payload bytes are never logged."""
    if image_count or oversize_count:
        logger.info(
            "AML multimodal content: image_parts=%d oversize_skipped=%d "
            "(no original image / no OCR / no image evidence returned)",
            image_count, oversize_count,
        )


def _governance_snapshot() -> Dict[str, int]:
    with _GOVERNANCE_LOCK:
        return dict(_GOVERNANCE_COUNTS)


def _record_governance(request_id: str, parsed_messages: List[Tuple[str, str]],
                       write_results: List[Dict[str, Any]],
                       image_count: int, oversize_count: int) -> None:
    """Aggregate policy outcomes for observability, never for the response.

    Governance decisions (stale / filtered / duplicate / stored / updated)
    are successful Add outcomes per fix-round2 I-1 and must not be turned
    into a 4xx contract error.  The request body is never logged.
    """
    statuses = [str(r.get("status", "error")) for r in write_results]
    with _GOVERNANCE_LOCK:
        _GOVERNANCE_COUNTS["requests"] += 1
        _GOVERNANCE_COUNTS["fragments"] += len(statuses)
        _GOVERNANCE_COUNTS["image_parts"] += image_count
        _GOVERNANCE_COUNTS["oversize_image_parts"] += oversize_count
        if not parsed_messages:
            _GOVERNANCE_COUNTS["textless_requests"] += 1
        for status in statuses:
            if status in _GOVERNANCE_COUNTS:
                _GOVERNANCE_COUNTS[status] += 1
    logger.info(
        "AML governance request_id=%s text_parts=%d fragments=%d "
        "statuses=%s textless_images=%d",
        request_id, len(parsed_messages), len(statuses), statuses, image_count,
    )


# ---- request_id idempotency ledger (SQLite, bounded, restart-safe) --------

def _ledger_path() -> str:
    data_dir = os.environ.get("MNEMOSYNE_DATA_DIR") or os.path.expanduser(
        "~/.memorycore/data")
    return os.path.join(data_dir, "aml_request_ledger.sqlite3")


def _ledger_connect() -> "sqlite3.Connection":
    path = _ledger_path()
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA busy_timeout=5000")
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
    return conn


def _body_hash(body: Dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, ensure_ascii=False,
                           separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _ledger_begin(request_id: str, body_hash: str):
    """Reserve/replay a request_id atomically in SQLite.

    Returns (state, row) where state is new/replay/conflict/busy.  A short
    global lock plus BEGIN IMMEDIATE makes check-and-reserve atomic within the
    process (and safe enough for the single-worker service contract).
    """
    conn = _ledger_connect()
    try:
        with _LEDGER_LOCK:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT request_id, body_hash, status_code, response_json, updated_at "
                "FROM aml_request_ledger WHERE request_id = ?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row[1] != body_hash:
                    conn.execute("ROLLBACK")
                    return "conflict", row
                if row[2] and row[3]:
                    conn.execute("ROLLBACK")
                    return "replay", row
                # Crash/stale in-flight reservation: allow a bounded retry.
                if time.time() - float(row[4] or 0) < _LEDGER_INFLIGHT_STALE_SECONDS:
                    conn.execute("ROLLBACK")
                    return "busy", row
                conn.execute(
                    "DELETE FROM aml_request_ledger WHERE request_id = ?",
                    (request_id,),
                )
            now = time.time()
            conn.execute(
                "INSERT OR REPLACE INTO aml_request_ledger "
                "(request_id, body_hash, status_code, response_json, updated_at) "
                "VALUES (?, ?, 0, '', ?)",
                (request_id, body_hash, now),
            )
            # Bound the ledger by TTL, then keep only the most recent N rows.
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


def _ledger_finalize(request_id: str, body_hash: str, status_code: int,
                     response: Dict[str, Any]) -> None:
    conn = _ledger_connect()
    try:
        with _LEDGER_LOCK:
            conn.execute(
                "UPDATE aml_request_ledger SET status_code = ?, "
                "response_json = ?, updated_at = ? "
                "WHERE request_id = ? AND body_hash = ?",
                (status_code, json.dumps(response, ensure_ascii=False),
                 time.time(), request_id, body_hash),
            )
            conn.commit()
    finally:
        conn.close()


def _ledger_abort(request_id: str, body_hash: str) -> None:
    conn = _ledger_connect()
    try:
        with _LEDGER_LOCK:
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
    """AML Add: validate, idempotently ingest, then return success."""
    if not _check_auth(request):
        return _bad(401, "unauthorized")
    try:
        body = await request.json()
    except Exception:
        return _bad(400, "invalid JSON body")
    if not isinstance(body, dict):
        return _bad(400, "body must be a JSON object")

    request_id = _require_str(body, "request_id")
    user_id = _require_str(body, "user_id")
    session_id = _require_str(body, "session_id")
    if not request_id or not user_id or not session_id:
        return _bad(400, "request_id / user_id / session_id are required strings")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return _validation_error("messages must be a non-empty array")

    # validate every message before any write, so one bad message cannot leave
    # a partially-written request behind.
    parsed_messages: List[Tuple[str, str]] = []
    total_images = 0
    total_oversize = 0
    for idx, msg in enumerate(messages):
        if not isinstance(msg, dict):
            return _validation_error(f"messages[{idx}] must be an object")
        role = msg.get("role")
        if not isinstance(role, str) or not role.strip():
            return _validation_error(f"messages[{idx}].role must be a non-empty string")
        texts, img_count, oversize_count, err = _extract_texts(
            msg.get("content"), f"messages[{idx}].content")
        if err:
            return _validation_error(err)
        total_images += img_count
        total_oversize += oversize_count
        for text in texts:
            parsed_messages.append((role.strip(), text))

    _log_multimodal_counts(total_images, total_oversize)

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

    # Image-only (or all-oversize-image) requests are legal but have no text
    # to write; they still go through the idempotency ledger and return 200.
    client = _get_client() if parsed_messages else None
    if parsed_messages and client is None:
        _ledger_abort(request_id, body_hash)
        return _bad(500, "storage backend unavailable (embedding service down?)")

    write_results: List[Dict[str, Any]] = []
    try:
        for role, text in parsed_messages:
            for frag in _split_fragments(text):
                result = _store_fragment(client, frag, user_id, source=role)
                if result.get("status") == "error":
                    _ledger_abort(request_id, body_hash)
                    return _bad(500, f"write failed: {result.get('detail', result)}")
                write_results.append(result)
    except Exception as e:
        _ledger_abort(request_id, body_hash)
        return _bad(500, f"write failed: {e}")

    # I-1: policy filtering is not a malformed request.  A legal Add returns
    # 200 success=true even when governance writes zero memories
    # (stale/filtered/duplicate-only) or the request carried only images.
    _record_governance(request_id, parsed_messages, write_results,
                       total_images, total_oversize)

    response = {
        "success": True,
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
    }
    try:
        _ledger_finalize(request_id, body_hash, 200, response)
    except Exception as e:
        # The memory write is already committed; do not turn it into a retry
        # that may duplicate.  Log the ledger degradation.
        logger.error("AML request_id ledger finalize failed: %s", e)
    return JSONResponse(response, status_code=200)


@mcp.custom_route("/search", methods=["POST"])
async def aml_search(request: Request) -> JSONResponse:
    """AML Search: author-scoped recall, strict top_k, AML response format."""
    if not _check_auth(request):
        return _bad(401, "unauthorized")
    try:
        body = await request.json()
    except Exception:
        return _bad(400, "invalid JSON body")
    if not isinstance(body, dict):
        return _bad(400, "body must be a JSON object")

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
    # Accept only JSON integers.  bool is an int subclass; reject it
    # explicitly.  Floats (including Infinity/NaN), strings, null, missing
    # and negative values are field-validation errors (422), not 500/400.
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

    # Image-only query: legal ContentPart[], but there is no text to search.
    if not query_texts:
        return JSONResponse({"data": []}, status_code=200)

    client = _get_client()
    if client is None:
        return _bad(500, "storage backend unavailable (embedding service down?)")

    try:
        results = client.recall_results(query, top_k=top_k, author_id=user_id)
        # options-aware fallback (单次兑底, 非迭代搜索):
        # 协议示例查询如 "Which answer best matches the memory?" 本身不携带
        # 事实词, 存储层词法门禁会返回空; 把 options 拼进检索查询可找回证据。
        # options 只用于检索上下文, 不写入记忆、不生成答案。
        options = body.get("options")
        if isinstance(options, list) and options:
            opt_text = " ".join(str(o) for o in options if isinstance(o, str))
            if opt_text:
                extra = client.recall_results(
                    query + " " + opt_text, top_k=top_k, author_id=user_id)
                seen = {r.get("id") for r in results}
                for r in extra:
                    if r.get("id") not in seen:
                        results.append(r)
        # Spec: data is a relevance-ordered array; the response order is the
        # evidence-priority order.  Decay the whole candidate pool first
        # (primary recall + options fallback, deduplicated), then cap the
        # result count at top_k.  Capping before decay could return an entry
        # whose reported score is lower than a discarded candidate's score.
        results = _apply_decay(results)
        results = results[:top_k]
    except Exception as e:
        return _bad(500, f"recall failed: {e}")

    data = []
    for r in results:
        content = (r.get("content") or "").strip()
        rid = r.get("id")
        if not content or not rid:
            continue
        score = r.get("final_score", r.get("dense_score", 0))
        try:
            score = round(float(score), 6)
        except (TypeError, ValueError):
            score = 0.0
        data.append({
            "id": str(rid),
            "content": content,
            "score": score,
            "created_at": _iso_z(r.get("timestamp")),
        })
    # Final safety net: response order is retrieval evidence priority order
    # and count is always bounded by top_k.
    data = data[:top_k]
    return JSONResponse({"data": data}, status_code=200)


@mcp.custom_route("/health", methods=["GET"])
async def aml_health(request: Request) -> JSONResponse:
    """AML Health: unauthenticated 2xx, with version/commit for verification."""
    state = "ok"
    detail: Dict[str, Any] = {}
    client = _get_client()
    if client is None:
        state = "degraded"
        detail["storage"] = "unavailable"
    else:
        try:
            detail["storage"] = client.stats(all_sessions=True)
        except Exception:
            state = "degraded"
            detail["storage"] = "unavailable"
    payload = {"status": state, **detail,
               "version": SERVICE_VERSION, "commit": _git_commit(),
               "governance": _governance_snapshot()}
    return JSONResponse(payload, status_code=200)


def main() -> None:
    host = os.environ.get("AML_HOST", "0.0.0.0")
    port = int(os.environ.get("AML_PORT", "8000"))
    import uvicorn
    uvicorn.run(mcp.streamable_http_app(), host=host, port=port)


if __name__ == "__main__":
    main()
