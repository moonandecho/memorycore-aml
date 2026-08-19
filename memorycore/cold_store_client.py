#!/usr/bin/env python3
"""cold_store_client.py — dual-backend client for the cold memory tier

Two backends, same interface (remember/recall/update/forget/stats):
  - LocalBackend:  in-process mnemosyne-memory (SQLite, zero external services)
  - RemoteBackend: MCP streamable-http client (original behaviour)

Selection: env MEMORYCORE_COLD_BACKEND, default "local".

Multi-tenant isolation (per-user engines):
  remember/recall/update/forget accept optional identity filters
  (author_id / author_type / channel_id / source / from_date / to_date).
  - LocalBackend maps author_id to a per-user mnemosyne engine
    (session_id = "user:<author_id>") so writes are session-scoped per user
    and recall filters author_id in SQL (vector + FTS + fallback paths).
  - RemoteBackend passes the same kwargs through to the remote MCP tools.
  All parameters default to None → existing single-tenant behaviour
  is unchanged.
"""
import json
import os
import threading
import urllib.error
from typing import Any, Dict, List, Optional

from .core.config import COLD_BACKEND, MNEMOSYNE_URL  # noqa: E402

TIMEOUT = 10.0


# ═══════════════════════════════════════════════════════════════════════════
# LocalBackend — in-process mnemosyne-memory library
# ═══════════════════════════════════════════════════════════════════════════

class LocalBackend:
    """Cold-tier backend backed by the mnemosyne-memory in-process library.

    Requires ollama with a qwen3 embedding model (or any OpenAI-compatible
    embedding API).  The embedding API is configured via environment
    variables set by core/config.py:

      MEMORYCORE_EMBED_URL   — default http://localhost:11434/v1
      MEMORYCORE_EMBED_MODEL — default qwen3-embedding:0.6b (1024-dim)

    These feed MNEMOSYNE_EMBEDDING_API_URL / MNEMOSYNE_EMBEDDING_MODEL
    which the mnemosyne library reads natively.

    Per-user identity isolation (author_id):
      The shared engine (session "memorycore") stays the default for
      single-tenant usage.  When author_id is given, writes go through a
      lazily-created per-user engine whose session_id is "user:<author_id>"
      — mnemosyne dedups exact content per (session_id, content), so a
      per-user session keeps the dedup scope per user.  Recall runs on the
      shared engine with the author_id SQL filter, which mnemosyne applies
      across sessions (vector + FTS + fallback paths).
    """

    _USER_SESSION_PREFIX = "user:"

    def __init__(self):
        from mnemosyne import Mnemosyne  # noqa: E402
        try:
            self._engine = Mnemosyne(session_id="memorycore")
        except Exception as e:
            raise RuntimeError(
                "Failed to initialize local cold-store backend.\n"
                "MemoryCore now requires ollama with a qwen3 embedding model.\n"
                "Install:  curl -fsSL https://ollama.com/install.sh | sh\n"
                "Pull:     ollama pull qwen3-embedding:0.6b\n"
                "Start:    ollama serve\n"
                "Or set MEMORYCORE_EMBED_URL / MEMORYCORE_EMBED_MODEL for a compatible API.\n"
                f"Embedding URL: {os.environ.get('MNEMOSYNE_EMBEDDING_API_URL', 'not set')}\n"
                f"Original error: {e}"
            )

        # Probe the embedding API to prevent silent zero-vector writes.
        # Mnemosyne's __init__ only sets up SQLite — it won't fail if the
        # embedding endpoint is unreachable.  Without this probe, remember()
        # would return "stored" while generating no actual vector, corrupting
        # the cold tier silently.
        try:
            from mnemosyne.core import embeddings as _emb  # noqa: E402
        except ImportError:
            raise RuntimeError(
                "mnemosyne embedding module not available — MemoryCore cannot "
                "store or recall memories without vector embeddings.\n"
                "Make sure mnemosyne-memory is installed:\n"
                "  pip install mnemosyne-memory"
            )

        if not _emb.available():
            embed_url = os.environ.get("MNEMOSYNE_EMBEDDING_API_URL", "not set")
            embed_model = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "not set")
            raise RuntimeError(
                "No embedding backend available — MemoryCore cannot store or "
                "recall memories without vector embeddings.\n"
                f"  API URL:   {embed_url}\n"
                f"  Model:     {embed_model}\n\n"
                "Make sure ollama is running and the model is pulled:\n"
                "  ollama serve\n"
                "  ollama pull qwen3-embedding:0.6b\n\n"
                "Or configure a compatible embedding API:\n"
                "  export MEMORYCORE_EMBED_URL=https://your-api/v1\n"
                "  export MEMORYCORE_EMBED_MODEL=your-model"
            )

        try:
            probe_vec = _emb.embed_query("MemoryCore embedding probe")
        except Exception as e:
            embed_url = os.environ.get("MNEMOSYNE_EMBEDDING_API_URL", "not set")
            embed_model = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "not set")
            raise RuntimeError(
                "Embedding API is unreachable — MemoryCore cannot store or "
                "recall memories without vector embeddings.\n"
                f"  API URL:   {embed_url}\n"
                f"  Model:     {embed_model}\n\n"
                "Make sure ollama is running and the model is pulled:\n"
                "  ollama serve\n"
                "  ollama pull qwen3-embedding:0.6b\n\n"
                "Or configure a compatible embedding API:\n"
                "  export MEMORYCORE_EMBED_URL=https://your-api/v1\n"
                "  export MEMORYCORE_EMBED_MODEL=your-model\n\n"
                f"Original error: {e}"
            ) from e

        # Double-check: a reachable API that returns zero/empty vectors
        # (e.g. wrong model name) is also a misconfiguration.
        if probe_vec is None or (hasattr(probe_vec, '__len__') and len(probe_vec) == 0):
            embed_url = os.environ.get("MNEMOSYNE_EMBEDDING_API_URL", "not set")
            embed_model = os.environ.get("MNEMOSYNE_EMBEDDING_MODEL", "not set")
            raise RuntimeError(
                "Embedding API returned an empty vector for the probe.\n"
                f"  API URL:   {embed_url}\n"
                f"  Model:     {embed_model}\n"
                "The model may not be pulled in ollama, or the API returned "
                "a zero vector.\n"
                "Check:  ollama list | grep qwen3-embedding\n"
                "Pull:   ollama pull qwen3-embedding:0.6b"
            )

        # per-user engines: key = (author_id, author_type, channel_id)
        self._user_engines: Dict[tuple, Any] = {}
        self._user_lock = threading.Lock()

    def _engine_for(self, author_id: str, author_type: Optional[str] = None,
                    channel_id: Optional[str] = None):
        """Return the per-user mnemosyne engine for a user_id.

        The engine's session_id is "user:<author_id>" so exact-content
        dedup (mnemosyne scopes dedup by session_id + content) never
        crosses users.  The engine's author_id is stamped onto every
        row it writes, and recall's author_id filter finds those rows
        regardless of session.
        """
        key = (author_id or "", author_type or "", channel_id or "")
        with self._user_lock:
            eng = self._user_engines.get(key)
            if eng is None:
                from mnemosyne import Mnemosyne  # noqa: E402
                eng = Mnemosyne(
                    session_id=f"{self._USER_SESSION_PREFIX}{author_id}",
                    author_id=author_id,
                    author_type=author_type or "agent",
                    channel_id=channel_id or "user",
                )
                self._user_engines[key] = eng
            return eng

    # -- remember -------------------------------------------------------

    def remember(self, content: str, importance: float = 0.8,
                 scope: str = "global",
                 author_id: Optional[str] = None,
                 author_type: Optional[str] = None,
                 channel_id: Optional[str] = None,
                 source: Optional[str] = None,
                 from_date: Optional[str] = None,
                 to_date: Optional[str] = None) -> Dict[str, Any]:
        """Store a memory. Returns {status, memory_id} matching remote.

        author_id (per-user isolation): when set, the write goes through
        a per-user engine (session "user:<author_id>", author_id stamped).
        source tags the origin (e.g. message role in multi-user ingestion).
        """
        if author_id:
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            engine = self._engine
        kwargs: Dict[str, Any] = {}
        if source is not None:
            kwargs["source"] = source
        memory_id = engine.remember(
            content,
            importance=importance,
            scope=scope,
            **kwargs,
        )
        if memory_id is None:
            return {"status": "filtered",
                    "detail": "content rejected by write classifier"}
        return {"status": "stored", "memory_id": memory_id}

    # -- recall ---------------------------------------------------------

    def recall(self, query: str, top_k: int = 5,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None,
               source: Optional[str] = None,
               from_date: Optional[str] = None,
               to_date: Optional[str] = None) -> Dict[str, Any]:
        """Semantic recall. Returns {status, results: [...]} matching remote.

        Each result dict carries the same keys the remote backend returns:
        id, content, dense_score, keyword_score, fts_score, importance.

        Identity filters (author_id / author_type / channel_id / source /
        from_date / to_date) are passed to the shared engine's SQL layer —
        author-only recall spans sessions but never crosses authors.
        """
        kwargs: Dict[str, Any] = {}
        if author_id is not None:
            kwargs["author_id"] = author_id
        if author_type is not None:
            kwargs["author_type"] = author_type
        if channel_id is not None:
            kwargs["channel_id"] = channel_id
        if source is not None:
            kwargs["source"] = source
        if from_date is not None:
            kwargs["from_date"] = from_date
        if to_date is not None:
            kwargs["to_date"] = to_date
        items = self._engine.recall(query, top_k=top_k, **kwargs)
        results = []
        for it in items:
            results.append({
                "id": it.get("id", ""),
                "content": it.get("content", "")[:500],
                "dense_score": round(it.get("dense_score", 0.0), 4),
                "keyword_score": round(it.get("keyword_score", 0.0), 4),
                "fts_score": round(it.get("fts_score", 0.0), 4),
                "importance": it.get("importance", 0.5),
                # 透传治理字段 (decay/遗忘依赖; 缺失时上层降级用 timestamp)
                "timestamp": it.get("timestamp"),
                "last_recalled": it.get("last_recalled"),
            })
        return {"status": "ok", "results": results}

    def recall_results(self, query: str, top_k: int = 5,
                       author_id: Optional[str] = None,
                       author_type: Optional[str] = None,
                       channel_id: Optional[str] = None,
                       source: Optional[str] = None,
                       from_date: Optional[str] = None,
                       to_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Convenience: return just the results list (parsed, same shape
        as ColdStoreClient.recall_results for remote)."""
        raw = self.recall(query, top_k=top_k,
                          author_id=author_id, author_type=author_type,
                          channel_id=channel_id, source=source,
                          from_date=from_date, to_date=to_date)
        return raw.get("results", [])

    # -- update ---------------------------------------------------------

    def update(self, memory_id: str, content: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        """Update by ID. Returns {status, memory_id} matching remote.

        With author_id: routes to the per-user engine so the update hits
        the row's session scope ("user:<author_id>").
        """
        if author_id:
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            engine = self._engine
        ok = engine.update(memory_id, content=content)
        if ok:
            return {"status": "updated", "memory_id": memory_id}
        return {"status": "error",
                "detail": f"update failed for {memory_id}"}

    # -- forget ---------------------------------------------------------

    def forget(self, memory_id: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        """Delete by ID. Returns {status, memory_id} matching remote."""
        if author_id:
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            engine = self._engine
        ok = engine.forget(memory_id)
        if ok:
            return {"status": "deleted", "memory_id": memory_id}
        return {"status": "error",
                "detail": f"forget failed for {memory_id}"}

    # -- stats ----------------------------------------------------------

    def stats(self, all_sessions: bool = False) -> Dict[str, Any]:
        """Cold-tier statistics. Returns {total, embeddings, ...} matching remote.

        Queries SQLite directly for accurate counts:
          - total       = COUNT(*) FROM working_memory
          - embeddings  = COUNT(*) FROM vec_working_rowids (shadow table,
                           readable without the sqlite-vec extension)

        all_sessions=True counts across every session (multi-tenant
        capacity gate); the default counts the shared "memorycore" session
        only, matching the original single-tenant behaviour.

        Avoids the library's get_stats() which double-counts
        (legacy memories + BEAM working) and misses vec_working.
        """
        conn = self._engine.conn  # has sqlite-vec loaded
        if all_sessions:
            total = conn.execute(
                "SELECT COUNT(*) FROM working_memory"
            ).fetchone()[0]
        else:
            total = conn.execute(
                "SELECT COUNT(*) FROM working_memory"
                " WHERE session_id = ?",
                ("memorycore",),
            ).fetchone()[0]

        embeddings = 0
        try:
            embeddings = conn.execute(
                "SELECT COUNT(*) FROM vec_working_rowids"
            ).fetchone()[0]
        except Exception:
            pass  # shadow table may not exist yet

        ep_total = 0
        if all_sessions:
            ep_total = conn.execute(
                "SELECT COUNT(*) FROM episodic_memory"
            ).fetchone()[0]
        else:
            ep_total = conn.execute(
                "SELECT COUNT(*) FROM episodic_memory"
                " WHERE session_id = ?",
                ("memorycore",),
            ).fetchone()[0]

        return {
            "total": total,
            "embeddings": embeddings,
            "database": str(self._engine.db_path),
            "mode": "beam",
            "beam": {
                "working_memory": {
                    "total": total,
                    "consolidated": 0,
                    "unconsolidated": total,
                    "last": None,
                },
                "episodic_memory": {
                    "total": ep_total,
                    "last": None,
                    "vectors": embeddings,
                    "vec_type": "sqlite-vec",
                },
            },
        }

    # -- list_all (optional, not in core 5-method contract) -------------

    def list_all(self) -> List[Dict[str, Any]]:
        """List all memories (both working + episodic)."""
        all_mems = self._engine.get_all_memories()
        return [dict(m) for m in all_mems]


# ═══════════════════════════════════════════════════════════════════════════
# RemoteBackend — original MCP streamable-http client (unchanged logic)
# ═══════════════════════════════════════════════════════════════════════════

def _parse_sse(body: str) -> Dict[str, Any]:
    """Parse streamable-http SSE response (event: message / data: {...})."""
    for line in body.splitlines():
        if line.startswith("data:"):
            return json.loads(line[5:].strip())
    # Fallback: plain JSON
    return json.loads(body)


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    """Drop None values so remote tool calls only carry set filters."""
    return {k: v for k, v in d.items() if v is not None}


class RemoteBackend:
    """Cold-tier client over MCP streamable-http.

    Each call initializes a session (small overhead); on 4xx responses
    it clears the session and reconnects.
    """

    def __init__(self, url: str = MNEMOSYNE_URL, timeout: float = TIMEOUT):
        if not url:
            raise ValueError(
                "MNEMOSYNE_URL is required when MEMORYCORE_COLD_BACKEND=remote"
            )
        self.url = url
        self.timeout = timeout
        self._session_id: Optional[str] = None
        self._rpc_id = 0

    def _headers(self) -> Dict[str, str]:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            h["mcp-session-id"] = self._session_id
        return h

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        import urllib.request

        req = urllib.request.Request(
            self.url,
            data=json.dumps(payload).encode(),
            headers=self._headers(),
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            body = resp.read().decode()
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self._session_id = sid
        return _parse_sse(body)

    def _initialize(self) -> None:
        resp = self._post({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "memorycore", "version": "0.1.0"},
            },
        })
        # Notify server (optional, best-effort)
        try:
            self._post({
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            })
        except Exception:
            pass
        return resp

    def _call_tool(self, name: str,
                   arguments: Dict[str, Any]) -> Dict[str, Any]:
        if not self._session_id:
            self._initialize()
        self._rpc_id += 1
        payload = {
            "jsonrpc": "2.0",
            "id": self._rpc_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
        try:
            resp = self._post(payload)
        except urllib.error.HTTPError as e:
            # Server restart invalidates old session → 4xx.
            # Clear session, re-initialize, retry once.
            if e.code in (400, 401, 404) and self._session_id:
                self._session_id = None
                self._initialize()
                resp = self._post(payload)
            else:
                raise
        if "error" in resp:
            raise RuntimeError(
                f"cold tier tool {name} error: {resp['error']}"
            )
        content = resp.get("result", {}).get("content", [])
        text = content[0]["text"] if content else "{}"
        try:
            return json.loads(text)
        except Exception:
            try:
                import ast
                return ast.literal_eval(text)
            except Exception:
                return {"raw": text}

    # -- 5-method interface --------------------------------------------

    def remember(self, content: str, importance: float = 0.8,
                 scope: str = "global",
                 author_id: Optional[str] = None,
                 author_type: Optional[str] = None,
                 channel_id: Optional[str] = None,
                 source: Optional[str] = None,
                 from_date: Optional[str] = None,
                 to_date: Optional[str] = None) -> Dict[str, Any]:
        args = _drop_none({
            "content": content,
            "importance": importance,
            "scope": scope,
            "author_id": author_id,
            "author_type": author_type,
            "channel_id": channel_id,
            "source": source,
            "from_date": from_date,
            "to_date": to_date,
        })
        return self._call_tool("remember", args)

    def recall(self, query: str, top_k: int = 5,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None,
               source: Optional[str] = None,
               from_date: Optional[str] = None,
               to_date: Optional[str] = None) -> Dict[str, Any]:
        args = _drop_none({
            "query": query, "top_k": top_k,
            "author_id": author_id,
            "author_type": author_type,
            "channel_id": channel_id,
            "source": source,
            "from_date": from_date,
            "to_date": to_date,
        })
        return self._call_tool("recall", args)

    def recall_results(self, query: str, top_k: int = 5,
                       author_id: Optional[str] = None,
                       author_type: Optional[str] = None,
                       channel_id: Optional[str] = None,
                       source: Optional[str] = None,
                       from_date: Optional[str] = None,
                       to_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """Parse recall response into structured list.

        Returns [{id, content, dense_score, keyword_score, fts_score,
                  importance}, ...]
        """
        raw = self.recall(query, top_k=top_k,
                          author_id=author_id, author_type=author_type,
                          channel_id=channel_id, source=source,
                          from_date=from_date, to_date=to_date)

        # _call_tool already tried json.loads + ast.literal_eval
        if "raw" in raw and len(raw) == 1:
            text = raw["raw"]
            data = None
        else:
            data = raw
            text = None

        if data is None and text:
            try:
                data = json.loads(text)
            except Exception:
                try:
                    import ast
                    data = ast.literal_eval(text)
                except Exception:
                    pass

        if isinstance(data, dict) and data.get("status") == "ok":
            results = data.get("results", [])
            return [dict(r) for r in results]
        return []

    def update(self, memory_id: str, content: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        return self._call_tool("update", _drop_none({
            "memory_id": memory_id, "content": content,
            "author_id": author_id,
            "author_type": author_type,
            "channel_id": channel_id,
        }))

    def forget(self, memory_id: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        return self._call_tool("forget", _drop_none({
            "memory_id": memory_id,
            "author_id": author_id,
            "author_type": author_type,
            "channel_id": channel_id,
        }))

    def stats(self, all_sessions: bool = False) -> Dict[str, Any]:
        return self._call_tool("stats", _drop_none({
            "all_sessions": all_sessions,
        }))

    def list_all(self) -> List[Dict[str, Any]]:
        """Try list_all / get_all; raise AttributeError if unavailable.

        Paginated loop (limit=500, offset++) with seen_ids dedup — mirrors
        production client. If the server returns total and we have consumed
        it, stop; if a page returns fewer than limit, it is the last page.
        """
        try:
            all_results = []
            seen_ids = set()
            offset = 0
            limit = 500
            while True:
                resp = self._call_tool("list_all", {"limit": limit, "offset": offset})
                items = []
                total = 0
                if isinstance(resp, list):
                    items = resp
                elif isinstance(resp, dict):
                    items = resp.get("results", resp.get("items", []))
                    total = resp.get("total", 0)
                if not items:
                    break
                for item in items:
                    iid = item.get("id", "")
                    if iid and iid not in seen_ids:
                        seen_ids.add(iid)
                        all_results.append(item)
                offset += len(items)
                if total > 0 and offset >= total:
                    break
                if len(items) < limit:
                    break
            return all_results
        except Exception:
            try:
                resp = self._call_tool("get_all", {})
                if isinstance(resp, list):
                    return resp
                if isinstance(resp, dict):
                    return resp.get("results", resp.get("items", []))
                return []
            except Exception:
                raise AttributeError("list_all unavailable")


# ═══════════════════════════════════════════════════════════════════════════
# ColdStoreClient — factory that picks backend based on env
# ═══════════════════════════════════════════════════════════════════════════

class ColdStoreClient:
    """Cold-tier client factory.

    Usage (identical regardless of backend):

        client = ColdStoreClient()
        client.remember("some fact")
        client.recall("query")
        client.stats()

    Backend selection via env MEMORYCORE_COLD_BACKEND:
      - "local"  (default) → LocalBackend  (mnemosyne-memory in-process)
      - "remote"            → RemoteBackend (MCP streamable-http)

    Per-user identity filters (author_id / author_type / channel_id / source /
    from_date / to_date) are accepted by every data method and default to
    None → single-tenant behaviour unchanged.
    """

    def __init__(self, url: str = None, timeout: float = TIMEOUT):
        backend = COLD_BACKEND
        if backend == "remote":
            remote_url = url or MNEMOSYNE_URL
            self._backend = RemoteBackend(url=remote_url, timeout=timeout)
        else:
            self._backend = LocalBackend()

    # Delegate all 5 methods -------------------------------------------

    def remember(self, content: str, importance: float = 0.8,
                 scope: str = "global",
                 author_id: Optional[str] = None,
                 author_type: Optional[str] = None,
                 channel_id: Optional[str] = None,
                 source: Optional[str] = None,
                 from_date: Optional[str] = None,
                 to_date: Optional[str] = None) -> Dict[str, Any]:
        return self._backend.remember(
            content, importance=importance, scope=scope,
            author_id=author_id, author_type=author_type,
            channel_id=channel_id, source=source,
            from_date=from_date, to_date=to_date,
        )

    def recall(self, query: str, top_k: int = 5,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None,
               source: Optional[str] = None,
               from_date: Optional[str] = None,
               to_date: Optional[str] = None) -> Dict[str, Any]:
        return self._backend.recall(
            query, top_k=top_k,
            author_id=author_id, author_type=author_type,
            channel_id=channel_id, source=source,
            from_date=from_date, to_date=to_date,
        )

    def recall_results(self, query: str, top_k: int = 5,
                       author_id: Optional[str] = None,
                       author_type: Optional[str] = None,
                       channel_id: Optional[str] = None,
                       source: Optional[str] = None,
                       from_date: Optional[str] = None,
                       to_date: Optional[str] = None) -> List[Dict[str, Any]]:
        return self._backend.recall_results(
            query, top_k=top_k,
            author_id=author_id, author_type=author_type,
            channel_id=channel_id, source=source,
            from_date=from_date, to_date=to_date,
        )

    def update(self, memory_id: str, content: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        return self._backend.update(memory_id, content,
                                    author_id=author_id,
                                    author_type=author_type,
                                    channel_id=channel_id)

    def forget(self, memory_id: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        return self._backend.forget(memory_id,
                                    author_id=author_id,
                                    author_type=author_type,
                                    channel_id=channel_id)

    def stats(self, all_sessions: bool = False) -> Dict[str, Any]:
        return self._backend.stats(all_sessions=all_sessions)

    def list_all(self) -> List[Dict[str, Any]]:
        return self._backend.list_all()


# -- CLI quick-test --------------------------------------------------------

if __name__ == "__main__":
    import sys

    c = ColdStoreClient()
    if len(sys.argv) > 1 and sys.argv[1] == "recall":
        q = sys.argv[2] if len(sys.argv) > 2 else "服务器网络"
        print(json.dumps(c.recall(q, top_k=3),
                         ensure_ascii=False, indent=2))
    else:
        print(json.dumps(c.stats(), ensure_ascii=False, indent=2))
