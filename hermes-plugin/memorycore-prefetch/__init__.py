"""memorycore-prefetch — MemoryProvider plugin: cold-tier recall + write-mirror overflow

Dual-role plugin:
1. prefetch (read-only recall): recalls the cold tier (top-3) every turn
   and injects the result into the agent context.
2. on_memory_write mirror (2026-08-03): after every built-in memory tool
   write (add/replace), checks hot-tier usage in real time; triggers
   overflow at >=80% (hard) or >=60% (soft, with 5% hysteresis).
   Overflow runs in a background thread so it never blocks the tool return.

Implements the Hermes MemoryProvider ABC: is_available → True,
get_tool_schemas → [] (no tools injected), prefetch(query) recalls the
cold tier and returns text to inject.

queue_prefetch(query) runs recall in a background thread; the result is
cached for the next prefetch call.

Activation (Hermes Agent):
    hermes config set memory.provider memorycore-prefetch
    # and copy/link this directory into ~/.hermes/plugins/memorycore-prefetch/

This plugin depends on the Hermes Agent runtime (agent.memory_provider,
agent.model_metadata, agent.models_dev). It is a Hermes-specific
integration; the MemoryCore server itself stays client-agnostic.
"""

import json
import logging
import os
import statistics
import sys
import tempfile
import threading
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

_TOP_K = 3
_PREFETCH_TIMEOUT = 5.0   # prefetch-specific timeout, shorter than default 10s
_QUEUE_TIMEOUT = 8.0      # background queue slightly more generous

# --- on_memory_write auto-overflow (2026-08-03) ---
_OVERFLOW_RETRY_DELTA = 5  # soft trigger must see >=5% growth since last overflow (avoid
                            # re-running on every write)

# --- adaptive threshold config -------------------------------------------
_BASELINE_INIT = 0.70        # initial baseline: median batch-max dense_score
                             # from top_k=3 semantic queries (46 samples)
_ABS_FLOOR = 0.45            # lower guardrail: below this, never inject
                             # (calibrated on small cold tier; known to
                             #  admit noise at 1000+ entries — see
                             #  docs/ADAPTIVE_THRESHOLD.md § 大库实测)
_BASELINE_WINDOW = 200       # rolling sample window
_BASELINE_RECALC_EVERY = 50  # recompute median every N new samples
# water-level bands -> coefficients
_WATER_LOW = 50_000          # <50K tokens → low water
_WATER_MID = 150_000         # 50K~150K → mid water
_COEF_LOW = 0.90
_COEF_MID = 0.90
_COEF_HIGH = 1.00
# compression-point correction (combined scheme)
_COMPRESS_RATIO = 0.7        # config compression.threshold
_COMPRESS_FORCE_MID = 0.5    # tokens > compress_at*0.5 → at least mid
_COMPRESS_FORCE_HIGH = 0.8   # tokens > compress_at*0.8 → force high
# baseline persistence file
_BASELINE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "baseline.json")


class MemoryCorePrefetchProvider(MemoryProvider):
    name = "memorycore-prefetch"

    def __init__(self):
        super().__init__()
        self._cache: Optional[str] = None
        self._cache_lock = threading.Lock()
        self._pending_query: Optional[str] = None
        self._bg_thread: Optional[threading.Thread] = None
        # adaptive-threshold state
        self._water_lock = threading.Lock()
        self._water_level: Optional[str] = None  # 'low' | 'mid' | 'high'
        self._last_tokens: int = 0
        self._last_messages: Optional[List[Dict[str, Any]]] = None
        self._context_window: Optional[int] = None  # cached on first use
        self._baseline_lock = threading.Lock()
        self._baseline_samples: List[float] = []
        self._baseline_value: float = _BASELINE_INIT
        self._sample_count_since_recalc: int = 0
        self._load_baseline()
        # on_memory_write auto-overflow state
        self._overflow_lock = threading.Lock()
        self._overflow_thread: Optional[threading.Thread] = None
        # target -> last post-overflow usage pct; soft trigger requires
        # current > last + _OVERFLOW_RETRY_DELTA (debounce)
        self._last_overflow_pct: Dict[str, int] = {}

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        pass

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    # -- on_memory_write mirror overflow (2026-08-03) ---------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Called after every built-in memory tool write: check usage, auto-overflow.

        Closes the gap left by plan B — automatic overflow now covers *all*
        writes (built-in memory tool, WeChat gateway, other sessions), not
        just store_fact.

        remove only reduces usage, never triggers overflow.
        add/replace check usage and trigger when thresholds are crossed.
        Overflow runs in a background thread so the memory tool return is
        never blocked.
        """
        if action == "remove":
            return
        if target not in ("memory", "user"):
            return
        try:
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
        """Background overflow: reuses MemoryCore run_overflow (no logic copy).

        When the cold tier is unreachable, run_overflow accumulates errors
        internally and keeps local entries — no data is lost.
        Records post-overflow usage so the next soft trigger can debounce.
        """
        try:
            store = LocalStore()
            client = ColdStoreClient()  # default 10s timeout, needed for dedup
            stat = run_overflow(store, client, target)
            pct_after = int(str(stat.get("usage_after", "0%")).rstrip("%") or 0)
            with self._overflow_lock:
                self._last_overflow_pct[target] = pct_after
            logger.info("overflow auto-done target=%s %s", target, stat)
        except Exception as e:
            logger.debug("overflow bg failed: %s", e)

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Called every turn: recall cold tier top-3 for the current query.

        Returns the cached queue_prefetch result when available; otherwise
        does a synchronous recall. Timeout/failure degrade silently to an
        empty string (never blocks the conversation).
        """
        if not query or not query.strip():
            return ""

        # prefer cached result (recalled in background)
        with self._cache_lock:
            if self._cache is not None:
                cached = self._cache
                self._cache = None
                return cached

        # synchronous recall with timeout protection
        return self._recall_sync(query)

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Async background recall: cache result for the next prefetch call.

        Non-blocking; failures are silent.
        """
        if not query or not query.strip():
            return

        self._pending_query = query

        # don't start a second thread while one is still running
        if self._bg_thread and self._bg_thread.is_alive():
            return

        self._bg_thread = threading.Thread(
            target=self._recall_bg,
            args=(query,),
            daemon=True,
        )
        self._bg_thread.start()

    # -- internals ---------------------------------------------------------

    def _recall_sync(self, query: str) -> str:
        """Synchronous recall (with dedicated timeout)."""
        try:
            client = ColdStoreClient(timeout=_PREFETCH_TIMEOUT)
            results = client.recall_results(query, top_k=_TOP_K)
            results = self._filter_by_threshold(results)
            return self._format_results(results)
        except Exception as e:
            logger.debug("memorycore-prefetch sync recall failed: %s", e)
            return ""

    def _recall_bg(self, query: str) -> None:
        """Background-thread recall."""
        try:
            client = ColdStoreClient(timeout=_QUEUE_TIMEOUT)
            results = client.recall_results(query, top_k=_TOP_K)
            results = self._filter_by_threshold(results)
            formatted = self._format_results(results)
            with self._cache_lock:
                self._cache = formatted
        except Exception as e:
            logger.debug("memorycore-prefetch bg recall failed: %s", e)
            # cache an empty result so the next turn doesn't retry
            with self._cache_lock:
                if self._cache is None:
                    self._cache = ""

    @staticmethod
    def _format_results(results: List[Dict[str, Any]]) -> str:
        """Format recall results into injectable text."""
        if not results:
            return ""
        lines = []
        for r in results:
            score = r.get("dense_score", 0)
            content = r.get("content", "")
            lines.append(f"- [{score:.2f}] {content}")
        return "## MemoryCore Recall\n" + "\n".join(lines)

    # -- adaptive water-level threshold -------------------------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Official Hermes interface: called after every turn with the full
        conversation history. Only estimates and caches the water level;
        writes nothing.
        """
        try:
            if messages:
                self._last_messages = messages
                level, tokens = self._estimate_water_level(messages)
                with self._water_lock:
                    self._water_level = level
                    self._last_tokens = tokens
        except Exception as e:
            logger.debug("water level estimate failed: %s", e)
            with self._water_lock:
                self._water_level = None

    def _estimate_water_level(self, messages) -> tuple:
        """Estimate context water level, return (band, tokens)."""
        from agent.model_metadata import estimate_messages_tokens_rough
        tokens = estimate_messages_tokens_rough(messages or [])
        # absolute bands
        if tokens < _WATER_LOW:
            level = 'low'
        elif tokens < _WATER_MID:
            level = 'mid'
        else:
            level = 'high'
        # compression-point correction
        window = self._get_context_window()
        compress_at = window * _COMPRESS_RATIO
        if tokens > compress_at * _COMPRESS_FORCE_HIGH:
            level = 'high'
        elif tokens > compress_at * _COMPRESS_FORCE_MID and level == 'low':
            level = 'mid'
        return level, tokens

    def _get_context_window(self) -> int:
        """Look up the current model's context window; cached. Default 1M."""
        if self._context_window is not None:
            return self._context_window
        window = 1_000_000
        try:
            import yaml
            config_path = os.path.expanduser("~/.hermes/config.yaml")
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            provider_id = model_cfg.get("provider", "")
            model_id = model_cfg.get("default", "")
            if provider_id and model_id:
                from agent.models_dev import get_model_info
                info = get_model_info(provider_id, model_id)
                if info and info.context_window:
                    window = info.context_window
        except Exception as e:
            logger.debug("get_context_window failed, using 1M default: %s", e)
        self._context_window = window
        return window

    def _compute_threshold(self, level: str) -> float:
        coef = {'low': _COEF_LOW, 'mid': _COEF_MID, 'high': _COEF_HIGH}[level]
        return max(_ABS_FLOOR, self._current_baseline() * coef)

    def _record_score(self, top1: float) -> None:
        """Record the batch max dense_score of each recall; roll the window
        at _BASELINE_WINDOW samples; recompute + persist the median every
        _BASELINE_RECALC_EVERY new samples.
        """
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
        """Return the current baseline."""
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
        """Atomically persist the baseline."""
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

    def _filter_by_threshold(self, results: list) -> list:
        """Filter by the current band threshold; also record the batch max
        dense_score into the baseline.

        The baseline represents "the best semantic quality this query could
        recall", so we take the batch MAX rather than results[0] (the server
        sorts by blended score; the first result is not necessarily the
        highest dense score).
        """
        if not results:
            return []
        # water level read lock (sync_turn writes from a background thread)
        with self._water_lock:
            level = self._water_level or 'mid'
        threshold = self._compute_threshold(level)
        # record batch max dense_score as a baseline sample
        top1_score = max(r.get('dense_score', 0) for r in results)
        if top1_score:
            self._record_score(top1_score)
        kept = [r for r in results if r.get('dense_score', 0) >= threshold]
        logger.debug(
            "prefetch filter: level=%s threshold=%.3f kept=%d/%d",
            level, threshold, len(kept), len(results),
        )
        return kept


def register(ctx) -> None:
    ctx.register_memory_provider(MemoryCorePrefetchProvider())
