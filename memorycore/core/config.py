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

# Fastembed cache directory — where the ONNX embedding model lives.
# The model is bundled with the package (memorycore/assets/fastembed-cache/)
# and auto-deployed into this directory on first use (never downloads).
# Default ~/.memorycore/fastembed; override with MNEMOSYNE_FASTEMBED_CACHE_DIR.
MNEMOSYNE_FASTEMBED_CACHE_DIR = os.environ.get(
    "MNEMOSYNE_FASTEMBED_CACHE_DIR",
    os.path.normpath(os.path.join(MNEMOSYNE_DATA_DIR, "..", "fastembed")),
)
if "MNEMOSYNE_FASTEMBED_CACHE_DIR" not in os.environ:
    os.environ["MNEMOSYNE_FASTEMBED_CACHE_DIR"] = MNEMOSYNE_FASTEMBED_CACHE_DIR

# Local embedding model — fastembed built-in (no API key needed).
# Two models are bundled in the package:
#   BAAI/bge-small-zh-v1.5  (default) — Chinese-optimised, 512-dim, MIT
#   BAAI/bge-small-en-v1.5            — English-optimised, 384-dim, MIT
# Switch languages by setting this env var; the model is already on disk
# (zero download).  For other languages or custom models, set
# MNEMOSYNE_EMBEDDING_API_URL to use an external embedding API instead
# (e.g. Ollama, OpenAI, local llama.cpp).
if "MNEMOSYNE_EMBEDDING_MODEL" not in os.environ:
    os.environ["MNEMOSYNE_EMBEDDING_MODEL"] = "BAAI/bge-small-zh-v1.5"
# Deliberately do NOT set MNEMOSYNE_EMBEDDING_API_URL — the library uses
# local fastembed by default.  No HF_ENDPOINT / NO_EMBEDDINGS flags are set;
# the model is available locally so zero network is required.

# ---- LLM (optional, merge enhancement for ambiguous groups) ----
# Used by overflow merge when rule-based dedup cannot resolve an ambiguous
# same-topic group. Falls back to pure rules when unset or on any failure.
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT = 15.0

# ---- 冷热判定关键词 (热数据特征: 每轮都要用的偏好/准则/纠正/常量) ----
HOT_KEYWORDS = [
    "偏好", "准则", "原则", "禁止", "必须", "习惯", "要求",
    "纠正", "用户拍板", "用户明确", "零容忍", "不允许",
    "环境常量", "交互习惯", "行为准则", "写作风格", "回答风格",
]

# ---- 过时状态标记 (E2: 状态记录类不再反映当前状态 → forget 不迁移) ----
STALE_MARKERS = [
    "已修复", "已解决", "已切换", "已退役", "已停用",
    "已迁移", "已删除", "已完成", "不再使用", "已废弃",
]
