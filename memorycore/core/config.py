#!/usr/bin/env python3
"""core/config.py — MemoryCore configuration

Thresholds / paths / remote memory service URL / timeouts.
All values can be overridden via environment variables.
"""
import os
from pathlib import Path

# ---- capacity (chars) ----
CHAR_LIMIT_MEMORY = 5000
CHAR_LIMIT_USER = 5000

SOFT_THRESHOLD = 0.60   # 60% of limit: overflow once before writing new hot data
HARD_THRESHOLD = 0.80   # 80%: force overflow
TARGET_RATIO = 0.40     # overflow target: <=40%

# ---- hot tier paths (Hermes local memory, overridable) ----
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", os.path.expanduser("~/.hermes/memories")))
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"

# ---- cold tier engine (dual backend: local in-process or remote MCP) ----
# MEMORYCORE_COLD_BACKEND: "local" (default) or "remote"
#   - local:  uses mnemosyne-memory in-process (zero external services)
#   - remote: connects to an MCP memory service via MNEMOSYNE_URL
COLD_BACKEND = os.environ.get("MEMORYCORE_COLD_BACKEND", "local")

# ---- cold tier — remote mode ----
# Required when COLD_BACKEND=remote: point MNEMOSYNE_URL at any MCP memory
# service exposing remember/recall/update/forget/stats
# (see examples/cold-store-contract.md).
MNEMOSYNE_URL = os.environ.get("MNEMOSYNE_URL", "")
MNEMOSYNE_TIMEOUT = 10.0

# ---- cold tier — local mode (mnemosyne-memory in-process) ----
# Data directory for the local SQLite database.
# Default ~/.memorycore/data; override with MNEMOSYNE_DATA_DIR (the env var
# that the mnemosyne library itself recognises).
MNEMOSYNE_DATA_DIR = os.environ.get(
    "MNEMOSYNE_DATA_DIR",
    os.path.expanduser("~/.memorycore/data"),
)
# Ensure the env var is set for the mnemosyne library to pick up.
if "MNEMOSYNE_DATA_DIR" not in os.environ:
    os.environ["MNEMOSYNE_DATA_DIR"] = MNEMOSYNE_DATA_DIR

# ---- cold tier — local mode: embedding API (ollama / qwen3) ----
# MEMORYCORE_EMBED_URL: ollama (or compatible) embedding API base URL.
# Default http://localhost:11434/v1 — ollama's OpenAI-compatible endpoint.
# Set to "" to use a different provider or the legacy fastembed path.
MEMORYCORE_EMBED_URL = os.environ.get("MEMORYCORE_EMBED_URL", "http://localhost:11434/v1")

# MEMORYCORE_EMBED_MODEL: embedding model name to use via the API.
# Default qwen3-embedding:0.6b (1024-dim). Must match a model pulled in ollama.
MEMORYCORE_EMBED_MODEL = os.environ.get("MEMORYCORE_EMBED_MODEL", "qwen3-embedding:0.6b")

# Feed these into the mnemosyne library so LocalBackend uses ollama.
if "MNEMOSYNE_EMBEDDING_API_URL" not in os.environ:
    os.environ["MNEMOSYNE_EMBEDDING_API_URL"] = MEMORYCORE_EMBED_URL
if "MNEMOSYNE_EMBEDDING_MODEL" not in os.environ:
    os.environ["MNEMOSYNE_EMBEDDING_MODEL"] = MEMORYCORE_EMBED_MODEL
# qwen3-embedding:0.6b outputs 1024-dim vectors.
if "MNEMOSYNE_EMBEDDING_DIM" not in os.environ:
    os.environ["MNEMOSYNE_EMBEDDING_DIM"] = "1024"

# ---- LLM (optional, merge enhancement for ambiguous groups) ----
# Used by overflow merge when rule-based dedup cannot resolve an ambiguous
# same-topic group. Falls back to pure rules when unset or on any failure.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT = 15.0

# ---- cold tier capacity hard gate (Task C) ----
# Before writing to the cold tier, check total entries: beyond SOFT_LIMIT run
# one maintenance pass first; beyond HARD_LIMIT force maintenance in a loop
# until back under SOFT_LIMIT (max 5 rounds).  With sqlite-vec enabled the
# retrieval latency is no longer the bottleneck, so limits can be generous.
COLD_SOFT_LIMIT = 6000   # soft: run maintenance once before writing
COLD_HARD_LIMIT = 10000  # hard: force maintenance to shrink before writing

# ---- 冷热判定关键词 (热数据特征: 每轮都要用的偏好/准则/纠正/常量) ----
HOT_KEYWORDS = [
    "偏好", "准则", "原则", "禁止", "必须", "习惯", "要求",
    "纠正", "用户拍板", "用户明确", "零容忍", "不允许",
    "环境常量", "交互习惯", "行为准则", "写作风格", "回答风格",
]

# ---- 过时状态标记 (E2: 状态记录类不再反映当前状态 → forget 不迁移) ----
# P2-2: 仅保留已完成的过时标记; 进行时词 (落地中/进行中/规划中/待定/未完成)
# 移至 core/maintenance._LONG_STALE_MARKERS 交 LLM 判定
STALE_MARKERS = [
    "已修复", "已解决", "已切换", "已退役", "已停用",
    "已迁移", "已删除", "已完成", "不再使用", "已废弃",
]

# ---- hot-tier metadata aging (Phase 2, 2026-08-16) ----
# state-typed entries (historical decisions / status records) retire to the
# cold tier N days after write; rule-typed entries (precepts/preferences)
# never retire by age, only eligible for LLM compression.
STATE_TTL_DAYS = 7
RULE_COMPRESS_DAYS = 30
# sidecar metadata filename suffix: MEMORY.md -> MEMORY.meta.json
META_SUFFIX = ".meta.json"

# ---- rule invalidation signals (Phase 3, 2026-08-20) ----
# Tiered protection: B-class rules (domain/project preferences) may be
# retired by layered evidence (merge / compress / sink / dedup); A-class
# meta-rules (behavior/interaction/writing-style precepts), red-line rules
# and high-importance entries are never touched (only merge/compress).

# S2 completion re-check (rule -> state retype: historical decision records
# wearing rule metadata)
RULE_RETYPE_DAYS = 60              # embedded date must be >= N days old
RULE_RETYPE_MIN_DONE_MARKERS = 2   # completion-marker hits required
RULE_RETYPE_DONE_MARKERS = [
    # same source as classifier done_markers + audit/fix/finalize extensions
    "拍板", "已配置", "已停", "已切换", "已退役", "退役", "已装", "已加",
    "停训", "停用", "已删", "已完成", "半搬", "已重开", "已停用", "已清除",
    "已启用", "已禁用", "已定稿", "已定案",
    "已修", "已禁", "已关闭", "已修复", "已固化", "已改用", "已迁",
    "已落", "已建", "已改", "已切", "已恢复", "已回退", "不再使用", "无入侵",
]
RULE_RETYPE_BEHAVIOR_MARKERS = [
    # hard condition: zero behavior-directive words (same source as
    # classifier behavior_markers + F4 pending-prefix exclusions)
    "用户要求我", "用户偏好", "红线", "禁止", "习惯", "行为准则",
    "用户纠正", "交互习惯", "写作风格", "回答风格", "零容忍", "PM准则",
    "准则", "偏好",
    "未定稿", "未拍板", "未定案", "待定稿", "待拍板", "待定案", "需用户拍板",
]

# S4 topic-activity proxy + stub-sink (dormant B-class rule -> full text to
# cold tier + small pointer stub left in the hot tier)
RULE_STUB_IDLE_DAYS = 45           # stub eligibility: days without update
ACTIVITY_WINDOW_DAYS = 30          # topic-dormancy window
# Set ACTIVITY_LOG_ENABLED=0 to disable the query activity log; S4 stub-sink
# is then disabled entirely (mechanism degrades to previous behaviour).
ACTIVITY_LOG_ENABLED = os.environ.get("ACTIVITY_LOG_ENABLED", "1") != "0"
ACTIVITY_LOG_RETENTION_DAYS = 45   # log rolling retention
ACTIVITY_LOG_MAX_BYTES = 256 * 1024
ACTIVITY_LOG_FILE = MEMORY_DIR / "activity.jsonl"
MAX_STUB_PER_RUN = 3               # stubs per overflow run (gradual, never drains)
STUB_MAX_CHARS = 40                # pointer length cap
STUB_PREFIX = "[规则指针]"          # pointer prefix: identification / reconcile anchor
STUB_GC_MIN_AGE_DAYS = 1           # stub GC min age (no create-then-collect thrash)

# S5 cross-tier redundancy cleanup (cold tier already holds an equivalent
# copy -> drop the hot copy; only probed after this many idle days to keep
# overflow cheap)
CROSS_DEDUP_MIN_IDLE_DAYS = RULE_COMPRESS_DAYS

# S6 protection line: importance >= this = explicitly high value, protected
# like A-class / red-line rules
IMPORTANCE_PROTECT = 0.9

# S3 clustering embedding channel (optional enhancement; falls back to
# lexical-only when the embedding API is unavailable)
CLUSTER_EMBED_THRESHOLD = 0.85
