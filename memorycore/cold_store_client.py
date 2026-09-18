#!/usr/bin/env python3
"""cold_store_client.py — dual-backend client for the cold memory tier

Two backends, same interface (remember/recall/update/forget/stats):
  - LocalBackend:  in-process mnemosyne-memory (SQLite, zero external services)
  - RemoteBackend: MCP streamable-http client (original behaviour)

Selection: env MEMORYCORE_COLD_BACKEND, default "local".

Multi-tenant isolation (AML per-user engines):
  remember/recall/update/forget accept optional identity filters
  (author_id / author_type / channel_id / source / from_date / to_date).
  - LocalBackend maps author_id to a per-user mnemosyne engine
    (session_id = "aml:<author_id>") so writes are session-scoped per user
    and recall filters author_id in SQL (vector + FTS + fallback paths).
  - RemoteBackend passes the same kwargs through to the remote MCP tools.
  All parameters default to None → existing single-tenant behaviour
  is unchanged.
"""
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
import urllib.error
from typing import Any, Dict, List, Optional

from .core.config import COLD_BACKEND, MNEMOSYNE_URL  # noqa: E402

TIMEOUT = 10.0


def _env_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default

# ── P0 storage safety (2026-09-19) ──────────────────────────────────────────
# Mnemosyne engines hold long-lived sqlite3 connections.  The shared engine is
# used by recall/dedup for every author, and per-author engines all point at the
# same SQLite file.  A process-wide RLock serialises every engine call; per-
# engine locks keep the same connection from being entered concurrently even if
# a future refactor drops the global lock.  This is deliberately conservative:
# the embedding/HTTP work is warmed out of the lock by the AML adapter, so the
# critical section is only SQLite/enrichment CPU+I/O.
_ENGINE_GLOBAL_LOCK = threading.RLock()


def _path_identity(path: Any) -> Optional[Dict[str, Any]]:
    """Return (st_dev, st_ino) for a path, or None if it does not exist."""
    try:
        st = os.stat(str(path))
        return {"path": str(path), "st_dev": int(st.st_dev),
                "st_ino": int(st.st_ino)}
    except OSError:
        return None


class StorageBusyError(RuntimeError):
    """SQLite stayed locked after bounded retries (mapped to retryable 5xx)."""


def _is_sqlite_locked(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "database is locked" in msg or "database table is locked" in msg or "busy" in msg

# Search response content cap.  The AML text-track evidence is often a long
# conversation/BEAM fragment, so the old 500-character cut dropped answer
# evidence.  Keep a module-level default plus a testable env resolver.
_DEFAULT_CONTENT_MAX_CHARS = 2000


def _get_content_max_chars() -> int:
    """Return the effective recall content cap.

    ``MEMORYCORE_CONTENT_MAX_CHARS`` overrides the default.  Missing values
    use the default; invalid values (non-integer, <= 0, empty) fall back to
    the default with an explicit warning instead of failing silently.
    """
    raw = os.environ.get("MEMORYCORE_CONTENT_MAX_CHARS")
    if raw is None:
        return _DEFAULT_CONTENT_MAX_CHARS
    try:
        value = int(raw.strip())
        if value <= 0:
            raise ValueError("value must be a positive integer")
        return value
    except (TypeError, ValueError):
        logging.getLogger("memorycore.cold_store").warning(
            "Invalid MEMORYCORE_CONTENT_MAX_CHARS=%r; expected a positive "
            "integer; falling back to %d",
            raw,
            _DEFAULT_CONTENT_MAX_CHARS,
        )
        return _DEFAULT_CONTENT_MAX_CHARS


def _hydrate_full_contents(engine: Any, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Restore full stored content for recall rows.

    Some mnemosyne-memory releases apply their own hard ``[:500]`` cut inside
    ``BeamMemory.recall`` before returning rows.  The AML client must surface
    the stored text so the configured content cap, not a hidden engine 500
    cut, is the only limit.  This is best-effort: engines without a readable
    ``conn`` (or test doubles) keep their original payloads.
    """
    if not items:
        return items
    # A 500-char row is the only shape that can have been silently cut by
    # the upstream engine; skip the extra query for ordinary short rows.
    if not any(
        isinstance(it, dict)
        and isinstance(it.get("content"), str)
        and len(it["content"]) >= 500
        for it in items
    ):
        return items

    ids: List[Any] = []
    seen = set()
    for it in items:
        if not isinstance(it, dict):
            continue
        rid = it.get("id")
        if rid is None or rid in seen:
            continue
        seen.add(rid)
        ids.append(rid)
    if not ids:
        return items

    conn = getattr(engine, "conn", None)
    if conn is None:
        return items

    lookup: Dict[Any, str] = {}
    placeholders = ",".join("?" * len(ids))
    for table in ("working_memory", "episodic_memory"):
        try:
            rows = conn.execute(
                f"SELECT id, content FROM {table}"
                f" WHERE id IN ({placeholders})",
                tuple(ids),
            ).fetchall()
        except Exception:
            continue
        for row in rows:
            if row and row[0] not in lookup:
                lookup[row[0]] = row[1]

    if not lookup:
        return items

    hydrated: List[Dict[str, Any]] = []
    for it in items:
        if isinstance(it, dict) and it.get("id") in lookup:
            it = dict(it)
            it["content"] = lookup[it["id"]]
        hydrated.append(it)
    return hydrated


# ═══════════════════════════════════════════════════════════════════════════
# LocalBackend — in-process mnemosyne-memory library
# ═══════════════════════════════════════════════════════════════════════════


# ---- E11 helper (2026-09-12) ------------------------------------------------
_embed_fail_warned = False


def _warn_embed_once(e: Exception) -> None:
    """Warn once per process about embedding failure (rate-limited)."""
    global _embed_fail_warned
    if not _embed_fail_warned:
        _embed_fail_warned = True
        import logging
        logging.getLogger("memorycore.cold_store").warning(
            "EMBED: 冷层向量嵌入失败 / cold-tier vector embedding failed (%s)"
            " → lexical-only recall degradation (further failures silent)", e)

class LocalBackend:
    """Cold-tier backend backed by the mnemosyne-memory in-process library.

    Requires ollama with a qwen3 embedding model (or any OpenAI-compatible
    embedding API).  The embedding API is configured via environment
    variables set by core/config.py:

      MEMORYCORE_EMBED_URL   — default http://localhost:11434/v1
      MEMORYCORE_EMBED_MODEL — default qwen3-embedding:0.6b (1024-dim)

    These feed MNEMOSYNE_EMBEDDING_API_URL / MNEMOSYNE_EMBEDDING_MODEL
    which the mnemosyne library reads natively.

    Per-user identity isolation (author_id) — AML:
      The shared engine (session "memorycore") stays the default for
      single-tenant usage.  When author_id is given, writes go through a
      lazily-created per-user engine whose session_id is "aml:<author_id>"
      — mnemosyne dedups exact content per (session_id, content), so a
      per-user session keeps the dedup scope per user.  Recall runs on the
      shared engine with the author_id SQL filter, which mnemosyne applies
      across sessions (vector + FTS + fallback paths).
    """

    _USER_SESSION_PREFIX = "aml:"

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

        # AML per-user engines: key = (author_id, author_type, channel_id)
        self._aml_engines: Dict[tuple, Any] = {}
        self._aml_engine_locks: Dict[tuple, threading.RLock] = {}
        self._aml_lock = threading.Lock()
        self._engine_lock = threading.RLock()
        self._max_user_engines = _env_int(
            "MNEMOSYNE_MAX_USER_ENGINES", 512, minimum=1)

        # P0.5 data-lifecycle guard (review §3.2/§4.5).  A long-lived
        # sqlite connection to an unlinked DB silently splits Add/Search.
        self._db_path = str(getattr(self._engine, "db_path", ""))
        self._db_identity = _path_identity(self._db_path)
        self._identity_mismatch_count = 0
        self._identity_last_mismatch_at = None
        self._identity_last_before = None

    def _ensure_engine_locks(self) -> None:
        if not hasattr(self, "_engine_lock") or self._engine_lock is None:
            self._engine_lock = threading.RLock()
        if not hasattr(self, "_aml_engine_locks") or self._aml_engine_locks is None:
            self._aml_engine_locks = {}
        if not hasattr(self, "_aml_lock") or self._aml_lock is None:
            self._aml_lock = threading.Lock()

    @staticmethod
    def _close_engine(engine: Any) -> None:
        seen = set()
        for conn in (getattr(engine, "conn", None),
                     getattr(getattr(engine, "beam", None), "conn", None)):
            if conn is None or id(conn) in seen:
                continue
            seen.add(id(conn))
            try:
                conn.close()
            except Exception:
                pass

    def _ensure_data_identity_locked(self):
        """Detect DB path unlink/recreate and rebuild all live engines.

        Returns (current_identity, mismatch_detected).  Must be called while
        holding _ENGINE_GLOBAL_LOCK.
        """
        # Test doubles construct LocalBackend via object.__new__ and only set
        # ``_engine``; initialise the lifecycle fields lazily for them.
        if not hasattr(self, "_db_path"):
            self._db_path = str(getattr(self._engine, "db_path", ""))
            self._db_identity = _path_identity(self._db_path)
            self._identity_mismatch_count = 0
            self._identity_last_mismatch_at = None
            self._identity_last_before = None
            self._aml_engines = getattr(self, "_aml_engines", {})
            self._aml_engine_locks = getattr(self, "_aml_engine_locks", {})
            self._aml_lock = getattr(self, "_aml_lock", threading.Lock())
            self._engine_lock = getattr(self, "_engine_lock", threading.RLock())
            return self._db_identity, False
        current = _path_identity(self._db_path)
        if current == self._db_identity:
            return current, False

        self._identity_mismatch_count += 1
        self._identity_last_mismatch_at = time.time()
        self._identity_last_before = dict(self._db_identity or {})

        for eng in list(self._aml_engines.values()):
            self._close_engine(eng)
        self._close_engine(self._engine)
        self._aml_engines.clear()
        self._aml_engine_locks.clear()

        # mnemosyne memory.py and beam.py each keep their own thread-local
        # connection.  Close alone is not enough: the legacy memory.py
        # _get_connection() trusts a non-None thread-local and would hand the
        # closed connection straight back to init_db().  Drop both caches.
        try:
            from mnemosyne.core import beam as _beam_mod
            from mnemosyne.core import memory as _memory_mod
            for mod in (_memory_mod, _beam_mod):
                tl = getattr(mod, "_thread_local", None)
                if tl is None:
                    continue
                for attr in ("conn", "db_path"):
                    try:
                        delattr(tl, attr)
                    except AttributeError:
                        pass
        except Exception:
            pass

        from mnemosyne import Mnemosyne  # noqa: E402
        self._engine = Mnemosyne(session_id="memorycore")
        self._db_path = str(getattr(self._engine, "db_path", self._db_path))
        self._db_identity = _path_identity(self._db_path)
        return self._db_identity, True

    def check_data_identity(self) -> Dict[str, Any]:
        """Public health/business hook: recover from DB inode split now."""
        with _ENGINE_GLOBAL_LOCK:
            current, mismatch = self._ensure_data_identity_locked()
            return {
                "db_path": self._db_path,
                "db_identity": dict(current or {}),
                "identity_mismatch": bool(
                    mismatch or self._identity_mismatch_count > 0),
                "identity_mismatch_count": int(self._identity_mismatch_count),
                "identity_last_mismatch_at": self._identity_last_mismatch_at,
                "identity_last_before": dict(self._identity_last_before or {}),
            }

    @contextlib.contextmanager
    def _engine_guard(self, key):
        """Serialise engine access: global lock + per-engine lock."""
        self._ensure_engine_locks()
        with _ENGINE_GLOBAL_LOCK:
            self._ensure_data_identity_locked()
            if key is None:
                lock = self._engine_lock
            else:
                with self._aml_lock:
                    lock = self._aml_engine_locks.setdefault(key, threading.RLock())
            with lock:
                yield

    def _engine_call(self, key, fn, *args, **kwargs):
        """Run an engine call under both locks, retrying SQLite busy errors."""
        last_exc = None
        for attempt in range(4):
            try:
                with self._engine_guard(key):
                    # If the identity guard rebuilt engines while we were
                    # waiting, a bound method captured on the old engine
                    # would still point at the unlinked inode.  Rebind by
                    # method name to the current engine under the same lock.
                    target = self._engine if key is None \
                        else self._aml_engines.get(key)
                    fn_name = getattr(fn, "__name__", None)
                    if target is not None and fn_name and hasattr(target, fn_name):
                        return getattr(target, fn_name)(*args, **kwargs)
                    return fn(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                last_exc = exc
                if _is_sqlite_locked(exc) and attempt < 3:
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                raise StorageBusyError(
                    f"storage engine busy after {attempt + 1} attempt(s): {exc}"
                ) from exc
            except Exception as exc:
                # Some mnemosyne versions surface sqlite lock failures as a
                # generic OperationalError subclass or wrapped RuntimeError.
                if _is_sqlite_locked(exc) and attempt < 3:
                    last_exc = exc
                    time.sleep(0.2 * (2 ** attempt))
                    continue
                raise
        if last_exc is not None:
            raise StorageBusyError(f"storage engine busy: {last_exc}") from last_exc

    def _engine_for(self, author_id: str, author_type: Optional[str] = None,
                    channel_id: Optional[str] = None):
        """Return the per-user mnemosyne engine for a user_id.

        The engine's session_id is "aml:<author_id>" so exact-content
        dedup (mnemosyne scopes dedup by session_id + content) never
        crosses users.  The engine's author_id is stamped onto every
        row it writes, and recall's author_id filter finds those rows
        regardless of session.
        """
        key = (author_id or "", author_type or "", channel_id or "")
        # Keep global → per-engine lock ordering consistent with
        # _engine_guard(); otherwise _engine_for and _engine_call can deadlock.
        with _ENGINE_GLOBAL_LOCK:
            self._ensure_data_identity_locked()
            with self._aml_lock:
                eng = self._aml_engines.get(key)
                if eng is None:
                    from mnemosyne import Mnemosyne  # noqa: E402
                    eng = Mnemosyne(
                        session_id=f"{self._USER_SESSION_PREFIX}{author_id}",
                        author_id=author_id,
                        author_type=author_type or "agent",
                        channel_id=channel_id or "aml",
                    )
                    self._aml_engines[key] = eng
                    self._aml_engine_locks[key] = threading.RLock()
                    # Bound the long-evaluation per-user map.  Do not close
                    # evicted engines: a caller that already fetched one may
                    # still hold a bound method; removing map references lets
                    # GC reclaim it after the call.
                    while len(self._aml_engines) > self._max_user_engines:
                        old_key = next(iter(self._aml_engines))
                        if old_key == key:
                            break
                        self._aml_engines.pop(old_key, None)
                        self._aml_engine_locks.pop(old_key, None)
                return eng

    def find_exact(self, content: str,
                   author_id: Optional[str] = None,
                   author_type: Optional[str] = None,
                   channel_id: Optional[str] = None) -> Optional[str]:
        """Return an exact same-content working-memory id, if any.

        This is the crash-window idempotency hook: a committed fragment can be
        absent from recall candidate gates, but a direct SQLite equality lookup
        still sees it before the retry re-executes the write.  It is read-only
        and scoped to the same per-user engine as the write.
        """
        if content is None:
            return None
        if author_id:
            key = (author_id or "", author_type or "", channel_id or "")
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            key = None
            engine = self._engine

        def _lookup():
            conn = getattr(engine, "conn", None)
            if conn is None:
                return None
            try:
                row = conn.execute(
                    "SELECT id FROM working_memory "
                    "WHERE session_id = ? AND content = ? "
                    "AND superseded_by IS NULL "
                    "ORDER BY timestamp DESC LIMIT 1",
                    (engine.session_id, str(content)),
                ).fetchone()
            except Exception:
                return None
            return row[0] if row else None

        return self._engine_call(key, _lookup)

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

        author_id (AML user_id isolation): when set, the write goes through
        a per-user engine (session "aml:<author_id>", author_id stamped).
        source tags the origin (e.g. message role in multi-user ingestion).
        """
        if author_id:
            key = (author_id or "", author_type or "", channel_id or "")
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            key = None
            engine = self._engine
        # Crash-window idempotency: a retry after a committed fragment must
        # not refresh an existing exact row's timestamp/source.  Dedup recall
        # gates can miss it (review probe_ledger_crash), so do a direct exact
        # equality lookup before handing the write to mnemosyne.
        existing_id = self.find_exact(
            content, author_id=author_id, author_type=author_type,
            channel_id=channel_id)
        if existing_id is not None:
            return {"status": "stored", "memory_id": existing_id}
        kwargs: Dict[str, Any] = {}
        if source is not None:
            kwargs["source"] = source
        memory_id = self._engine_call(
            key, engine.remember, content,
            importance=importance, scope=scope, **kwargs)
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
               to_date: Optional[str] = None,
               bump: bool = True) -> Dict[str, Any]:
        """Semantic recall. Returns {status, results: [...]} matching remote.

        bump: True (default) keeps the engine's recall-bump side effects
        (recall_count/last_recalled refresh); False requests a read-only
        recall so governance enumeration/prescreen does not pollute
        last_recalled freshness. On stock mnemosyne-memory builds without
        the bump_recalled kwarg the read-only mode degrades to a plain
        recall (governance still works, freshness tracking only).

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
        def _do_recall():
            if bump:
                raw_items = self._engine.recall(query, top_k=top_k, **kwargs)
            else:
                try:
                    raw_items = self._engine.recall(
                        query, top_k=top_k, bump_recalled=False, **kwargs)
                except TypeError:
                    # stock mnemosyne-memory lacks bump_recalled → degrade
                    raw_items = self._engine.recall(query, top_k=top_k, **kwargs)
            return _hydrate_full_contents(self._engine, raw_items)
        items = self._engine_call(None, _do_recall)
        content_max_chars = _get_content_max_chars()
        results = []
        for it in items:
            results.append({
                "id": it.get("id", ""),
                "content": it.get("content", "")[:content_max_chars],
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
                       to_date: Optional[str] = None,
                       bump: bool = True) -> List[Dict[str, Any]]:
        """Convenience: return just the results list (parsed, same shape
        as ColdStoreClient.recall_results for remote).

        bump=False → read-only recall (see recall())."""
        raw = self.recall(query, top_k=top_k,
                          author_id=author_id, author_type=author_type,
                          channel_id=channel_id, source=source,
                          from_date=from_date, to_date=to_date,
                          bump=bump)
        return raw.get("results", [])

    # -- update ---------------------------------------------------------

    def update(self, memory_id: str, content: str,
               author_id: Optional[str] = None,
               author_type: Optional[str] = None,
               channel_id: Optional[str] = None) -> Dict[str, Any]:
        """Update by ID. Returns {status, memory_id} matching remote.

        With author_id: routes to the per-user engine so the update hits
        the row's session scope ("aml:<author_id>").
        """
        if author_id:
            key = (author_id or "", author_type or "", channel_id or "")
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            key = None
            engine = self._engine
        ok = self._engine_call(key, engine.update, memory_id, content=content)
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
            key = (author_id or "", author_type or "", channel_id or "")
            engine = self._engine_for(author_id, author_type, channel_id)
        else:
            key = None
            engine = self._engine
        ok = self._engine_call(key, engine.forget, memory_id)
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

        all_sessions=True counts across every session (AML multi-tenant
        capacity gate); the default counts the shared "memorycore" session
        only, matching the original single-tenant behaviour.

        Avoids the library's get_stats() which double-counts
        (legacy memories + BEAM working) and misses vec_working.
        """
        def _do_stats():
            conn = self._engine.conn  # has sqlite-vec loaded
            if all_sessions:
                where = ""
                params: tuple = ()
            else:
                where = " WHERE session_id = ?"
                params = ("memorycore",)

            total = conn.execute(
                f"SELECT COUNT(*) FROM working_memory{where}",
                params,
            ).fetchone()[0]

            # P0.5: health must count both vector stores.  vec_working is the
            # primary sqlite-vec shadow table; memory_embeddings is the legacy
            # JSON fallback.  A row is vector-backed if either store has it.
            legacy_ids = set()
            try:
                legacy_ids = {
                    row[0] for row in conn.execute(
                        "SELECT memory_id FROM memory_embeddings")
                }
            except Exception:
                pass
            vec_rowids = set()
            try:
                vec_rowids = {
                    row[0] for row in conn.execute(
                        "SELECT rowid FROM vec_working_rowids")
                }
            except Exception:
                pass

            embeddings = 0
            try:
                wm_rows = conn.execute(
                    f"SELECT id, rowid FROM working_memory{where}",
                    params,
                ).fetchall()
                embeddings = sum(
                    1 for row in wm_rows
                    if row[0] in legacy_ids or row[1] in vec_rowids
                )
            except Exception:
                embeddings = 0

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
                "memory_embeddings": len(legacy_ids),
                "vec_working": len(vec_rowids),
                "database": str(self._engine.db_path),
                "mode": "beam",
                "beam": {
                    "working_memory": {
                        "total": total,
                        "consolidated": 0,
                        "unconsolidated": total,
                        "last": None,
                        "vectors": embeddings,
                    },
                    "episodic_memory": {
                        "total": ep_total,
                        "last": None,
                        "vectors": 0,
                        "vec_type": "sqlite-vec",
                    },
                },
            }
        return self._engine_call(None, _do_stats)

    # -- list_all (optional, not in core 5-method contract) -------------

    def embed_texts(self, texts) -> List[List[float]]:
        """Batch-embed texts via the in-process mnemosyne library.

        Same score space as recall (same model, empty doc prefix).
        Unavailable/failure -> [] (caller degrades to lexical mode).
        """
        if not texts:
            return []
        try:
            from mnemosyne.core import embeddings as _emb  # noqa: E402
            vecs = _emb.embed(list(texts))
            if vecs is None:
                return []
            return [[round(float(x), 6) for x in v] for v in vecs]
        except Exception as e:
            # E11 (2026-09-12): embedding failure is no longer silent —
            # warn once per process (no spam); semantics unchanged (return []
            # → caller degrades to lexical recall).
            _warn_embed_once(e)
            return []

    def embed_queries(self, texts) -> List[List[float]]:
        """Batch-embed query texts (query prefix) and warm the shared cache."""
        if not texts:
            return []
        try:
            from mnemosyne.core import embeddings as _emb  # noqa: E402
            if hasattr(_emb, "embed_queries"):
                vecs = _emb.embed_queries(list(texts))
            else:  # older patch/test double
                vecs = [_emb.embed_query(t) for t in texts]
            if vecs is None:
                return []
            return [[round(float(x), 6) for x in v] for v in vecs]
        except Exception as e:
            _warn_embed_once(e)
            return []

    def prefetch_embeddings(self, kind: str,
                           texts: List[str]) -> List[List[float]]:
        """Explicitly precompute embeddings for Phase A.

        With the P0.5 library patch this populates both the normal LRU (when
        enabled) and a bounded explicit prefetch buffer, so Phase B can still
        reuse vectors even when MNEMOSYNE_EMBED_CACHE_SIZE=0.
        """
        if not texts:
            return []
        try:
            from mnemosyne.core import embeddings as _emb  # noqa: E402
            vecs = None
            fn = getattr(_emb, "prefetch_embeddings", None)
            if callable(fn):
                try:
                    vecs = fn(list(texts), kind=kind)
                except Exception:
                    vecs = None
            if vecs is None or len(vecs) == 0:
                # Compatibility with callers/tests that monkeypatch the legacy
                # batch entry points; retain the returned vectors explicitly
                # so a disabled normal cache still gives write-phase hits.
                if kind == "query" and hasattr(_emb, "embed_queries"):
                    vecs = _emb.embed_queries(list(texts))
                else:
                    vecs = _emb.embed(list(texts))
                prime = getattr(_emb, "prime_embeddings", None)
                if callable(prime) and vecs is not None:
                    prime(list(texts), vecs, kind=kind)
            if vecs is None:
                return []
            return [[round(float(x), 6) for x in v] for v in vecs]
        except Exception as e:
            _warn_embed_once(e)
            return []

    def assert_embeddings_cached(self, kind: str,
                                 texts: List[str]) -> bool:
        """Return True when every text is already available cache-side."""
        if not texts:
            return True
        try:
            from mnemosyne.core import embeddings as _emb  # noqa: E402
            fn = getattr(_emb, "embeddings_cached", None)
            if callable(fn):
                # The write path calls the public embed()/embed_query()
                # functions.  If they were replaced, the prefetch-aware cache
                # check cannot be assumed, so fail closed before any write.
                unsafe = not getattr(getattr(_emb, "embed", None),
                                     "_mnemosyne_prefetch_aware", False)
                return bool(fn(list(texts), kind=kind)) and not unsafe
        except Exception:
            return False
        # Older patch versions lack the probe; do not block writes on it.
        return True

    def list_all(self) -> List[Dict[str, Any]]:
        """List all memories (both working + episodic)."""
        all_mems = self._engine_call(None, self._engine.get_all_memories)
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
        # Review §2.2: _post/_initialize/session-id mutation were unlocked.
        self._lock = threading.RLock()

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

        with self._lock:
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
        with self._lock:
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
               to_date: Optional[str] = None,
               bump: bool = True) -> Dict[str, Any]:
        args = _drop_none({
            "query": query, "top_k": top_k,
            "author_id": author_id,
            "author_type": author_type,
            "channel_id": channel_id,
            "source": source,
            "from_date": from_date,
            "to_date": to_date,
        })
        # Read-only recall: only send the flag when explicitly requested.
        # The default path stays wire-compatible with older mnemosyne MCP
        # servers (bump_recalled defaults to True there anyway).
        if not bump:
            args["bump_recalled"] = False
        return self._call_tool("recall", args)

    def recall_results(self, query: str, top_k: int = 5,
                       author_id: Optional[str] = None,
                       author_type: Optional[str] = None,
                       channel_id: Optional[str] = None,
                       source: Optional[str] = None,
                       from_date: Optional[str] = None,
                       to_date: Optional[str] = None,
                       bump: bool = True) -> List[Dict[str, Any]]:
        """Parse recall response into structured list.

        bump=False → read-only recall (see recall())."

        Returns [{id, content, dense_score, keyword_score, fts_score,
                  importance}, ...]
        """
        raw = self.recall(query, top_k=top_k,
                          author_id=author_id, author_type=author_type,
                          channel_id=channel_id, source=source,
                          from_date=from_date, to_date=to_date,
                          bump=bump)

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

    def embed_texts(self, texts) -> List[List[float]]:
        """Batch-embed texts via the remote MCP service's embed_texts tool.

        Same score space as recall (same model). Unavailable/failure -> []
        (caller degrades to lexical mode).
        """
        if not texts:
            return []
        try:
            raw = self._call_tool("embed_texts", {"texts": list(texts)})
            data = raw.get("raw", raw) if isinstance(raw, dict) else raw
            if not isinstance(data, dict) or data.get("status") != "ok":
                return []
            emb = data.get("embeddings")
            return emb if isinstance(emb, list) else []
        except Exception:
            return []

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

    AML identity filters (author_id / author_type / channel_id / source /
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
               to_date: Optional[str] = None,
               bump: bool = True) -> Dict[str, Any]:
        return self._backend.recall(
            query, top_k=top_k,
            author_id=author_id, author_type=author_type,
            channel_id=channel_id, source=source,
            from_date=from_date, to_date=to_date,
            bump=bump,
        )

    def recall_results(self, query: str, top_k: int = 5,
                       author_id: Optional[str] = None,
                       author_type: Optional[str] = None,
                       channel_id: Optional[str] = None,
                       source: Optional[str] = None,
                       from_date: Optional[str] = None,
                       to_date: Optional[str] = None,
                       bump: bool = True) -> List[Dict[str, Any]]:
        return self._backend.recall_results(
            query, top_k=top_k,
            author_id=author_id, author_type=author_type,
            channel_id=channel_id, source=source,
            from_date=from_date, to_date=to_date,
            bump=bump,
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

    def embed_texts(self, texts) -> List[List[float]]:
        """Batch-embed via the active backend (Phase 4 LRU activity signal)."""
        return self._backend.embed_texts(texts)

    def embed_queries(self, texts) -> List[List[float]]:
        """Batch-embed query texts via the active backend (P0 cache warm)."""
        return self._backend.embed_queries(texts)

    def prefetch_embeddings(self, kind: str,
                            texts: List[str]) -> List[List[float]]:
        """Explicit Phase A warm-up for the active backend (P0.5)."""
        fn = getattr(self._backend, "prefetch_embeddings", None)
        if not callable(fn):
            return self._backend.embed_queries(texts) if kind == "query" \
                else self._backend.embed_texts(texts)
        return fn(kind, texts)

    def assert_embeddings_cached(self, kind: str, texts: List[str]) -> bool:
        """Return True when the active backend has every text cached."""
        fn = getattr(self._backend, "assert_embeddings_cached", None)
        if callable(fn):
            return bool(fn(kind, texts))
        return True

    def check_data_identity(self) -> Dict[str, Any]:
        """Delegate the runtime DB-inode guard to the active backend."""
        fn = getattr(self._backend, "check_data_identity", None)
        if callable(fn):
            return fn()
        return {}

    def find_exact(self, content: str,
                   author_id: Optional[str] = None,
                   author_type: Optional[str] = None,
                   channel_id: Optional[str] = None) -> Optional[str]:
        """Delegate exact-content idempotency lookup when supported."""
        fn = getattr(self._backend, "find_exact", None)
        if not callable(fn):
            return None
        return fn(content, author_id=author_id,
                  author_type=author_type, channel_id=channel_id)


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
