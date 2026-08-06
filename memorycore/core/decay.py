#!/usr/bin/env python3
"""core/decay.py — cold-tier decay helper (shared by recall + prefetch)

Extracted from server.py so prefetch does not import server.py (which would
trigger FastMCP side effects). Mirrors the production implementation.
"""
from datetime import datetime, timezone as _tz


def _apply_decay(results):
    """Apply cold-tier decay to recall results: final_score = dense_score × 0.5^(days/90).

    Semantics: presentation-layer ranking (recall visibility) — answers
    "how high should this entry rank for this query".
    Division of labour with _forget_decayed: this handles recency-aware
    ranking (base = dense_score); _forget_decayed handles storage retention
    (base = importance, more stable without query semantics).

    - importance >= 0.8: no decay (final_score = dense_score, protected line)
    - last_recalled missing → fall back to timestamp (write time)
    - last_recalled/timestamp both missing/unparseable: treat as 365 days ago
      (factor ≈ 0.060)
    - importance missing: default 0.5
    - dense_score missing: default 0 (graceful degradation)
    - sort: final_score descending, ties keep original order (stable sort)
    """
    if not results:
        return results

    now = datetime.now(_tz.utc)

    for r in results:
        base_score = r.get("dense_score", 0)
        importance = r.get("importance", 0.5)

        # protected line: importance >= 0.8 never decays
        if importance >= 0.8:
            r["final_score"] = base_score
            continue

        # parse last_recalled (fall back to timestamp when missing)
        last_str = r.get("last_recalled")
        days = 365  # final fallback
        if last_str:
            try:
                normalized = last_str.replace("Z", "+00:00")
                last_dt = datetime.fromisoformat(normalized)
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=_tz.utc)
                delta = now - last_dt
                days = max(delta.days, 0)
            except (ValueError, TypeError):
                days = 365
        else:
            # last_recalled missing → fall back to timestamp (write time)
            ts_str = r.get("timestamp")
            if ts_str:
                try:
                    normalized = ts_str.replace("Z", "+00:00")
                    ts_dt = datetime.fromisoformat(normalized)
                    if ts_dt.tzinfo is None:
                        ts_dt = ts_dt.replace(tzinfo=_tz.utc)
                    delta = now - ts_dt
                    days = max(delta.days, 0)
                except (ValueError, TypeError):
                    days = 365

        factor = 0.5 ** (days / 90)
        r["final_score"] = base_score * factor

    # stable sort: final_score descending
    results.sort(key=lambda x: x.get("final_score", 0), reverse=True)

    return results
