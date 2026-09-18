#!/usr/bin/env python3
"""core/config.py — MemoryCore configuration (release port R1).

阈值/路径/冷层后端/超时。热层路径支持 MEMORY_DIR 环境变量覆盖;
冷层为双后端 (默认本地 mnemosyne-memory, 可切 MCP 远程).

机制常量与本地运行版源树保持一致 (GRACE_MULT/RULE_BUDGET_CHARS/
RULE_MIN_RESIDENCY_DAYS/WEIGHT_* 等); 仅冷层容量硬闸保留发布版本地
SQLite 标定值 6000/10000 (见 PORT-R1-REPORT.md 有意差异).
"""
import os
from pathlib import Path

# ---- 容量口径 (2026-08-03 修正: chars 为准, 上限 config memory_char_limit) ----
CHAR_LIMIT_MEMORY = 5000
CHAR_LIMIT_USER = 5000

SOFT_THRESHOLD = 0.60   # 60% = 3000 chars, 写新高频前先溢流
HARD_THRESHOLD = 0.80   # 80% = 4000 chars, 强制溢流
TARGET_RATIO = 0.40     # 溢流目标 ≤40% = 2000 chars

# ---- 热层路径 (Hermes 本地 memory) ----
MEMORY_DIR = Path(os.environ.get("MEMORY_DIR", os.path.expanduser("~/.memorycore/memories")))
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

# E13 (2026-09-12): import-time os.environ writes are no longer silent —
# one info log line documents the exported values (paths/URLs/model names,
# no secrets).
import logging as _logging
_logging.getLogger("memorycore.config").info(
    "MNEMOSYNE env export: DATA_DIR=%s EMBEDDING_API_URL=%s "
    "EMBEDDING_MODEL=%s EMBEDDING_DIM=%s",
    os.environ["MNEMOSYNE_DATA_DIR"], os.environ["MNEMOSYNE_EMBEDDING_API_URL"],
    os.environ["MNEMOSYNE_EMBEDDING_MODEL"], os.environ["MNEMOSYNE_EMBEDDING_DIM"])

# ---- LLM (冻结语义, 勿用于新代码!) ----
# 2026-09-12 评审 v2: LLM 配置唯一真相源已迁移至 core/llm_config.py
# (惰性解析 + ~/.hermes/.env 白名单 + config.yaml provider 门控 + 观测三态
#  + 安全阀; 原 _load_hermes_llm_config 的 except: pass 静默缺陷一并移除)。
# 本区块常量仅为兼容旧测试/脚本保留: import 时绑定一次, 运行中改 env 不生效
# (冻结语义)。新代码一律用 llm_config.resolve()。
LLM_API_KEY = os.environ.get("LLM_API_KEY", "")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.deepseek.com")
LLM_MODEL = os.environ.get("LLM_MODEL", "deepseek-v4-flash")
LLM_TIMEOUT = 15.0

# ---- cold tier capacity hard gate (Task C) ----
# 发布版本地 mnemosyne-memory/SQLite 标定 6000/10000, 与 README 契约一致;
# 源树 Mac 远程冷层为 26000/30000。PORT-R1 明确保留发布版容量契约 (有意差异),
# 本轮缓存化与判型机制常量不含容量闸值。
COLD_SOFT_LIMIT = 6000   # soft: run maintenance once before writing
COLD_HARD_LIMIT = 10000  # hard: force maintenance to shrink before writing


# (2026-09-12, 评审 E6): 原 _load_hermes_llm_config() 已删除 — 其
# except: pass 静默吞掉 config.yaml 缺失/损坏/pyyaml 缺失; 且 base_url/model
# 无条件读取、无 provider 门控 (跨源混搭隐患)。yaml 读取现由
# core/llm_config._load_config_yaml_model 承担 (provider 门控 + 失败可见)。

# ---- 冷热判定关键词 (热数据特征: 每轮都要用的偏好/准则/纠正/常量) ----
HOT_KEYWORDS = [
    "偏好", "准则", "原则", "禁止", "必须", "习惯", "要求", "规范",
    "纠正", "用户明确", "零容忍", "不允许",
    "交互习惯", "行为准则", "写作风格", "回答风格",
]

# ---- 过时状态标记 (E2: 状态记录类不再反映当前状态 → forget 不迁移) ----
STALE_MARKERS = [
    # P2-2: 仅保留已完成的过时标记; 进行时词移至 maintenance._LONG_STALE_MARKERS
    "已修复", "已解决", "已切换", "已退役", "已停用",
    "已迁移", "已删除", "已完成", "不再使用", "已废弃",
]

# ---- Phase 2 热层元数据老化 (2026-08-16) ----
# state 型条目 (历史决策/状态记录) 写入 N 天后自动退役下沉冷层
STATE_TTL_DAYS = 7
# rule 型条目 (准则/偏好) N 天未更新后, 溢流时优先 LLM 压缩 (不删)
RULE_COMPRESS_DAYS = 30
# sidecar 元数据文件名后缀: MEMORY.md -> MEMORY.meta.json
META_SUFFIX = ".meta.json"

# ---- Phase 3: rule 失效信号机制 (2026-08-20 设计定稿"分层保护") ----
# 设计: 源树 rule-stale-design (DESIGN-DEVIATIONS.md)
# 约束 1 修正: rule 允许按失效证据分层放弃 (B 类可下沉/合并/删除),
# A 类 (行为/交互/写作风格元准则) 与红线类绝不误伤。

# S2 完成态复核 (rule -> state retype, 穿 rule 衣服的历史决策记录)
RULE_RETYPE_DAYS = 60              # 内嵌日期距今 ≥N 天才有复核资格
RULE_RETYPE_MIN_DONE_MARKERS = 2   # 完成态词命中下限 (实证: 真准则 ≤1, 历史记录 ≥3)
RULE_RETYPE_DONE_MARKERS = [
    # 与 classifier done_markers + STATE_DONE_RESULT_PATTERNS 同源
    # (单一真相源在 core/classifier.py 的 _DONE_MARKERS /
    #  STATE_DONE_RESULT_PATTERNS; 本表为 S2 复核用的子串化平铺,
    #  修改判型词表时必须同步) + Phase 3 扩展 (审计/修复/固化类)
    "拍板", "已配置", "已停", "已切换", "已退役", "退役", "已装", "已加",
    "停训", "停用", "已删", "已完成", "半搬", "已重开", "已停用", "已清除",
    "已启用", "已禁用", "已定稿", "已定案",
    "已修", "已禁", "已关闭", "已修复", "已固化", "已改用", "已迁",
    "已落", "已建", "已改", "已切", "已恢复", "已回退", "不再使用", "无入侵",
    # v2 结果/决定模式子串化 (2026-09-12, DESIGN §Q1):
    "已部署", "已上线", "已交付", "已找回", "已下线",
    "部署成功", "上线成功", "交付成功", "调研成功", "恢复成功", "救回成功",
    "部署完成", "上线完成", "交付完成", "调研完成",
    "部署完毕", "上线完毕", "交付完毕", "调研完毕",
    "四轮闭环交付", "调研结论", "决定不做", "决定不用", "决定不接",
    "决定不碰", "决定不继续", "放弃跟进", "不再考虑",
    "改主意恢复", "复制恢复", "启动成功", "再确认",
]
RULE_RETYPE_BEHAVIOR_MARKERS = [
    # S2 硬条件: 零行为指令词 (与 classifier STRONG_BEHAVIOR + 名词"准则/
    # 偏好" 同源 + F4 待定前缀排除; 单一真相源在 classifier.py, 本表仅为
    # S2 复核用, 修改判型词表时必须同步)
    "用户要求我", "用户偏好", "红线", "禁止", "习惯", "行为准则",
    "用户纠正", "交互习惯", "写作风格", "回答风格", "零容忍", "最高准则",
    "准则", "偏好",
    "未定稿", "未拍板", "未定案", "待定稿", "待拍板", "待定案", "需用户拍板",
]

# S4 主题活性代理 + stub-sink (休眠 B 类 rule → 全文冷层 + 指针留热层)
RULE_STUB_IDLE_DAYS = 45           # stub 资格: 未更新时长下限
ACTIVITY_WINDOW_DAYS = 30          # 主题休眠判定窗口
# ACTIVITY_LOG_ENABLED: E8 惰性解析 (模块尾部 __getattr__), "0" → S4 整体禁用
ACTIVITY_LOG_RETENTION_DAYS = 45   # 日志滚动保留
ACTIVITY_LOG_MAX_BYTES = 256 * 1024
ACTIVITY_LOG_FILE = MEMORY_DIR / "activity.jsonl"
MAX_STUB_PER_RUN = 3               # 每轮溢流 stub 上限 (渐进, 防单轮抽空)
STUB_MAX_CHARS = 40                # 指针长度上限
STUB_PREFIX = "[规则指针]"          # 指针前缀: 识别/reconcile 补盖锚点
# FIX8 口径 (2026-09-13): 指针是页表不是缓存内容。stub GC 与全文候选共用
# 同一个 `_rule_rank` (含 protected 与新鲜窗口乘数); 新建指针另由
# last_evicted_at/retire_count 年龄门槛保护, 没有独立的 stub 宽限档位。
# 测试 tests/test_fix6_runtime.py::test_fix6_stub_gc_uses_unified_rank_no_separate_tier
# 固定低 rank 新鲜 stub 仍先于高 rank 普通 stub 被 GC 的行为。
STUB_GC_MIN_AGE_DAYS = 1           # stub 回收最小年龄 (防刚建即被 GC 抖振)

# S5 跨层冗余清除 (冷层已有等价全文 → 删本地): 闲置 ≥N 天才查冷层 (省 recall 开销)
CROSS_DEDUP_MIN_IDLE_DAYS = RULE_COMPRESS_DAYS

# S6 保护线: importance ≥ 此值 = 用户显式高价值, 与 A 类/红线类同等绝对保护
IMPORTANCE_PROTECT = 0.9

# S3 聚簇嵌入通道 (ollama qwen3, 不可用 → 纯词法降级)
CLUSTER_EMBED_THRESHOLD = 0.85

# ---- Phase 4: 热层规则预算制 (LRU 缓存模型, 2026-08-26 设计定稿) ----
# 设计: /tmp/memorycore-lru-design.md (主设计) + /tmp/memorycore-lru-signal-design.md (活性信号)
# 铁律: 热层无永久保留规则; 保护只是权重乘数; 寿命由活性决定 (LRU 触达语义)
# 2026-09-12 重设计 (DESIGN.md Q2/Q6): 预算与 40% 目标同源 int(5000*TARGET_RATIO)=2000;
# protected 始终只是 ×3 排序乘数 (不前置排除, 见 overflow._select_retirement_candidates)。
RULE_BUDGET_CHARS = 2000            # 规则生态 (rule+stub) 字符硬预算 = TARGET_RATIO 同源 40% 上限
# 常驻目录/页表预算 (CACHE-DESIGN §Q4a): = int(RULE_BUDGET_CHARS × TARGET_RATIO)
# 只用于 plugin 每轮目录注入, 不参与热层内容预算; 单句柄目标 ≤ STUB_MAX_CHARS/2。
INDEX_BUDGET_CHARS = int(RULE_BUDGET_CHARS * TARGET_RATIO)   # 800
STUB_HANDLE_MAX_CHARS = STUB_MAX_CHARS // 2                   # 20
# RULE_BUDGET_ENABLED: E8 惰性解析 (模块尾部 __getattr__), 回滚开关
# 以下三级年龄门槛 (2026-09-12 DESIGN §Q2) 已降级为审计/回滚兼容,
# 不再参与换出资格 (CACHE-DESIGN §Q1/Q6)。保留一个发布周期。
# RULE_MIN_RESIDENCY_DAYS (FIX5 2026-09-13, FIX8 2026-09-13 换挡):
# 新鲜窗口天数。窗口内 written_at/last_recall_hit_at 距 now ≤ N 天 → rank 乘
# GRACE_MULT; 窗口外无乘数。<=0 整体关闭 (rank 逐条等于 FIX5 前纯 _rule_rank,
# 也是回滚口径之一; 另一回滚口径是 CACHE_POLICY_V2=0)。
# FIX8 B2: 不再有"宽限候选档/资格豁免"; 全部条目在同一候选池按唯一 rank 排序。
RULE_MIN_RESIDENCY_DAYS = 7         # 新鲜窗口天数 (排序乘数窗口); <=0 关闭
RULE_MIN_RESIDENCY_IDLE_DAYS = 7    # deprecated 审计/tier 解释
RULE_MIN_RESIDENCY_WARM_DAYS = 14   # deprecated 审计/tier 解释
RULE_MIN_RESIDENCY_ACTIVE_DAYS = 30 # deprecated 审计/tier 解释
WEIGHT_INIT = 1.0                   # 新规则初始权重 (与 30 天前活跃过一次等价起步)
WEIGHT_HIT_INCREMENT = 1.0          # 一次强命中 ≈ 抵消一个半衰期 (30 天)
WEIGHT_MAX = 5.0                    # 权重封顶 (防数值膨胀; 相对排序不变)
WEIGHT_HALF_LIFE_DAYS = 30          # 半衰期 = ACTIVITY_WINDOW_DAYS 同窗 (数学自洽)
# CACHE-POLICY-V2 (2026-09-13): protected 回归统一排序乘数 (always-on),
# 资格豁免/年龄门/ambiguous 资格全部废弃; PROTECT_SKIP_LRU 仅旧策略回滚对照。
WEIGHT_PROTECT_MULT = 3.0           # protected ×3 排序乘数 (非豁免; 足够压力必然可出)
WEIGHT_KWSINK_MULT = 0.5            # kw 可沉型 (should_keep_local=False) 权重减半
# FIX8 B1 (2026-09-13): 新鲜窗口只是第四个排序乘数, 与 protected 同构;
# 不是候选资格/分档。标定过程见 FIX8-REPORT.md §2 / evidence/fix8/：
# realdata MEMORY 快照 21 条, rank 纯排序 1.0~15.0; 预算 2000 时典型缺口
# (need) = 1029 chars; 从低 rank 累加, 需要 9 条旧条目才覆盖 (第 9 条
# rank=8.552)。新写入 weight=1.0, 要使其不在“典型压力会换出的前 9 条”里,
# 取 GRACE_MULT=9.0 > 8.552; 压力足够 (need 继续增大 / 接近全量) 时
# 它只是排在旧条之后, 仍会被统一池选走。<=0 = 关闭 (等价 RULE_MIN...<=0)。
# 数据复算: memorycore/evidence/fix8/calibrate_grace_mult.py。
GRACE_MULT = 9.0                    # 新鲜窗口排序乘数 (有界, 非资格)
MAX_EVICT_PER_RUN = 3               # 单轮全文退役上限 (见下方 P5 口径)
# P5 口径 (FIX8, 固定选择“独立口径”):
#   - MAX_EVICT_PER_RUN 只约束“全文内容换出” (`stat["lru_evicted"]` 与
#     `_handle_rule_stub_sink` 删/换本地内容)，不包含 `_stub_gc` 删除指针；
#   - `_stub_gc` 单轮独立受 MAX_STUB_PER_RUN (3) 约束，不算入实际全文换出数。
#   - `--budget 0` (T3 cold-only) 下换出仍走 `_handle_rule_stub_sink` 的
#     “冷写成功→删本地内容”路径，计入 `lru_evicted`，因此仍 ≤ 本常量。
# 对应固定测试: tests/test_fix8_runtime.py::test_p5_budget_zero_evict_cap_scopes。
# FIX6 R2 未来时间戳容差 (2026-09-13): 判据锚点 ts > now + 此值视为时钟/
# 数据损坏, 计入 ts_anomaly 并 log.warning, 锚点夹到 now; 窗口内 (含容差)
# 按 now 处理。300s 为评审建议值; 生产时钟偏移分布未标定 (显式待标定)。
TS_ANOMALY_TOLERANCE_SECONDS = 300

# ---- 缺口2 (2026-08-28): audit 活性维度可沉判定 ----
# rule 型条目: weight < 阈值 且 last_active_at 距今 > 阈值天 且非 protected
# → 标 sink_candidate + sink_reason (仅体检可见性, 不改溢流执行逻辑)
AUDIT_SINK_WEIGHT_THRESHOLD = 1.5   # weight 低于此值视为"低权重"
AUDIT_SINK_INACTIVE_DAYS = 30       # last_active_at 距今超过此天数视为"久远失活"

# ---- SAFE-JUDGE v3 (2026-09-13, JUDGE-DESIGN.md §E-2/§E-3) -------------------
# 模糊带三级出口参数; 全部为纯规则判型/周治理异步终审参数, 同步判型路径零 LLM。
JUDGE_AMBIGUOUS_REVIEW_DAYS = 7      # ambiguous 首次复审时点 (+7d)
JUDGE_AMBIGUOUS_LRU_DAYS = 21        # A1-stub 兜底年龄门槛 (全文先冷层, 热层只留指针)
JUDGE_RESOLVED_RULE_GRACE_DAYS = 14  # LLM 终审判 rule 后的 14d 免 LRU 宽限
JUDGE_AMBIGUOUS_MAX_REVIEWS = 2      # 最多复审 2 次 → 到期只允许 stub 兜底

# ---- E8: env 开关惰性解析 (PEP 562, 2026-09-12) -----------------------------
# 原实现 import 时 os.environ.get 绑定一次 (与 LLM_API_KEY 同款冻结模式):
# 长驻进程 (MCP server) 运行中改 env 不生效。现改为每次属性访问重读 env。
# 消费方 (overflow/metadata) 通过各自模块级 __getattr__ 委托到本模块;
# monkeypatch.setattr(module, "NAME", v) 仍可覆盖 (测试契约不变)。

_ENV_SWITCH_SPECS = {
    "ACTIVITY_LOG_ENABLED": ("ACTIVITY_LOG_ENABLED", "1"),
    "RULE_BUDGET_ENABLED": ("MEMORYCORE_RULE_BUDGET_ENABLED", "1"),
    "HIT_WEAK_MODE": ("MEMORYCORE_HIT_WEAK_MODE", "degraded"),
    "EMBED_BACKEND": ("MEMORYCORE_EMBED_BACKEND", "mnemosyne"),
    # 2026-09-12 重设计回滚开关 (DESIGN.md §8):
    "CLASSIFIER_V2_ENABLED": ("MEMORYCORE_CLASSIFIER_V2_ENABLED", "1"),
    # SAFE-JUDGE v3 回滚开关 (JUDGE-DESIGN.md §E-3):
    #   JUDGE_V3_ENABLED=0        → 回 v2 (CLASSIFIER_V2_ENABLED 决定 v2/v1)
    #   JUDGE_AMBIGUOUS_HOLD=0    → ambiguous 直接当 rule, 退二值行为
    "JUDGE_V3_ENABLED": ("MEMORYCORE_JUDGE_V3_ENABLED", "1"),
    "JUDGE_AMBIGUOUS_HOLD": ("MEMORYCORE_JUDGE_AMBIGUOUS_HOLD", "1"),
    # PROTECT_SKIP_LRU=1: 旧一轮 protected 资格豁免 (已废弃, 仅 CACHE_POLICY_V2=0
    # 回滚对照); 默认 0 = protected ×WEIGHT_PROTECT_MULT 参与统一候选池。
    "PROTECT_SKIP_LRU": ("MEMORYCORE_PROTECT_SKIP_LRU", "0"),
    # 本轮热层缓存化的总回滚开关: =0 时恢复 PROTECT_SKIP_LRU/年龄门/ambiguous
    # 旧的换出资格语义 (旧候选池), 但冷写安全铁律不变。
    "CACHE_POLICY_V2": ("MEMORYCORE_CACHE_POLICY_V2", "1"),
    # 动作触发开关: =0 只关闭 on_turn_start/handle_tool_call 的
    # memorycore_action_recall 动作召回与合成 query, 常驻目录/常规召回保留。
    "ACTION_RECALL": ("MEMORYCORE_ACTION_RECALL", "1"),
    # 三通道降级: 默认 K,H,S 全开; 可设为 "K,H" 等子集回滚单通道行为。
    "FAULT_CHANNELS": ("MEMORYCORE_FAULT_CHANNELS", "K,H,S"),
}


def __getattr__(name):
    spec = _ENV_SWITCH_SPECS.get(name)
    if spec is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    env_name, default = spec
    val = os.environ.get(env_name, default)
    if name in ("ACTIVITY_LOG_ENABLED", "RULE_BUDGET_ENABLED",
                "CLASSIFIER_V2_ENABLED", "PROTECT_SKIP_LRU",
                "JUDGE_V3_ENABLED", "JUDGE_AMBIGUOUS_HOLD",
                "CACHE_POLICY_V2", "ACTION_RECALL"):
        result = val != "0"
        if name == "PROTECT_SKIP_LRU" and result:
            _warn_deprecated_protect_skip()
        return result
    return val


def _warn_deprecated_protect_skip() -> None:
    """PROTECT_SKIP_LRU=1 告警 (一个发布周期后删除该开关)。"""
    global _PROTECT_SKIP_WARNED
    if _PROTECT_SKIP_WARNED:
        return
    _PROTECT_SKIP_WARNED = True
    import warnings
    warnings.warn(
        "MEMORYCORE_PROTECT_SKIP_LRU=1 已废弃: protected 资格豁免不再是默认; "
        "请用 MEMORYCORE_CACHE_POLICY_V2=0 做完整旧行为回滚。",
        DeprecationWarning, stacklevel=2)


_PROTECT_SKIP_WARNED = False

# ---- Phase 4 活性信号 (语义两级判定, 实测校准 2026-08-26) ----
# 实测: 真实 780 对中 _topic_overlap 通过 0 对 (纯词法主信号全灭);
#       噪声主体 0.25-0.45, 真相关带 0.55-0.76, 用户两例 0.323/0.559 正确分类
HIT_STRONG_COS = 0.48               # 强命中阈值 (三重标定: S5 线上 0.48 / 无关 p90=0.487 / prefetch 0.4665)
HIT_WEAK_COS = 0.42                 # 灰区下界 (仅 HIT_WEAK_MODE=grey 生效)
HIT_STRONG_INCREMENT = 1.0          # 强命中加分 (设计值)
HIT_WEAK_INCREMENT = 0.3            # 弱命中加分 (设计值, 保守压噪声)
HIT_CAP_PER_SCAN = 1                # 每扫描轮每规则封顶 (饱和数学: 无封顶全员 1-2 天钉满 5.0)
# HIT_WEAK_MODE: E8 惰性解析 (模块尾部 __getattr__), degraded/off/grey
LEX_EVIDENCE_BIGRAMS = 2            # 词法弱命中: 共享 bigram ≥2 (用户 FP 例 sb=2)
FRESH_QUERY_SCAN_CAP = 50           # 每轮扫描 fresh 查询上限 (≈1 天增量, 嵌入成本封顶 ~2.5s)
EMBED_BATCH_MAX = 32                # 服务端批量上限 (32 条 ≈2s, 保 FastMCP 不阻塞)
EMBED_TIMEOUT = 30                  # 嵌入超时 (模型冷加载实测 15s + 余量)
# EMBED_BACKEND: E8 惰性解析 (模块尾部 __getattr__), mnemosyne/ollama/off
EMBED_MODEL = "qwen3-embedding-ctx256"  # 与 Mnemosyne recall 同模型 (分数空间一致)

# ---- 周整理 (smart_tidy, 2026-09 评审 P1 常量收敛 — 消除 weekly 本地双源漂移) ----
# 快车道/慢车道分级 (评审拍板 D3): 快车道 = 14d + 1 完成词 + LLM 确认,
# 仅周日 + 溢流后占用 >60% 时由 weekly_maintenance 执行 (本区常量);
# 慢车道 = 60d + 2 词 + 零行为词 (RULE_RETYPE_DONE_MARKERS, 日常 ≥60% 即跑)。
TIDY_ACTIVITY_EXEMPT_DAYS = 7    # 活性豁免窗 (与 RULE_MIN_RESIDENCY_DAYS / LRU 挤权词法窗同值)
TIDY_COMPLETE_AGE_DAYS = 14      # 历史条目日期距今 ≥ 此天数才考虑下沉 (较新锚点: min(内嵌日期, written_at))
TIDY_DONE_WORDS = [
    # 快车道完成态词表 (22 词)。比 RULE_RETYPE_DONE_MARKERS (60d 慢车道) 语义分层:
    # 本表更宽 (含"不再/放弃/卸载"等), 仅配 LLM 确认使用; 两表交集仅 5 词
    # (退役/已删/已修复/已停用/已退役), 收敛在一处便于维护。
    "退役", "已删", "已清理", "已解决", "已卸载", "已放弃", "已归档", "已停用",
    "已拆除", "已修复", "已废弃", "已移除", "已注销", "已退役", "放弃", "卸载",
    "purge", "清理干净", "不再", "已退出", "已下线", "已弃用",
]
TIDY_MAX_SINK_PER_RUN = 3        # 每 target 单轮下沉上限
TIDY_MAX_MERGE_PER_RUN = 1       # 每 target 单轮合并对数上限
TIDY_MERGE_RATIO = 0.62          # 合并候选相似度阈值
