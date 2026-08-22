#!/usr/bin/env python3
"""aml_server.py — AML (Agent Memory Leaderboard) HTTP adapter for MemoryCore

差异化路线：记忆治理在线化（ActiveMemoryIndex 式路线，MemoryCore 自己的治理机制）。

HTTP endpoints (plain REST, mounted on FastMCP streamable-http app):
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
import json
import os
import re
import secrets
import threading
from datetime import datetime, timezone
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from .cold_store_client import ColdStoreClient  # noqa: E402
from .core.classifier import classify, STALE  # noqa: E402
from .core.overflow import _find_best_match, _merge_two_entries  # noqa: E402
from .core.decay import _apply_decay  # noqa: E402
from .core.config import COLD_SOFT_LIMIT, COLD_HARD_LIMIT  # noqa: E402

# ---- AML write-side constants (governance online) -------------------------

_FACT_IMPORTANCE = 0.6     # 事实默认重要度: 不触发热关键词 → 冷层
_DEDUP_RECALL_TOP_K = 5    # 写入前查重召回数 (比 overflow 的 3 更宽, 候选池更全)
_MAX_FRAGMENT_CHARS = 300  # 长消息按句切分后的片段上限
_SENTENCE_SPLIT_RE = re.compile(r"[。！？；;\n]")
_MAX_TOP_K = 100           # AML 协议固定 top_k 上限

AML_API_KEY = os.environ.get("AML_API_KEY", "").strip()

mcp = FastMCP("memorycore-aml")

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
    return JSONResponse({"detail": detail}, status_code=status)


def _require_str(body: Dict[str, Any], field: str) -> Optional[str]:
    v = body.get(field)
    if v is None or not isinstance(v, str) or not v.strip():
        return None
    return v


# ---- AML endpoints ---------------------------------------------------------

@mcp.custom_route("/add", methods=["POST"])
async def aml_add(request: Request) -> JSONResponse:
    """AML Add: ingest one source conversation (session) into the cold tier."""
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
    messages = body.get("messages")
    if not request_id or not user_id or not session_id:
        return _bad(400, "request_id / user_id / session_id are required strings")
    if not isinstance(messages, list) or not messages:
        return _bad(400, "messages must be a non-empty array")

    client = _get_client()
    if client is None:
        return _bad(500, "storage backend unavailable (embedding service down?)")

    total_fragments = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        role = msg.get("role")
        if not isinstance(content, str) or not content.strip():
            continue
        source = role if isinstance(role, str) and role else "conversation"
        for frag in _split_fragments(content):
            try:
                client_result = _store_fragment(client, frag, user_id,
                                                source=source)
                total_fragments += 1
            except Exception as e:
                return _bad(500, f"write failed: {e}")

    return JSONResponse({
        "success": True,
        "request_id": request_id,
        "user_id": user_id,
        "session_id": session_id,
    }, status_code=200)


@mcp.custom_route("/search", methods=["POST"])
async def aml_search(request: Request) -> JSONResponse:
    """AML Search: author-scoped recall, decay-ranked, AML response format."""
    if not _check_auth(request):
        return _bad(401, "unauthorized")
    try:
        body = await request.json()
    except Exception:
        return _bad(400, "invalid JSON body")
    if not isinstance(body, dict):
        return _bad(400, "body must be a JSON object")

    query = _require_str(body, "query")
    user_id = _require_str(body, "user_id")
    if not query or not user_id:
        return _bad(400, "query / user_id are required strings")

    top_k = body.get("top_k", _MAX_TOP_K)
    try:
        top_k = int(top_k)
    except (TypeError, ValueError):
        return _bad(400, "top_k must be an integer")
    top_k = max(1, min(top_k, _MAX_TOP_K))

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
        results = _apply_decay(results)
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
    return JSONResponse({"data": data}, status_code=200)


@mcp.custom_route("/health", methods=["GET"])
async def aml_health(request: Request) -> JSONResponse:
    """AML Health: 2xx = alive. Reports storage state without failing."""
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
    return JSONResponse({"status": state, **detail}, status_code=200)


def main() -> None:
    host = os.environ.get("AML_HOST", "0.0.0.0")
    port = int(os.environ.get("AML_PORT", "8000"))
    import uvicorn
    uvicorn.run(mcp.streamable_http_app(), host=host, port=port)


if __name__ == "__main__":
    main()
