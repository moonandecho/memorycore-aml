#!/usr/bin/env python3
"""trash_store.py — cold-tier stale-entry recycle bin

Mechanism:
  - Inbound: long entries (>80 chars) containing "落地中/进行中"-style
    in-progress markers that the LLM judged not_stale → trashed (NOT forgotten)
  - Recall revival: during maintenance, each trashed entry is recall-queried;
    if it is hit within top_k=10, it is removed from the bin (still in use)
  - Expiry purge: entries older than ttl_days → forget + remove from bin
  - Reporting: pending_stale (current bin count) / trash_cleared (purged this
    run) / trash_revived (revived this run)

Storage: single JSON file, atomic writes. Default path ~/.memorycore/trash.json
(override with MEMORYCORE_TRASH_PATH).
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List

TRASH_PATH = Path(
    os.environ.get(
        "MEMORYCORE_TRASH_PATH",
        os.path.expanduser("~/.memorycore/trash.json"),
    )
)
TRASH_TTL_DAYS = 30


class TrashStore:
    """Recycle bin (single JSON file, atomic writes)."""

    def __init__(self, path: Path = TRASH_PATH, ttl_days: int = TRASH_TTL_DAYS):
        self.path = path
        self.ttl_days = ttl_days

    # -- read ----------------------------------------------------

    def _load(self) -> Dict[str, Any]:
        if not self.path.exists():
            return {"entries": []}
        try:
            raw = self.path.read_text(encoding="utf-8")
            if not raw.strip():
                return {"entries": []}
            return json.loads(raw)
        except (json.JSONDecodeError, OSError):
            return {"entries": []}

    # -- write (atomic) -------------------------------------------

    def _save(self, data: Dict[str, Any]) -> None:
        import tempfile

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=".trash-", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # -- operations -----------------------------------------------

    def add(
        self,
        memory_id: str,
        content: str,
        reason: str = "stale_candidate",
        source_decision: str = "llm_not_stale",
    ) -> None:
        """Add an entry to the bin (dedupe by memory_id: re-trash updates)."""
        data = self._load()
        existing = [e for e in data["entries"] if e.get("memory_id") == memory_id]
        if existing:
            for e in existing:
                e["trashed_at"] = datetime.now(tz=timezone.utc).isoformat()
                e["reason"] = reason
                e["source_decision"] = source_decision
        else:
            data["entries"].append({
                "memory_id": memory_id,
                "content": content[:300],  # truncated, enough for recall queries
                "trashed_at": datetime.now(tz=timezone.utc).isoformat(),
                "reason": reason,
                "source_decision": source_decision,
            })
        self._save(data)

    def remove(self, memory_id: str) -> bool:
        """Remove from bin (after revival or expiry purge). Returns success."""
        data = self._load()
        before = len(data["entries"])
        data["entries"] = [
            e for e in data["entries"] if e.get("memory_id") != memory_id
        ]
        if len(data["entries"]) < before:
            self._save(data)
            return True
        return False

    def get_all(self) -> List[Dict[str, Any]]:
        """All entries currently in the bin."""
        return self._load().get("entries", [])

    def get_expired(self) -> List[Dict[str, Any]]:
        """Entries older than ttl_days (by trashed_at)."""
        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=self.ttl_days)
        expired = []
        for e in self.get_all():
            try:
                ts = datetime.fromisoformat(e["trashed_at"])
            except (ValueError, KeyError):
                ts = datetime.now(tz=timezone.utc)  # unparseable → not expired
            if ts < cutoff:
                expired.append(e)
        return expired

    def count(self) -> int:
        """Current bin size."""
        return len(self.get_all())
