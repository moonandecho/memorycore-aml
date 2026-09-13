# MemoryCore — AML Online-Governance Submission

[English](README.md) | [简体中文](README.zh-CN.md)

**MemoryCore** is a memory governance layer for LLM agents: agents accumulate memory fast — preferences, facts, decisions — and memory that is not maintained quietly degrades. MemoryCore keeps the hot tier within budget, the cold tier findable, and every write deduplicated and mergeable.

This repository is the **Agent Memory Leaderboard (AML) submission**. It runs MemoryCore's verified governance mechanisms **online** on the AML write/retrieval paths:

- **Write governance on `/add`** — message → fact fragments → stale filter → semantic dedup/merge → cold-tier write (author-scoped).
- **Retrieval governance on `/search`** — in-sample recall (top_k up to 100) → similarity × time-decay ranking (90-day half-life, high-importance memories exempt) → AML-format evidence.
- **Strict per-user isolation** — `user_id` maps 1:1 to the storage `author_id`; cross-user retrieval is impossible (SQL-layer filter, test-covered).
- **No external API calls at evaluation time** — embeddings run locally (ollama + `qwen3-embedding:0.6b`, 1024-dim), storage is an in-process SQLite engine ([mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/)).

Upstream open-source project: [moonandecho/origin-memorycore](https://github.com/moonandecho/origin-memorycore) (MIT). This repo adds the AML HTTP adapter `memorycore/aml_server.py` and author-scoped identity passthrough on top of it.

**Full method disclosure, deployment notes, reproducible tests and honest known-boundaries section: [AML-COMPETITION.md](AML-COMPETITION.md)** (English: [AML-COMPETITION.en.md](AML-COMPETITION.en.md)).

## Quick start

### Docker (recommended)

```bash
docker build -t memorycore-aml .
docker run -p 8000:8000 -v aml-data:/data memorycore-aml
```

> The image preloads the embedding model (`qwen3-embedding:0.6b`, ~639 MB) at build time — container startup needs no network. The entrypoint keeps a fallback that pulls the model at runtime only if it is missing (e.g. a custom `MEMORYCORE_EMBED_MODEL`). The data volume `/data` persists the SQLite memory store across restarts.

Smoke test:

```bash
curl http://localhost:8000/health
curl -X POST http://localhost:8000/add -H "Content-Type: application/json" -d '{
  "request_id": "eval:smoke:0",
  "messages": [{"role": "user", "content": "The user is a software engineer."}],
  "user_id": "eval:smoke:u1",
  "session_id": "eval:smoke:s0"}'
curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d '{
  "query": "What is the user\u0027s occupation?",
  "options": ["A. software engineer", "B. teacher"],
  "user_id": "eval:smoke:u1", "top_k": 100}'
```

### Bare metal

```bash
python3 -m venv .venv && source .venv/bin/activate   # 推荐: 独立 venv, 依赖 mcp>=2,<3 (已知兼容 2.0.0/2.2.0)
pip install .
ollama serve &
ollama pull qwen3-embedding:0.6b
MNEMOSYNE_DATA_DIR=/data python -m memorycore.aml_server   # 0.0.0.0:8000 by default
```

## HTTP API

All endpoints are plain REST, served on the MCP server streamable-http app (uvicorn). Optional auth via `AML_API_KEY` (Bearer / Token / X-Api-Key).

| Endpoint | Description |
|---|---|
| `POST /add` | AML write: messages → fact fragments → stale filter → semantic dedup/merge → author-scoped cold-tier write. Returns 200 only after the write is committed (no async task IDs). |
| `POST /search` | AML recall: author-scoped in-sample recall → time-decay ranking → AML-format evidence list. `options` are appended to the query for a fallback recall pass (retrieval context only — never used to generate answers or write memory). |
| `GET /health` | Liveness. Returns 2xx even when storage is degraded (`status: degraded` + `storage.error`), so the platform can distinguish crash from dependency outage. |

Error-code semantics:

- `400` — malformed request (missing `request_id`/`user_id`/`session_id`, non-array `messages`, …)
- `401` — auth required but missing/invalid (only when `AML_API_KEY` is set)
- `500` — storage backend unavailable (e.g. embedding service down); transient, safe to retry on 5xx

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `AML_HOST` / `AML_PORT` | `0.0.0.0` / `8000` | HTTP listen address |
| `AML_API_KEY` | empty (no auth, smoke mode) | when set, Add/Search require Bearer/Token/X-Api-Key |
| `MNEMOSYNE_DATA_DIR` | `~/.memorycore/data` | SQLite data directory (point evaluations at a dedicated dir) |
| `MEMORYCORE_EMBED_URL` | `http://localhost:11434/v1` | embedding API (ollama or any OpenAI-compatible server) |
| `MEMORYCORE_EMBED_MODEL` | `qwen3-embedding:0.6b` | embedding model (1024-dim) |
| `LLM_API_KEY` | empty (LLM off) | optional LLM enhancement key (empty = pure rules, safe) |
| `LLM_BASE_URL` / `LLM_MODEL` | `https://api.deepseek.com` / `deepseek-v4-flash` | optional LLM endpoint/model |

> **LLM enhancement is optional and off by default.** At evaluation time this
> submission makes **no external API calls** (embeddings are local ollama;
> LLM features never run on the `/add` / `/search` paths). The optional LLM
> (hot-tier compression / dormancy judgement / merge confirmation) is
> **env-only**: file sources (`~/.hermes/.env` / `~/.hermes/config.yaml`)
> stay disabled unless `MEMCORE_LLM_FILE_SOURCES=1` is set explicitly, so a
> key belonging to another tool (e.g. Hermes) is never picked up silently.
> Safety valves: `MEMCORE_LLM_ENABLED=0`, per-run call caps
> (`MEMCORE_LLM_MAX_CALLS` / `MEMCORE_LLM_COLD_MAX_CALLS`, default 8) and
> fail-backoff. LLM state is always visible in stats/reports/logs.
> Self-check: `python -m memorycore.llm_check [--live]` (live check bills at
> most one max_tokens=1 completion, ~1e-5 yuan).

## Architecture

```
AML HTTP (MCP server custom routes: /add /search /health, optional Bearer/Token/X-Api-Key)
   │
   ├─ Add: fact splitting → stale filter → semantic dedup/merge → cold-tier write (author_id=user_id)
   ├─ Search: in-sample recall (author_id filter, top_k≤100) → time-decay ranking → AML format
   └─ Health: 2xx liveness (degraded when storage is down)
        │
        ▼
MemoryCore cold-store client (per-user engine, in-process SQLite + vector index)
        │
        ▼
embedding: ollama + qwen3-embedding:0.6b (local, no external API dependency)
```

The underlying governance layer (hot/cold routing, write-time normalization dedup, capacity control, overflow, recycle bin) is documented in the [upstream project](https://github.com/moonandecho/origin-memorycore).

## Upstream governance sync (R1–R3)

This AML submission keeps its adapter (`memorycore/aml_server.py`, author-scoped
identity, per-user engines, container contract) unchanged, and carries the
upstream origin-memorycore R1–R3 governance core as the shared implementation
underneath.  AML `/add` and `/search` already route through this core
(`classify`, `_find_best_match` / `_merge_two_entries`, `_apply_decay`,
`ColdStoreClient`), so the R1–R3 fixes apply online without changing the HTTP
contract.

- **R1 — cache policy V2 / SAFE-JUDGE v3 / `_ts_anchor`**: unified LRU
  candidate pool with `GRACE_MULT=9.0`, `RULE_BUDGET_CHARS=2000`,
  `RULE_MIN_RESIDENCY_DAYS=7`, `MAX_EVICT_PER_RUN=3`,
  `HIT_STRONG_COS=0.48`, deterministic zero-LLM ternary typing and the
  shared timestamp entry (`metadata._ts_anchor`, 300 s future tolerance).
- **R2 — privacy**: all bundled fixtures are synthetic; no real identifier,
  path or corpus appears in the tree.
- **R3 — realistic synthetic corpus**: 409 short real-distribution queries;
  longest common substring between fixture rules and queries ≤7 chars;
  replay baseline is frozen to `fault_rate=7.0%`, `relative_drop=83.1%`.
- **Release isolation**: tests point `MEMORY_DIR` / `MNEMOSYNE_DATA_DIR` at a
  temp runtime, LLM file sources default OFF (opt-in
  `MEMCORE_LLM_FILE_SOURCES=1`), and no `~/.hermes` production config is read.

### Hot-tier cache policy V2 (LRU, 2026-09-13)

Cache-policy V2 turns hot-tier retention into one unified cache:

- **One candidate pool.** `rule`, `state` and pointer `stub` entries are no
  longer handled by separate eligibility gates. Every entry participates in
  the same candidate pool, ordered by a single rank:
  `w_eff × protected(×3.0) × kw_sink(×0.5) × freshness(×9.0 when within
  RULE_MIN_RESIDENCY_DAYS)`.
- **Budget-first eviction.** Rule ecology is capped at
  `RULE_BUDGET_CHARS=2000` (= `int(CHAR_LIMIT_MEMORY × TARGET_RATIO)`,
  i.e. the same 40% target as overflow). Eviction is cold-write-first:
  the local entry changes only after the cold tier confirms, and
  `MAX_EVICT_PER_RUN=3` bounds full-text evictions per round.
- **Two-stage output.** A normal eviction leaves a ≤40-char pointer stub
  (`STUB_MAX_CHARS=40`, handle ≤ `STUB_HANDLE_MAX_CHARS=20`) with the
  `cold_id`; `memorycore_recall(..., handle="...")` bypasses the semantic
  threshold, recalls by the stub topic, marks `page_fault=true`, and feeds
  the write-back/restore path. At explicit `--budget 0` / zero pointer
  budget the tool goes cold-only: full text is written to the cold tier and
  the local text is deleted without a pointer (`budget_semantics=
  T3_cold_only_zero_pointer_budget` in the replay fixture).
- **No permanent residency.** All-protected / red-line / `importance>=0.9`
  / `protect_override` entries are still evictable under enough pressure —
  protection is only the ×3 multiplier. Pinned stress case:
  `tests/test_cache_policy_v2.py::test_no_permanent_residency_all_protected`.
- **Freshness is a multiplier, not an exemption.**
  `RULE_MIN_RESIDENCY_DAYS=7` covers `written_at` / `last_recall_hit_at`
  through the shared `_ts_anchor` time entry; inside that window rank ×
  `GRACE_MULT=9.0` (calibrated in the design with the bundled synthetic
  R2 snapshot fixture, 25 entries; `tools/residency_dryrun.py` reproduces
  the fresh-window ordering). It is still fully evictable when pressure
  is sufficient.
- **Activity decides lifetime, not age.** Weight starts at
  `WEIGHT_INIT=1.0`, decays with a 30-day half-life, gains +1.0 on a strong
  semantic hit (`HIT_STRONG_COS=0.48`, one increment per scan per rule) or
  +0.3 weak hit when embedding is unavailable, and is capped at
  `WEIGHT_MAX=5.0`. Default kw-sinkable content gets
  `WEIGHT_KWSINK_MULT=0.5`.

### SAFE-JUDGE v3 (2026-09-13)

`memorycore/core/judge.py` replaces the old sequence of lexical
`classify()` + `should_keep_local()` double judgements with one
deterministic ternary verdict:

- `state` → cold migration (cold-write-first);
- `rule` → hot, stamped with `judge_v3` audit fields;
- `ambiguous` → force hot, write `judge_review_at` (`+7d` first review,
  `JUDGE_AMBIGUOUS_LRU_DAYS=21` A1 pointer fallback, max 2 reviews). The
  synchronous typing path calls no LLM; only weekly maintenance may spend
  one explicitly recorded LLM confirmation. A final `rule` verdict gets a
  `JUDGE_RESOLVED_RULE_GRACE_DAYS=14` audit grace.
- Strong rule and ambiguous content are force-kept hot at the write
  entrance; ambiguous local-write failure returns an error instead of a
  silent cold fallback (`DESIGN-DEVIATIONS.md` §6.5).

### Rollback / kill switches

| Switch | Default | Effect |
|---|---|---|
| `MEMORYCORE_CACHE_POLICY_V2=0` | `1` | Restore the legacy qualified candidate pool (protected eligibility exemption / age gates) while keeping cold-write-first. `PROTECT_SKIP_LRU=1` only warns (deprecated). |
| `RULE_MIN_RESIDENCY_DAYS <= 0` | `7` | Disable only the fresh-window multiplier (exact legacy pure-rank ordering). |
| `GRACE_MULT <= 0` | `9.0` | Same rollback as above at the rank multiplier entry. |
| `MEMORYCORE_RULE_BUDGET_ENABLED=0` | `1` | Disable the rule-budget eviction layer (hard 5000-char backstop still applies). |
| `MEMORYCORE_JUDGE_V3_ENABLED=0` | `1` | Return to lexical v2 typing (`CLASSIFIER_V2_ENABLED` selects v2/v1); known attack baseline 20/29. |
| `MEMORYCORE_JUDGE_AMBIGUOUS_HOLD=0` | `1` | Treat ambiguous as rule (binary behaviour, no review deadline / no hot hold). |
| `MEMCORE_LLM_FILE_SOURCES=1` | `0` | Opt in to whitelisted `~/.hermes/.env` / `config.yaml` LLM file sources; never on by default. |

Constants (`memorycore/core/config.py`): `RULE_BUDGET_CHARS=2000`,
`INDEX_BUDGET_CHARS=800`, `RULE_MIN_RESIDENCY_DAYS=7`, `GRACE_MULT=9.0`,
`WEIGHT_INIT=1.0`, `WEIGHT_PROTECT_MULT=3.0`, `WEIGHT_KWSINK_MULT=0.5`,
`WEIGHT_HALF_LIFE_DAYS=30`, `HIT_STRONG_COS=0.48`, `MAX_EVICT_PER_RUN=3`,
`MAX_STUB_PER_RUN=3`, `STUB_MAX_CHARS=40`, `JUDGE_AMBIGUOUS_REVIEW_DAYS=7`,
`JUDGE_AMBIGUOUS_LRU_DAYS=21`, `JUDGE_RESOLVED_RULE_GRACE_DAYS=14`.

## Reproducible synthetic fixtures

The release tree ships neutral synthetic fixtures under
`tests/fixtures/synthetic/` (25 `notehub` rules: 19 rule + exactly 6
completed/history state entries; 5 USER entries; sidecar metadata; 409
realistic timestamped activity queries — short paraphrases, lexical-only
follow-ups, genuine off-topic noise, gated low-information messages, and
action commands; no rule text is copied into queries). No production memory is
required to run the fixture-backed acceptance paths. Rebuild the derived silver fixture with the
bundled one-shot generator:

```bash
.venv/bin/python tools/build_fault_replay_fixture.py \
  --activity tests/fixtures/synthetic/activity.jsonl \
  --rules    tests/fixtures/synthetic/MEMORY.md \
  --out      tests/fixtures/fault_replay_silver.json
```

Verified synthetic baselines:

| Check | Result |
|---|---|
| fault replay (`replay_fault_rate.py`) | `hits=186/200 faults=14 fault_rate=7.0% baseline=41.5% relative_drop=83.1% pass=True` (R3 realistic synthetic activity; fixture is reproducible synthetic, values differ from production) |
| retype dry-run on synthetic snapshot | `19 rule / 6 state`, state set exactly the 6 bundled targets |
| migration water level | `2598 / 5000 chars (51%)` |
| snapshot budget replay | `--budget 2000` and `--budget 0` both EXIT=0 |
| residency dry-run | 25 entries / 3441 chars / need 1441, `need_satisfied=True`, `new_evictable_when_full=True` |

## Tests (reproducible)

```bash
# Full regression suite (isolated temp runtime, no production paths):
.venv/bin/python -m pytest tests/ -q          # 490 passed

# Fixture-backed acceptance (no ollama required: frozen silver fixture):
.venv/bin/python tools/replay_fault_rate.py \
    --fixture tests/fixtures/fault_replay_silver.json
# -> hits=186/200 faults=14 fault_rate=7.0% relative_drop=83.1% pass=True

.venv/bin/python tools/retype_20260912.py --dry-run \
    --data-dir tests/fixtures/snapshot_20260912 --backup-dir /tmp/retype-backup
# -> 19 rule / 6 state

# AML adapter end-to-end (requires ollama + qwen3-embedding:0.6b):
MNEMOSYNE_DATA_DIR=$(mktemp -d) python3 tests/test_aml.py
```

28 assertions cover: cross-user isolation (A writes, B cannot see), second write of the same fact deduplicated, "plan A → changed to B" merged into one memory, full HTTP path for `/add` `/search` `/health`, options fallback recall, long-message splitting, and error codes.

## Known boundaries (honest disclosure)

- Retrieval uses the storage engine's lexical+vector hybrid ranking; the engine applies a lexical-relevance gate on long queries. The options-fallback recall covers multiple-choice scenes; open-ended English natural questions were verified to recall normally.
- Message-supplied `timestamp` is treated as reference only; ranking uses the persisted write time. `created_at` in responses returns the persisted time (the protocol's "source/persisted time").
- No online cold-tier full-maintenance pass; write-side governance is dedup/merge/stale-filter oriented.

## License

MIT — see [LICENSE](LICENSE). The engine dependency [mnemosyne-memory](https://pypi.org/project/mnemosyne-memory/) and [ollama](https://ollama.com) + qwen3-embedding are MIT / Apache-2.0. See [AML-COMPETITION.md](AML-COMPETITION.md) for the per-component change disclosure.
