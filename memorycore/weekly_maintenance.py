"""MemoryCore 每周治理 (weekly_maintenance) — 2026-09-04

编排周度治理 (originally driven by an external scheduler prompt; now a
self-contained module inside memorycore):
  1. six-step overflow (run_overflow x memory/user)
  2. smart tidy (only if hot tier still >60% after overflow):
       a) sink stale historical entries: embedded date >=14 days old +
          completion/retirement markers + NOT protected -> LLM confirm ->
          store to cold tier first, then remove from hot tier
       b) merge highly-overlapping behavior rules: similarity >= threshold +
          both NOT protected -> LLM produces merged text (all points kept) ->
          originals go to cold tier -> hot tier merged
     LLM unavailable -> only rule-strong-signal sinks run; merging is skipped
     (needs semantics, never guess)
  3. cold-tier maintenance (run_maintenance)
  4. report: written to logs/weekly-YYYYMMDD.md + optional notifier

Optional notification (default OFF): set env MEMORYCORE_NOTIFY_SCRIPT to a
script path; the report body is piped to it as stdin with subject as argv[1].
No personal channel or e-mail address is hardcoded anywhere in this module.

Privacy: config comes exclusively from environment variables
(LLM_API_KEY / LLM_BASE_URL / LLM_MODEL for the optional LLM confirmation;
MEMORYCORE_NOTIFY_SCRIPT for notifications). Nothing user-specific is baked in.

Usage:
    python -m memorycore.weekly_maintenance [--dry-run] [--no-email]

Safeguards: entries protected by _is_protected_rule (importance>=0.8 /
red-line words / behavior-rule words) are never deleted — only mergeable
(originals are archived to the cold tier first). Per-target caps: sink <=3,
merge <=1 pair per run. When in doubt, do nothing.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .local_store import LocalStore  # noqa: E402
from .cold_store_client import ColdStoreClient  # noqa: E402
from .core.metadata import MetaStore, _parse_iso  # noqa: E402
from .core.overflow import run_overflow, _is_protected_rule, _topic_overlap  # noqa: E402
from .core.maintenance import run_maintenance  # noqa: E402

# ---- constants ------------------------------------------------------------
MEMORY_LIMIT = 5000
SORT_THRESHOLD = 0.60          # run smart tidy only if usage stays above this
COMPLETE_AGE_DAYS = 14         # embedded date must be this old to consider sink
MAX_SINK_PER_RUN = 3           # per-target sink cap per run
MAX_MERGE_PER_RUN = 1          # per-target merge pairs cap per run
MERGE_RATIO = 0.62             # merge similarity threshold
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"

# Optional notifier: env MEMORYCORE_NOTIFY_SCRIPT (script path). Empty = off.
NOTIFY_SCRIPT = os.environ.get("MEMORYCORE_NOTIFY_SCRIPT", "")

_DONE_WORDS = [
    "退役", "已删", "已清理", "已解决", "已卸载", "已放弃", "已归档", "已停用",
    "已拆除", "已修复", "已废弃", "已移除", "已注销", "已退役", "放弃", "卸载",
    "purge", "清理干净", "不再", "已退出", "已下线", "已弃用",
]
_DATE_RE = re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})")


def _llm_call(system: str, user: str, max_tokens: int = 900) -> str | None:
    """Call the configured LLM (OpenAI-compatible chat/completions). None on failure."""
    api_key = os.environ.get("LLM_API_KEY", "")
    base = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    model = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
    if not api_key:
        return None
    import urllib.request
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    req = urllib.request.Request(
        base + "/chat/completions",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=float(os.environ.get("LLM_TIMEOUT", "20"))) as resp:
            data = json.loads(resp.read().decode())
        return data["choices"][0]["message"]["content"].strip()
    except Exception:
        return None


def _entry_date_days(entry: str, meta: dict) -> int | None:
    """Days since the entry's embedded/written date; None if undated."""
    m = _DATE_RE.search(entry)
    if m:
        try:
            d = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - d).days
        except ValueError:
            pass
    if meta.get("written_at"):
        try:
            d = _parse_iso(str(meta["written_at"]))
            if d is not None:
                return (datetime.now(timezone.utc) - d).days
        except Exception:
            pass
    return None


def _is_historical_done(entry: str) -> bool:
    return any(w in entry for w in _DONE_WORDS)


def _llm_confirm_sink(entry: str) -> bool:
    """LLM confirm the entry is a resolved/stale historical record safe to
    move out of the hot tier. When unsure -> False (keep)."""
    sys_p = ("你是记忆治理助手。判断一条记忆是否属于'已解决/已过时的历史记录, 不再需要在每轮注入的热层保留'。"
             "只判断事实状态, 不判断价值。拿不准一律输出 false。")
    out = _llm_call(sys_p, f"<entry>{entry[:400]}</entry>\n该条目可以安全下沉到冷层吗? 只输出 JSON: {{\"sink\": true/false}}")
    if not out:
        return False
    try:
        m = re.search(r"\"sink\"\s*:\s*(true|false)", out)
        return m is not None and m.group(1) == "true"
    except Exception:
        return False


def _llm_merge_text(a: str, b: str) -> str | None:
    """LLM merge two same-topic rules into one concise text (all points kept)."""
    sys_p = ("你是记忆整理助手。把两条同主题的记忆准则合并成一条精炼文本: "
             "保留所有要点、约束词、日期、例子, 只去重复表述; 不添加新事实; 100字以内一句话。只输出合并后文本。")
    out = _llm_call(sys_p, f"A: {a}\nB: {b}\n合并:")
    if not out or len(out) < 20:
        return None
    return out.strip().strip("\"'").strip()


def _sink_entry(store, client, target: str, entry: str, meta: dict, stat: dict, dry: bool) -> None:
    """Sink one historical entry: cold-tier write confirmed -> remove hot-tier."""
    if dry:
        stat["sink_dry"].append(entry[:50])
        return
    try:
        r = client.remember(entry, importance=0.3, scope="global")
        if r.get("status") != "stored":
            stat["errors"] += 1
            return
    except Exception:
        stat["errors"] += 1
        return
    before = len(store.entries(target))
    store.remove_by_exact(target, entry)
    if len(store.entries(target)) < before:
        stat["sunk"] += 1


def _merge_pair(store, client, target: str, a: str, b: str, meta_a: dict, stat: dict, dry: bool) -> None:
    """Merge a+b into one entry: originals archived to cold tier -> replace a,
    remove b. Any failure keeps both originals untouched."""
    merged = _llm_merge_text(a, b)
    if not merged:
        stat["merge_skipped"] += 1
        return
    if dry:
        stat["merge_dry"].append((a[:30], b[:30], merged[:60]))
        return
    ok = True
    for txt in (a, b):
        try:
            r = client.remember(txt, importance=0.5, scope="global")
            if r.get("status") != "stored":
                ok = False
        except Exception:
            ok = False
    if not ok:
        stat["errors"] += 1
        return
    entries_now = store.entries(target)
    if a not in entries_now or b not in entries_now:
        stat["errors"] += 1
        return
    store.replace(target, a, merged)
    store.remove_by_exact(target, b)
    if merged in store.entries(target) and b not in store.entries(target):
        stat["merged"] += 1


def _ratio(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def smart_tidy(store, client, target: str, stat: dict, dry: bool) -> None:
    """Smart tidy: sink stale history + merge overlapping rules. Protected
    entries are never deleted."""
    entries = store.entries(target)
    meta_store = MetaStore(target, memory_path=store.memory_path, user_path=store.user_path)

    # --- a) sink stale historical entries ---
    sink_cands = []
    for e in entries:
        meta = meta_store.get_entry(e) or {}
        if _is_protected_rule(e, meta):
            continue
        days = _entry_date_days(e, meta)
        if days is None or days < COMPLETE_AGE_DAYS:
            continue
        if not _is_historical_done(e):
            continue
        sink_cands.append((days, e, meta))
    sink_cands.sort(key=lambda t: -t[0])  # oldest first
    processed = 0
    for days, e, meta in sink_cands:
        if processed >= MAX_SINK_PER_RUN:
            break
        if len(store.entries(target)) <= 3:
            break  # never empty the hot tier
        if not _llm_confirm_sink(e):  # LLM unavailable/unsure -> skip (conservative)
            continue
        _sink_entry(store, client, target, e, meta, stat, dry)
        processed += 1

    # --- b) merge overlapping rules (needs LLM semantics; skip without key) ---
    if not os.environ.get("LLM_API_KEY", ""):
        return
    entries = store.entries(target)
    merged_any = True
    merge_count = 0
    while merged_any and merge_count < MAX_MERGE_PER_RUN:
        merged_any = False
        entries = store.entries(target)
        for i in range(len(entries)):
            for j in range(i + 1, len(entries)):
                a, b = entries[i], entries[j]
                if not (_topic_overlap(a, b) or _ratio(a, b) >= MERGE_RATIO):
                    continue
                meta_a = meta_store.get_entry(a) or {}
                meta_b = meta_store.get_entry(b) or {}
                if _is_protected_rule(a, meta_a) or _is_protected_rule(b, meta_b):
                    continue
                if len(a) < 60 or len(b) < 60:
                    continue
                _merge_pair(store, client, target, a, b, meta_a, stat, dry)
                merged_any = True
                merge_count += 1
                break
            if merged_any:
                break


def main() -> None:
    dry = "--dry-run" in sys.argv
    no_email = "--no-email" in sys.argv
    LOG_DIR.mkdir(exist_ok=True)

    store = LocalStore()
    client = ColdStoreClient()

    # cold-tier availability (read-only probe)
    cold_ok = True
    try:
        client.stats()
    except Exception:
        cold_ok = False

    stat = {
        "overflowed": 0, "updated": 0, "deleted": 0, "merged": 0, "kept": 0, "errors": 0,
        "sunk": 0, "sink_dry": [], "merge_dry": [], "merge_skipped": 0,
    }
    lines = [f"# MemoryCore weekly maintenance {datetime.now():%Y-%m-%d %H:%M}",
             f"mode: {'DRY-RUN' if dry else 'exec'} | cold tier: {'OK' if cold_ok else 'unreachable (degraded)'}"]

    for target in ("memory", "user"):
        before_pct = store.usage_pct(target)
        r = run_overflow(store, client, target)
        for k in stat:
            if k in r:
                stat[k] += r[k]
        after_pct = store.usage_pct(target)
        lines.append(f"\n## {target}  overflow: {before_pct}% -> {after_pct}%")

        # smart tidy when hot tier still above soft threshold
        if after_pct > SORT_THRESHOLD * 100 and cold_ok:
            t_stat = {"sunk": 0, "merged": 0, "errors": 0, "sink_dry": [], "merge_dry": [], "merge_skipped": 0}
            smart_tidy(store, client, target, t_stat, dry)
            stat["sunk"] += t_stat["sunk"]
            stat["merged"] += t_stat["merged"]
            stat["errors"] += t_stat["errors"]
            post = store.usage_pct(target)
            lines.append(f"  smart tidy: sunk={t_stat['sunk']} merged={t_stat['merged']} merge_skipped={t_stat['merge_skipped']} -> {post}%")
            if t_stat["sink_dry"]:
                lines.append("  [dry] would sink: " + "; ".join(t_stat["sink_dry"]))
            if t_stat["merge_dry"]:
                for x in t_stat["merge_dry"]:
                    lines.append(f"  [dry] would merge: {x[0]} + {x[1]} -> {x[2]}")
        else:
            lines.append(f"  smart tidy: skipped (usage {after_pct}% <= {SORT_THRESHOLD*100:.0f}% or cold tier unreachable)")

    # cold-tier maintenance
    try:
        m = run_maintenance(client)
        lines.append(f"\n## cold-tier maintenance\n{m}")
    except Exception as e:
        lines.append(f"\n## cold-tier maintenance\nerror: {e}")
        stat["errors"] += 1

    lines.append(f"\n## summary\noverflowed={stat['overflowed']} sunk={stat['sunk']} merged={stat['merged']} errors={stat['errors']}")
    for target in ("memory", "user"):
        lines.append(f"{target}: {store.usage_pct(target)}% ({store.char_count(target)}/{MEMORY_LIMIT})")

    report = "\n".join(lines)
    print(report)

    if dry:
        return
    # persist report
    fp = LOG_DIR / f"weekly-{datetime.now():%Y%m%d-%H%M}.md"
    fp.write_text(report + "\n")
    # optional notification: only when MEMORYCORE_NOTIFY_SCRIPT is set
    if not no_email and NOTIFY_SCRIPT:
        if not Path(NOTIFY_SCRIPT).exists():
            print(f"[warn] notifier script not found: {NOTIFY_SCRIPT}", file=sys.stderr)
        else:
            try:
                r = subprocess.run(["bash", str(NOTIFY_SCRIPT), "MemoryCore weekly maintenance report"],
                                   input=report, text=True, timeout=120, capture_output=True)
                if r.returncode != 0:
                    print(f"[warn] notification failed rc={r.returncode}: {r.stderr[-300:]}", file=sys.stderr)
            except Exception as e:
                print(f"[warn] notification failed: {e}", file=sys.stderr)
    elif not no_email:
        print("[info] MEMORYCORE_NOTIFY_SCRIPT not set, notification skipped (report saved under logs/)")


if __name__ == "__main__":
    main()
