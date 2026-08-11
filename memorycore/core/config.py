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
