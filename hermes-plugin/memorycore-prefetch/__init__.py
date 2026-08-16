"""memorycore-prefetch — MemoryProvider plugin: single-model (qwen3) dense recall

Dual-role plugin:
1. prefetch (per-turn semantic recall): recalls the cold tier (top-20),
   ranks by dense score, and injects top-5 into context.
   **On by default** — set MEMORYCORE_PREFETCH_ENABLED=0 to disable.
2. on_memory_write mirror (2026-08-03): after every built-in memory tool
   write (add/replace), checks hot-tier usage in real time; triggers
   overflow at >=80% (hard) or >=60% (soft, with 5% hysteresis).
   Overflow runs in a background thread so it never blocks the tool return.
3. on_memory_write direct-write governance (Phase 2, 2026-08-16): right
   after add/replace commits, types the entry (classify_entry_type):
   state-typed content is migrated to the cold tier in the background
   (dedup -> cold write confirmed -> remove from hot; on cold failure the
   entry stays with a state stamp as a 7-day backstop), rule-typed content
   is stamped. Governance core lives in core.metadata.direct_write_govern
   (no duplicated logic) and is independent of the usage thresholds.

2026-08-11: Single-model qwen3-embedding-ctx256 architecture (no reranker).
All reranker/bge-m3 code has been removed.

Also provides a static cold-store index block via system_prompt_block()
that guides the model to use on-demand recall (memorycore_recall tool).
This index block is always active regardless of the prefetch switch.

Implements the Hermes MemoryProvider ABC: is_available -> True,
get_tool_schemas -> [] (no tools injected).

Activation (Hermes Agent):
    hermes config set memory.provider memorycore-prefetch
    # and copy/link this directory into ~/.hermes/plugins/memorycore-prefetch/

This plugin depends on the Hermes Agent runtime (agent.memory_provider).
It is a Hermes-specific integration; the MemoryCore server itself stays
client-agnostic.

Prerequisites:
    ollama with qwen3-embedding:0.6b (or set MEMORYCORE_EMBED_URL/MODEL).
    When ollama is unreachable, prefetch silently returns empty — no errors.
"""

import json
import logging
import os
import queue
import statistics
import sys
import tempfile
import threading
import urllib.request
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)

# Use the open-source memorycore package (pip-installed origin-memorycore).
# Fallback: repo root two levels up (development mode, not pip-installed).
try:
    from memorycore.cold_store_client import ColdStoreClient  # noqa: E402
    from memorycore.local_store import LocalStore  # noqa: E402
    from memorycore.core.config import (  # noqa: E402
        SOFT_THRESHOLD,
        HARD_THRESHOLD,
        CHAR_LIMIT_MEMORY,
        CHAR_LIMIT_USER,
    )
    from memorycore.core.overflow import run_overflow  # noqa: E402
    from memorycore.core.decay import _apply_decay  # noqa: E402
    from memorycore.core.metadata import direct_write_govern  # noqa: E402
except ImportError:
    _REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    sys.path.insert(0, _REPO_ROOT)
    from memorycore.cold_store_client import ColdStoreClient  # noqa: E402
    from memorycore.local_store import LocalStore  # noqa: E402
    from memorycore.core.config import (  # noqa: E402
        SOFT_THRESHOLD,
        HARD_THRESHOLD,
        CHAR_LIMIT_MEMORY,
        CHAR_LIMIT_USER,
    )
    from memorycore.core.overflow import run_overflow  # noqa: E402
    from memorycore.core.decay import _apply_decay  # noqa: E402
    from memorycore.core.metadata import direct_write_govern  # noqa: E402

_RECALL_CANDIDATES = 20    # first-stage recall candidates (dense-only, 2026-08-11)
_INJECT_TOP_N = 5          # max injected after dense ranking
_PREFETCH_TIMEOUT = 5.0    # prefetch-specific timeout, shorter than default 10s
_QUERY_MAX_LEN = 1000      # recall query max chars

# --- ollama embedding config ---
_EMBED_URL = os.environ.get("MEMORYCORE_EMBED_URL", "http://localhost:11434/v1")
_EMBED_MODEL = os.environ.get("MEMORYCORE_EMBED_MODEL", "qwen3-embedding:0.6b")

# --- ollama availability probe (once, cached) ---
_ollama_available = None


def _probe_ollama() -> bool:
    """Probe the ollama embedding API once; cache the result.

    Returns True if the embedding endpoint responds within 3s.
    On failure logs DEBUG and returns False — prefetch skips silently.
    """
    global _ollama_available
    if _ollama_available is not None:
        return _ollama_available
    try:
        url = _EMBED_URL.rstrip("/") + "/embeddings"
        body = json.dumps({"model": _EMBED_MODEL, "input": ["test"]}).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=3.0)
        _ollama_available = True
        logger.debug("ollama embedding API reachable at %s", _EMBED_URL)
        return True
    except Exception as e:
        _ollama_available = False
        logger.debug("ollama embedding API unreachable at %s: %s", _EMBED_URL, e)
        return False


# --- on_memory_write auto-overflow (2026-08-03) ---
_OVERFLOW_RETRY_DELTA = 5  # soft trigger must see >=5% growth since last overflow

# --- dynamic baseline (single-model, 2026-08-11) ---
_BASELINE_INIT = 0.70        # initial baseline (calibrated on bge; qwen3 scores lower)
_BASELINE_WINDOW = 200       # rolling sample window
_BASELINE_RECALC_EVERY = 50  # recompute median every N new samples
# baseline persistence file
_BASELINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")


class MemoryCorePrefetchProvider(MemoryProvider):
    name = "memorycore-prefetch"

    def __init__(self):
        super().__init__()
        # dynamic baseline state
        self._baseline_lock = threading.Lock()
        self._baseline_samples: List[float] = []
        self._baseline_value: float = _BASELINE_INIT
        self._sample_count_since_recalc: int = 0
        self._load_baseline()
        # injection dedup state (2026-08-05): per-session id set + hot-tier full text
        self._injected_ids: set = set()
        self._hot_text: str = ""
        # on_memory_write auto-overflow state
        self._overflow_lock = threading.Lock()
        self._overflow_thread: Optional[threading.Thread] = None
        self._last_overflow_pct: Dict[str, int] = {}
        # Phase 2 (2026-08-16): direct-write governance state — single-flight:
        # one worker thread draining a bounded queue (no thread fan-out on
        # write bursts); when the queue is full the write is skipped and the
        # next overflow reconcile stamps it as a backstop.
        self._govern_lock = threading.Lock()
        self._govern_worker: Optional[threading.Thread] = None
        self._govern_queue: "queue.Queue" = queue.Queue(maxsize=128)

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """Reset per-session dedup state and load hot-tier full text."""
        self._injected_ids = set()
        self._hot_text = self._load_hot_layer_text()

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    # -- system_prompt_block: cold-store index (always active) -------------

    def system_prompt_block(self) -> str:
        """Return a static cold-store index block for on-demand recall guidance.

        This block is injected once as a system prompt and incurs zero
        per-turn overhead (no network/IO). It complements the optional
        per-turn prefetch — when prefetch is enabled, both channels coexist.
        """
        topics_env = os.environ.get("MEMORYCORE_INDEX_TOPICS", "").strip()
        if topics_env:
            topics = [t.strip() for t in topics_env.split(",") if t.strip()]
            topic_line = "Indexed topics: " + " / ".join(topics) + ".\n"
        else:
            topic_line = ""
        return (
            "## MemoryCore Cold-Store Index\n"
            + topic_line
            + "Use memorycore_recall(query) to retrieve historical details on demand."
        )

    # -- on_memory_write mirror overflow (2026-08-03) ---------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called after every built-in memory tool write: check usage, auto-overflow."""
        if action == "remove":
            return  # removal only lowers usage; orphan sidecar keys are GC'd by reconcile
        if target not in ("memory", "user"):
            return
        try:
            # Phase 2 (2026-08-16): direct-write governance — type the entry
            # right after add/replace commits: state-typed content is
            # migrated to the cold tier in the background (independent of the
            # usage thresholds); rule-typed content is stamped.
            if action in ("add", "replace"):
                self._govern_direct_write(target, content, action)
            limit = CHAR_LIMIT_MEMORY if target == "memory" else CHAR_LIMIT_USER
            store = LocalStore()
            pct = store.usage_pct(target, limit)
            hard = int(HARD_THRESHOLD * 100)
            soft = int(SOFT_THRESHOLD * 100)
            if pct >= hard:
                logger.info(
                    "on_memory_write: %s at %d%% >= hard %d%%, force overflow",
                    target, pct, hard,
                )
                self._spawn_overflow(target)
            else:
                last = self._last_overflow_pct.get(target, 0)
                if pct >= soft and pct > last + _OVERFLOW_RETRY_DELTA:
                    logger.info(
                        "on_memory_write: %s at %d%% >= soft %d%%, overflow "
                        "(last after=%d%%)", target, pct, soft, last,
                    )
                    self._spawn_overflow(target)
        except Exception as e:
            logger.debug("on_memory_write check failed: %s", e)

    def _govern_direct_write(self, target: str, content: str, action: str) -> None:
        """Direct-write governance (Phase 2, 2026-08-16): enqueue for the
        background worker; never blocks the memory tool return.

        The entry has already been written to the hot tier by Hermes; the
        governance only types it and migrates/stamps accordingly:
          state -> recall dedup -> cold write confirmed -> remove from hot
                   (cold failure keeps the entry + stamps a 7-day backstop)
          rule  -> sidecar stamp {rule, written_at=now, origin=hermes}
        """
        if not content or not content.strip():
            return
        with self._govern_lock:
            if self._govern_worker is None or not self._govern_worker.is_alive():
                self._govern_worker = threading.Thread(
                    target=self._govern_worker_loop,
                    daemon=True,
                )
                self._govern_worker.start()
        try:
            self._govern_queue.put_nowait((target, content, action))
        except queue.Full:
            logger.debug("govern queue full, skip (reconcile backstop)")

    def _govern_worker_loop(self) -> None:
        """Governance worker (single-flight): drains the queue serially so at
        most one cold-tier RPC is in flight at any time."""
        while True:
            item = self._govern_queue.get()
            try:
                self._run_govern_bg(*item)
            finally:
                self._govern_queue.task_done()

    def _run_govern_bg(self, target: str, content: str, action: str) -> None:
        """Run the governance via core.metadata.direct_write_govern."""
        try:
            store = LocalStore()
            client = ColdStoreClient()
            result = direct_write_govern(store, client, target, content, action)
            logger.info("direct-write govern target=%s action=%s result=%s",
                        target, action, result)
        except Exception as e:
            logger.debug("direct-write govern failed: %s", e)

    def _spawn_overflow(self, target: str) -> None:
        """Launch overflow in a background thread (at most one at a time)."""
        with self._overflow_lock:
            if self._overflow_thread and self._overflow_thread.is_alive():
                logger.debug("overflow already running, skip")
                return
            self._overflow_thread = threading.Thread(
                target=self._run_overflow_bg,
                args=(target,),
                daemon=True,
            )
            self._overflow_thread.start()

    def _run_overflow_bg(self, target: str) -> None:
        """Background overflow: reuses MemoryCore run_overflow."""
        try:
            store = LocalStore()
            client = ColdStoreClient()
            stat = run_overflow(store, client, target)
            pct_after = int(str(stat.get("usage_after", "0%")).rstrip("%") or 0)
            with self._overflow_lock:
                self._last_overflow_pct[target] = pct_after
            logger.info("overflow auto-done target=%s %s", target, stat)
        except Exception as e:
            logger.debug("overflow bg failed: %s", e)

    # -- prefetch / queue_prefetch (single-model, ENABLED by default) -----

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Per-turn cold-tier recall with dense-only ranking (qwen3 single-model).

        **Enabled by default.** Set MEMORYCORE_PREFETCH_ENABLED=0 to disable.
        When ollama is unreachable, returns empty string gracefully.
        """
        # Explicit "0" disables; unset or "1" enables.
        if os.environ.get("MEMORYCORE_PREFETCH_ENABLED", "").strip() == "0":
            return ""
        if not _probe_ollama():
            return ""
        try:
            # Ensure mnemosyne uses ollama for embedding (idempotent setdefault).
            os.environ.setdefault("MNEMOSYNE_EMBEDDING_API_URL", _EMBED_URL)
            os.environ.setdefault("MNEMOSYNE_EMBEDDING_MODEL", _EMBED_MODEL)
            os.environ.setdefault("MNEMOSYNE_EMBEDDING_DIM", "1024")
            return self._recall_sync(query)
        except Exception as e:
            logger.debug("prefetch failed: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No-op (background prefetch cache removed in 2026-08-05).

        Signature preserved for Hermes compatibility.
        """
        return

    # -- internals: recall / dedup / formatting (dense-only, 2026-08-11) --

    def _recall_sync(self, query: str) -> str:
        """Synchronous recall with dedicated timeout.

        Pipeline: query preprocessing -> cold-tier recall (20 candidates) ->
        dense ranking (top-5) -> session dedup -> hot-tier dedup ->
        format injection block.
        """
        q = self._preprocess_query(query)
        if not q:
            return ""
        try:
            client = ColdStoreClient(timeout=_PREFETCH_TIMEOUT)
            results = client.recall_results(q, top_k=_RECALL_CANDIDATES)
            self._record_baseline(results)
            results = _apply_decay(results)  # unified decay
            results = self._filter_by_dense_topn(results)
            results = self._dedupe_injected(results)
            results = self._dedupe_hot_layer(results)
            return self._format_results(results)
        except Exception as e:
            logger.debug("memorycore-prefetch sync recall failed: %s", e)
            return ""

    def _filter_by_dense_topn(self, results: list) -> list:
        """Single-model dense ranking (2026-08-11): sort by dense_score desc, take top-N.

        Replaces the old reranker pipeline. qwen3 scores are not cross-query
        comparable, so no absolute threshold — just relative ranking + truncation.
        """
        if not results:
            return []
        ranked = sorted(results, key=lambda r: r.get("dense_score", 0), reverse=True)
        return ranked[:_INJECT_TOP_N]

    def _record_baseline(self, results: list) -> None:
        """Record batch-max dense_score into the rolling baseline."""
        if not results:
            return
        top1 = max(r.get("dense_score", 0) for r in results)
        if top1:
            self._record_score(top1)

    @staticmethod
    def _format_results(results: List[Dict[str, Any]]) -> str:
        """Format recall results into injectable text (dense score only)."""
        if not results:
            return ""
        lines = []
        for r in results:
            score = r.get("dense_score", 0)
            content = r.get("content", "")
            lines.append(f"- [{score:.2f}] {content}")
        return "## MemoryCore Recall\n" + "\n".join(lines)

    @staticmethod
    def _preprocess_query(query: str) -> str:
        """Query preprocessing: strip, empty guard, length truncation."""
        q = (query or "").strip()
        if not q:
            return ""
        if len(q) > _QUERY_MAX_LEN:
            q = q[:_QUERY_MAX_LEN]
        return q

    def _load_hot_layer_text(self) -> str:
        """Load hot-tier MEMORY.md + USER.md full text for dedup."""
        try:
            store = LocalStore()
            parts = []
            for path in (store.memory_path, store.user_path):
                try:
                    parts.append(path.read_text(encoding="utf-8", errors="replace"))
                except Exception:
                    pass
            return "\n".join(parts)
        except Exception as e:
            logger.debug("hot layer load failed: %s", e)
            return ""

    def _dedupe_injected(self, results: list) -> list:
        """Session-level dedup: skip entries already injected this session."""
        kept = []
        for r in results:
            rid = r.get("id")
            if rid is not None and rid in self._injected_ids:
                continue
            kept.append(r)
            if rid is not None:
                self._injected_ids.add(rid)
        return kept

    def _dedupe_hot_layer(self, results: list) -> list:
        """Hot-tier dedup: skip entries whose content already exists in hot tier."""
        if not self._hot_text:
            return results
        kept = []
        for r in results:
            content = r.get("content", "")
            if content and content in self._hot_text:
                continue
            kept.append(r)
        return kept

    # -- dynamic baseline threshold (single-model, 2026-08-11) ------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Official Hermes interface: called after every turn (no-op)."""
        pass

    def _record_score(self, top1: float) -> None:
        """Record batch-max dense_score; roll window; recompute+persist median."""
        with self._baseline_lock:
            self._baseline_samples.append(top1)
            if len(self._baseline_samples) > _BASELINE_WINDOW:
                self._baseline_samples = self._baseline_samples[-_BASELINE_WINDOW:]
            self._sample_count_since_recalc += 1
            if self._sample_count_since_recalc >= _BASELINE_RECALC_EVERY:
                self._sample_count_since_recalc = 0
                if len(self._baseline_samples) >= 2:
                    self._baseline_value = statistics.median(self._baseline_samples)
                    self._persist_baseline()

    def _current_baseline(self) -> float:
        """Return the current baseline value."""
        with self._baseline_lock:
            return self._baseline_value

    def _load_baseline(self) -> None:
        """Load baseline from the persisted file."""
        try:
            with open(_BASELINE_FILE) as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._baseline_samples = data.get("samples", [])
                if not isinstance(self._baseline_samples, list):
                    self._baseline_samples = []
                self._baseline_value = data.get("baseline", _BASELINE_INIT)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            logger.debug("baseline load skipped: %s", e)
            self._baseline_value = _BASELINE_INIT

    def _persist_baseline(self) -> None:
        """Atomically persist the baseline to disk."""
        try:
            data = {
                "samples": self._baseline_samples,
                "baseline": self._baseline_value,
                "count": len(self._baseline_samples),
            }
            dirname = os.path.dirname(_BASELINE_FILE)
            fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".baseline_tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(data, f)
                os.replace(tmp, _BASELINE_FILE)
            except Exception:
                os.unlink(tmp)
                raise
        except Exception as e:
            logger.debug("baseline persist failed: %s", e)


def register(ctx) -> None:
    ctx.register_memory_provider(MemoryCorePrefetchProvider())
