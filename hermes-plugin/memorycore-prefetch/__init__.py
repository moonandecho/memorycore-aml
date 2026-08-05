"""memorycore-prefetch — MemoryProvider plugin: dual-channel recall + write-mirror overflow

Dual-role plugin:
1. prefetch (per-turn semantic recall): recalls the cold tier (top-20),
   optionally re-ranks via a cross-encoder, and injects top-5 into context.
   **Off by default** (EXPERIMENTAL) — set MEMORYCORE_PREFETCH_ENABLED=1.
2. on_memory_write mirror (2026-08-03): after every built-in memory tool
   write (add/replace), checks hot-tier usage in real time; triggers
   overflow at >=80% (hard) or >=60% (soft, with 5% hysteresis).
   Overflow runs in a background thread so it never blocks the tool return.

Also provides a static cold-store index block via system_prompt_block()
that guides the model to use on-demand recall (memorycore_recall tool).
This index block is always active regardless of the prefetch switch.

Implements the Hermes MemoryProvider ABC: is_available → True,
get_tool_schemas → [] (no tools injected).

Activation (Hermes Agent):
    hermes config set memory.provider memorycore-prefetch
    # and copy/link this directory into ~/.hermes/plugins/memorycore-prefetch/

This plugin depends on the Hermes Agent runtime (agent.memory_provider).
It is a Hermes-specific integration; the MemoryCore server itself stays
client-agnostic.
"""

import json
import logging
import os
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

_RECALL_CANDIDATES = 20    # first-stage recall candidates (before reranker)
_INJECT_TOP_N = 5          # max injected after reranker ranking
_PREFETCH_TIMEOUT = 5.0    # prefetch-specific timeout, shorter than default 10s
_QUERY_MAX_LEN = 1000      # recall query max chars (matches reranker query limit)

# --- on_memory_write auto-overflow (2026-08-03) ---
_OVERFLOW_RETRY_DELTA = 5  # soft trigger must see >=5% growth since last overflow
                            # (debounce — avoid re-running on every write)

# --- reranker two-stage refinement (2026-08-05) ---
# Configure MEMORYCORE_RERANK_URL to enable cross-encoder reranking.
# When unset, the plugin automatically degrades to a dense-score fallback
# (top-1 only, tighter threshold). Reranker call failures/timeouts also
# trigger the same graceful degradation — never blocks, never errors out.
#
# Set MEMORYCORE_RERANK_URL to the full reranker endpoint URL
# (e.g. http://your-reranker-host:8899/rerank).
#   POST {"query": q, "documents": docs}
#   Response: {"results": [{"index": 0, "relevance_score": 8.5}, ...]}
#
# Calibration (bge-reranker-v2-m3):
#   True relevant typically scores >= -3.5; unrelated < -5.
#   Threshold -3.5 sits in the gap (relaxed from -2.0 to keep more
#   true relevant entries; tiered injection below filters by importance).
_RERANK_THRESHOLD = -3.5
_RERANK_TIER_HIGH = 0.7    # tiered injection: importance >= this sorts first
_RERANK_TIMEOUT = 5.0
_RERANK_QUERY_MAX = 1000   # context truncation (bge-reranker training limit 1024)
_RERANK_DOC_MAX = 500      # memory entry truncation

# --- dynamic baseline threshold (fixed coefficient, no water-level bands) ---
_BASELINE_INIT = 0.70        # initial baseline: median batch-max dense_score
                             # (N=3000 core+scene, bootstrap CI [0.678, 0.716])
_ABS_FLOOR = 0.45            # lower guardrail (not a noise barrier at scale;
                             #  the reranker handles noise separation)
_BASELINE_WINDOW = 200       # rolling sample window
_BASELINE_RECALC_EVERY = 50  # recompute median every N new samples
_COEF = 0.90                 # fixed coefficient (water-level bands removed)
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
        # target -> last post-overflow usage pct; soft trigger requires
        # current > last + _OVERFLOW_RETRY_DELTA (debounce)
        self._last_overflow_pct: Dict[str, int] = {}

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
        per-turn prefetch — when prefetch is enabled, both channels coexist
        (static index + active injection).

        Topics can be configured via MEMORYCORE_INDEX_TOPICS (comma-separated).
        When unset, only a generic guidance line is returned.
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

    # -- prefetch / queue_prefetch (dual-channel, EXPERIMENTAL) ------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Per-turn cold-tier recall with optional reranker refinement.

        **Off by default (EXPERIMENTAL).** Set MEMORYCORE_PREFETCH_ENABLED=1
        to enable. When enabled: recalls top-20 from cold tier → optional
        reranker filtering → dedup → injects top-5 into context.

        When disabled (default), returns empty string — no per-turn overhead.
        """
        if os.environ.get("MEMORYCORE_PREFETCH_ENABLED", "").strip() != "1":
            return ""
        try:
            return self._recall_sync(query)
        except Exception as e:
            logger.debug("prefetch failed: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """No-op (background prefetch cache removed in 2026-08-05).

        The background recall cache was never consumed by the sync path,
        resulting in duplicate recall+rerank work every turn with no benefit.
        Signature preserved for Hermes compatibility.
        """
        return

    # -- internals: recall / reranker / dedup / formatting -----------------

    def _recall_sync(self, query: str) -> str:
        """Synchronous recall with dedicated timeout.

        Pipeline: query preprocessing → cold-tier recall (10 candidates) →
        reranker refinement (if configured) → session dedup → hot-tier dedup →
        format injection block.

        When the reranker is not configured or unavailable, falls back to
        dense-score top-1 with a tightened threshold.
        """
        q = self._preprocess_query(query)
        if not q:
            return ""
        try:
            client = ColdStoreClient(timeout=_PREFETCH_TIMEOUT)
            results = client.recall_results(q, top_k=_RECALL_CANDIDATES)
            self._record_baseline(results)
            results, rerank_ok = self._apply_rerank_filter(q, results)
            if not rerank_ok:
                results = self._filter_by_threshold(results)
            results = self._dedupe_injected(results)
            results = self._dedupe_hot_layer(results)
            return self._format_results(results)
        except Exception as e:
            logger.debug("memorycore-prefetch sync recall failed: %s", e)
            return ""

    def _record_baseline(self, results: list) -> None:
        """Record batch-max dense_score into the rolling baseline.

        Always uses dense scores (not reranker scores) so the baseline
        remains calibrated regardless of reranker availability.
        """
        if not results:
            return
        top1 = max(r.get("dense_score", 0) for r in results)
        if top1:
            self._record_score(top1)

    def _apply_rerank_filter(self, query: str, results: list):
        """Reranker two-stage refinement: absolute threshold → sort → inject top-N.

        Reads MEMORYCORE_RERANK_URL from environment. When unset or the
        reranker call fails/times out, returns (results, False) to signal
        the caller to fall back to dense-score filtering.

        Returns (kept, ok):
        - ok=True: reranker succeeded, kept is the filtered list (may be empty).
        - ok=False: reranker unavailable, caller should use dense fallback.
        """
        if not results:
            return results, True
        rerank_url = os.environ.get("MEMORYCORE_RERANK_URL", "").strip()
        if not rerank_url:
            return results, False
        try:
            docs = [r.get("content", "")[:_RERANK_DOC_MAX] for r in results]
            q = query[:_RERANK_QUERY_MAX]
            body = json.dumps({"query": q, "documents": docs}).encode()
            req_url = rerank_url
            req = urllib.request.Request(
                req_url, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=_RERANK_TIMEOUT) as resp:
                data = json.load(resp)
            scores = {item["index"]: item.get("relevance_score")
                      for item in data.get("results", [])}
            kept = []
            for i, res in enumerate(results):
                sc = scores.get(i)
                if sc is None:
                    continue
                res = dict(res)
                res["rerank_score"] = sc
                if sc >= _RERANK_THRESHOLD:
                    kept.append(res)
            # tiered injection: high-importance entries first, then by
            # rerank score within each tier; threshold uses raw score.
            hi = [r for r in kept if (r.get("importance", 0.5) or 0.5) >= _RERANK_TIER_HIGH]
            lo = [r for r in kept if (r.get("importance", 0.5) or 0.5) < _RERANK_TIER_HIGH]
            hi.sort(key=lambda r: r.get("rerank_score", 0), reverse=True)
            lo.sort(key=lambda r: r.get("rerank_score", 0), reverse=True)
            kept = (hi + lo)[:_INJECT_TOP_N]
            logger.info(
                "prefetch rerank: kept=%d/%d thr=%.2f top=%d",
                len(kept), len(results), _RERANK_THRESHOLD, _INJECT_TOP_N)
            return kept, True
        except Exception as e:
            logger.debug("prefetch rerank unavailable, fallback dense: %s", e)
            return results, False

    @staticmethod
    def _format_results(results: List[Dict[str, Any]]) -> str:
        """Format recall results into injectable text.

        Score display matches the filtering path: rerank_score when
        reranker was used, dense_score when on the fallback path.
        """
        if not results:
            return ""
        lines = []
        for r in results:
            if "rerank_score" in r:
                score = r.get("rerank_score", 0)
            else:
                score = r.get("dense_score", 0)
            content = r.get("content", "")
            lines.append(f"- [{score:.2f}] {content}")
        return "## MemoryCore Recall\n" + "\n".join(lines)

    @staticmethod
    def _preprocess_query(query: str) -> str:
        """Query preprocessing: strip, empty guard, length truncation.

        Truncation prevents embedding dilution on very long messages.
        Complex intent extraction is deferred to a future iteration.
        """
        q = (query or "").strip()
        if not q:
            return ""
        if len(q) > _QUERY_MAX_LEN:
            q = q[:_QUERY_MAX_LEN]
        return q

    def _load_hot_layer_text(self) -> str:
        """Load hot-tier MEMORY.md + USER.md full text for dedup (2026-08-05).

        Cold-tier entries whose content already appears verbatim in the
        hot tier are skipped — the agent already has this information.
        When local files are unreadable, returns empty string (fail-safe:
        no false-positive dedup kills).
        """
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

    # -- dynamic baseline threshold (fixed coefficient) --------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Official Hermes interface: called after every turn.

        Water-level estimation was removed in 2026-08-05; the threshold
        now uses a fixed coefficient (0.90) regardless of context fullness.
        Signature preserved for Hermes compatibility.
        """
        pass

    def _compute_threshold(self) -> float:
        """Fixed-coefficient threshold: baseline × 0.90, clamped to _ABS_FLOOR.

        Water-level bands (_COEF_LOW/MID/HIGH) were removed in 2026-08-05.
        """
        return max(_ABS_FLOOR, self._current_baseline() * _COEF)

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

    def _filter_by_threshold(self, results: list) -> list:
        """Fallback path (reranker unavailable): inject only top-1 with
        tightened threshold. Dense scores alone cannot reliably separate
        intent at scale, so we err on the side of silence.

        Baseline recording is handled separately by _record_baseline.
        """
        if not results:
            return []
        threshold = self._compute_threshold()
        best = max(results, key=lambda r: r.get("dense_score", 0))
        if best.get("dense_score", 0) >= threshold:
            return [best]
        logger.info(
            "prefetch fallback: top-1 dense=%.3f < thr=%.3f, drop all",
            best.get("dense_score", 0), threshold)
        return []


def register(ctx) -> None:
    ctx.register_memory_provider(MemoryCorePrefetchProvider())
