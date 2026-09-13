# Architecture

## Overview

MemoryCore is an MCP server that manages a two-tier memory for LLM agents:

- **Hot tier** — local markdown files (`MEMORY.md`, `USER.md`). Fast, always available, holds the knowledge the agent uses every turn: preferences, rules, corrections, environment constants. Capacity is measured in **characters** (not bytes — important for CJK content).
- **Cold tier** — any remote MCP memory service that satisfies the [cold store contract](examples/cold-store-contract.md). Holds low-frequency facts. Unbounded.

The value is in the *policing*: keeping hot memory small enough to stay inside the agent's context budget, without ever dropping knowledge silently.

## Components

```
memorycore/
├── server.py            # MCP server; routing + capacity gates + audit tools
├── local_store.py       # hot tier: char-counted sectioned file store, atomic lock
├── cold_store_client.py # cold tier: MCP streamable-http client (5 tools)
└── core/
    ├── config.py        # thresholds, paths, URL, timeouts (env-overridable)
    ├── classifier.py    # cold/hot/stale routing rules (+ SAFE-JUDGE wrappers)
    ├── judge.py         # SAFE-JUDGE v3: deterministic rule/state/ambiguous
    ├── overflow.py      # six-step overflow + unified cache-policy budget selector
    ├── metadata.py      # sidecar metadata, judge audit fields, _ts_anchor
    └── maintenance.py   # cold-tier governance pass
```

## Cache policy V2 and SAFE-JUDGE v3 (2026-09-13)

The hot tier is modelled as a cache.  Typed content is not split into
separate eviction ladders; `rule`, `state` and pointer `stub` entries share
one candidate pool ordered by a single rank:

```
priority = w_eff × protected(×3.0) × kw_sink(×0.5) × freshness(×9.0)
```

where freshness applies when `written_at` / `last_recall_hit_at` is inside
`RULE_MIN_RESIDENCY_DAYS=7` (timestamps go through `metadata._ts_anchor`).
`RULE_BUDGET_CHARS=2000` equals the overflow target 40% of the 5000-char
limit.  Eviction is cold-write-first and bounded by `MAX_EVICT_PER_RUN=3`.
Normal eviction leaves a ≤40-char pointer stub with a `cold_id`; recall by
`handle` performs a direct lookup and marks `page_fault=true`, feeding the
write-back path.  With an explicit zero pointer budget the full text is
written to the cold tier and the local text is removed without a pointer.

SAFE-JUDGE v3 (`core/judge.py`) replaces lexical double-classification with
a deterministic ternary verdict.  `rule` stays hot, `state` migrates cold
(cold-write-first), and `ambiguous` is forced hot with a recorded review
deadline (first review +7d, `JUDGE_AMBIGUOUS_LRU_DAYS=21` pointer fallback,
max 2 reviews).  Synchronous typing uses zero LLM; only weekly maintenance
may spend a recorded LLM finalisation.  Rollback switches are documented in
`README.md` (`MEMORYCORE_CACHE_POLICY_V2=0`, `RULE_MIN_RESIDENCY_DAYS<=0`,
`GRACE_MULT<=0`, `MEMORYCORE_JUDGE_V3_ENABLED=0`,
`MEMORYCORE_JUDGE_AMBIGUOUS_HOLD=0`).

## Data Flow

### Write path (`store_fact`)

```
content + importance
      │
      ▼
 classify()
 ├─ STALE ──────────────► dropped (returned to caller, not written anywhere)
 ├─ COLD ───────────────► cold_store_client.remember()  →  "cold_stored"
 └─ HOT
      │
      ▼  capacity check on hot tier
      ├─ < soft (60%)  ──► write local
      ├─ ≥ soft        ──► run overflow once, then write local
      └─ ≥ hard (80%) ──► force overflow (loop to ≤ target), then write local
      │
      ▼
   local write fails ──► fallback: write to cold tier ("cold_stored_fallback")
```

### Overflow (`trigger_overflow`)

Six steps, always *cold-first* (write/verify remote before deleting local):

1. **Capacity baseline** — count entries and chars per hot file.
2. **Dedup check** — recall the cold tier for near-duplicates of each candidate.
3. **Stale filtering** — drop entries matching stale markers (≤80 chars guard so long mixed records survive).
4. **Merge** — same-topic cold entries are merged before writing (Union-Find grouping).
5. **Safe write** — remember / update / delete-local in strict order; any remote failure keeps the local entry.
6. **Verify** — final capacity report; hard-threshold mode loops until ≤ target ratio (bounded to 3 rounds).

### Maintenance (`run_cold_storage_maintenance`)

1. Enumerate the cold tier (native list if available, else multi-query recall).
2. Merge duplicate memories (keep the most complete).
3. Clean stale entries (same classifier as overflow).
4. Resolve conflicts (same topic, divergent content).
5. Verify embedding integrity (`stats()` total == embeddings).

## Degradation & Consistency

| Failure | Behavior |
|---|---|
| Cold tier unreachable | `store_fact` returns `error` (never silently drops); overflow keeps local entries; `get_memory_usage` returns local status + `cold.error` |
| Local write fails | Falls back to cold tier (`cold_stored_fallback`) |
| Session invalidated (4xx) | Client clears session and reconnects |
| Concurrent writers | Independent `.lock` files, same convention as the agent's own memory store |
