"""memorycore-prefetch — MemoryProvider 插件: 冷层召回 + 写入镜像溢流

职责二:
1. prefetch (只读召回): 每轮召回 Mnemosyne 冷层 top-3 注入上下文。
2. on_memory_write 镜像 (2026-08-03 新增): 内置 memory 工具每次
   add/replace 后实时查占用, ≥80% 强制溢流 / ≥60% 且占用较上次
   溢流后再涨 ≥5% 触发溢流。把自动溢流从"只认 store_fact"扩到
   "认所有写入" (内置 memory 工具 / IM gateway / 其他会话)。
   溢流逻辑复用 MemoryCore core.overflow.run_overflow (治理不复制),
   后台线程执行不阻塞 memory 工具返回。
3. on_memory_write 直写通道治理 (Phase 2, 2026-08-16): add/replace
   提交后立即判型 — state 型直写后台迁移冷层 (查重→冷层写成功→删热层,
   失败保留+盖章兜底), rule 型 sidecar 盖章。治理核心复用 MemoryCore
   core.metadata.direct_write_govern, 与占用水位无关, 斩断直写污染。

实现 MemoryProvider ABC: is_available 恒 true, get_tool_schemas 返回空 (不注入工具),
prefetch(query) 调 Mnemosyne :8936 recall 候选 20 条, dense 排序后注入 top-5
(2026-08-11 单模型 qwen3-embedding-ctx256, 无 reranker)。

queue_prefetch(query) 已禁用 (2026-08-05): 后台预取结果缓存从未被消费,
每轮白做一遍重复 recall+rerank 徒增服务器负载。保留签名供 Hermes 每轮调用, no-op。

激活: config.yaml memory.provider: memorycore-prefetch
"""
import hashlib
import json
import logging
import math
import os
import queue
import re
import statistics
import sys
import tempfile
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from agent.memory_provider import (
    MemoryProvider,
    TRIVIAL_PROMPT_RE,  # noqa: F401  设计复用常量: Hermes 低语义闸门单一真相源
    is_trivial_prompt,   # F1: 入口只读复用, 不复制正则
)

logger = logging.getLogger(__name__)

# Use the open-source memorycore package (pip-installed origin-memorycore).
# Fallback: repo root two levels up (development mode, not pip-installed).
try:
    from memorycore.cold_store_client import ColdStoreClient  # noqa: E402
    from memorycore.local_store import LocalStore, normalize_for_compare  # noqa: E402
    from memorycore.core.config import (  # noqa: E402
        SOFT_THRESHOLD,
        HARD_THRESHOLD,
        CHAR_LIMIT_MEMORY,
        CHAR_LIMIT_USER,
        INDEX_BUDGET_CHARS,
        STUB_MAX_CHARS,
        HIT_WEAK_COS,
    )
    from memorycore.core import config as _mc_config  # noqa: E402
    from memorycore.core.overflow import (  # noqa: E402
        run_overflow, restore_stubs_from_results, _lex_evidence, _stub_topic,
        _NOISE_PREFIXES,
    )
    from memorycore.core.metadata import (  # noqa: E402
        direct_write_govern, log_activity_query, MetaStore,
    )
    from memorycore.core.decay import _apply_decay  # noqa: E402
    from memorycore.core import recall_fusion as _recall_fusion  # noqa: E402  # P2 融合内核(与治理层同一份实现)
    from memorycore.core.recall_probe import (  # noqa: E402  # P1 只读观测探针
        query_sha256 as _probe_query_sha256, record_recall_probe)
except ImportError:
    _REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    sys.path.insert(0, _REPO_ROOT)
    from memorycore.cold_store_client import ColdStoreClient  # noqa: E402
    from memorycore.local_store import LocalStore, normalize_for_compare  # noqa: E402
    from memorycore.core.config import (  # noqa: E402
        SOFT_THRESHOLD,
        HARD_THRESHOLD,
        CHAR_LIMIT_MEMORY,
        CHAR_LIMIT_USER,
        INDEX_BUDGET_CHARS,
        STUB_MAX_CHARS,
        HIT_WEAK_COS,
    )
    from memorycore.core import config as _mc_config  # noqa: E402
    from memorycore.core.overflow import (  # noqa: E402
        run_overflow, restore_stubs_from_results, _lex_evidence, _stub_topic,
        _NOISE_PREFIXES,
    )
    from memorycore.core.metadata import (  # noqa: E402
        direct_write_govern, log_activity_query, MetaStore,
    )
    from memorycore.core.decay import _apply_decay  # noqa: E402
    from memorycore.core import recall_fusion as _recall_fusion  # noqa: E402  # P2 融合内核(与治理层同一份实现)
    from memorycore.core.recall_probe import (  # noqa: E402  # P1 只读观测探针
        query_sha256 as _probe_query_sha256, record_recall_probe)

_RECALL_CANDIDATES = 20    # 第一阶段召回候选数 (单模型 dense, 2026-08-11 qwen3 切换后沿用 20)
_INJECT_TOP_N = 5          # 缺页注入上限 (沿用现有常量)
_PREFETCH_TIMEOUT = 5.0    # prefetch 专用超时, 比默认 10s 更短, 不阻塞对话
_QUERY_MAX_LEN = 1000      # 召回 query 超长截断
_INDEX_LINE_MAX = 8  # 单句柄行主题词上限 (handle 9 字符 + 括号/空格 = 20 整行)
_ACTION_RECALL_TOOL = "memorycore_action_recall"
# F4 (2026-09-13): 动作触发词收敛 —
#   强词: 仅这些词可在"最近用户意图"上触发动作召回;
#   弱词: 写/回复/发送 不再单独触发 (防闲聊/确认误触发, 评审 §5.4)。
_ACTION_WORDS = (
    "交付", "发布", "上线", "提交", "通知", "汇报",
    "删除", "清理", "卸载", "停止", "重启", "禁用",
    "修改", "配置", "部署", "迁移", "更新",
)
_ACTION_WEAK_WORDS = ("写", "回复", "发送")
# F4: 动作触发要求"动词+对象" — 泛动词必须搭配 MemoryCore 动作对象;
# 如何式提问不视为动作意图 (真实执行阶段由 tool_count>0 兜底)。
_ACTION_OBJECT_REQUIRED = ("修改", "配置", "安装", "清理", "更新",
                           "迁移", "停止", "禁用")
_ACTION_OBJECTS = (
    "文件", "目录", "配置", "系统", "服务器", "服务", "机器", "版本",
    "依赖", "规则", "记忆", "任务", "端口", "进程", "插件", "环境",
    "数据", "账号", "设置", "参数", "策略", "流程", "部署", "发布",
    "交付", "软件", "工具", "仓库", "镜像", "模型", "脚本", "定时任务",
)
_ACTION_QUESTION_RE = re.compile(
    r"怎么|如何|怎样|为什么|是什么|是否|吗[？?]?$|呢[？?]?$")
_ACTION_RECALL_CUES = "用户要求 行为规则 偏好"

# F1 (2026-09-13, FIX4 P0): 查询侧低信息/系统噪声闸门。
# 取舍原则 (FIX4 §1.4, 写入代码备查):
#   噪声注入的代价 = 少量上下文占用 (低, 可以通过排序/截断控制);
#   漏召回的代价   = 用户规则不生效 (高, 可能造成行为违规)。
#   => 存疑时选择注入; 闸门只在"近乎确定的噪声"上生效。
# 结构约束 (FIX4整改):
#   1. 确认/低信息类一律 fullmatch (尾随标点允许), 不做裸子串/前缀阻断;
#   2. 主题/内容词 (天气/周末/拍照/头像/游戏机/全家桶/python/excel/csv/电影/诗/...)
#      绝不单独作为拦截依据, 只能作为"降权/结构性低信息"的辅助信号;
#   3. 只有短查询 (<=_LOW_INFO_MAX_CHARS) 且无动作意图/规范词/对象词时,
#      才允许按"闲聊结构"或"外部工具问答结构"判低信息; 长句一律正常召回;
#   4. 系统噪声前缀 (_NOISE_PREFIXES/_SYSTEM_NOISE_RE) 保留; 但前缀后的
#      剩余正文若含规则/对象语义则让位召回 (前缀本身不是拦截终点)。
_NOISE_PUNCT_CLASS = r"[\s，。,.!！?？~～、;；:：…]"
_ACK_TOKEN = (
    r"(?:好的?|收到|明白(?:了)?|知道(?:了)?|了解(?:了)?|懂了|"
    r"嗯+|哦+|噢+|呃+|额+|行|可以|没事|算了|辛苦了|"
    r"谢谢(?:你|您)?|多谢|感谢|继续|在吗|在么|在不在|"
    r"先这样(?:吧)?|没问题|好吧|好了|是|对|对的|是的|没错|"
    r"ok|okay|系统)"
)
# 整条就是一个或多个确认/低信息 token (允许中间/尾随标点)。
_LOW_INFO_ACK_RE = re.compile(
    rf"^{_ACK_TOKEN}(?:{_NOISE_PUNCT_CLASS}*{_ACK_TOKEN})*"
    rf"{_NOISE_PUNCT_CLASS}*$",
    re.IGNORECASE,
)
# 确认 + 指代/短跟从 (fullmatch; 不要求整条只是一个 token)。
# 保留旧名供兼容: 旧实现是前缀 match, FIX4 起改为 strict fullmatch。
_LOW_INFO_ACK_PREFIX_RE = re.compile(
    rf"^{_ACK_TOKEN}{_NOISE_PUNCT_CLASS}*"
    rf"(?:(?:那就|就按))?{_NOISE_PUNCT_CLASS}*"
    rf"(?:按你说的?|听你的)?{_NOISE_PUNCT_CLASS}*"
    rf"(?:写吧|做吧|行|好的?|收到|明白|可以|继续|ok|好|是|对|嗯+)?"
    rf"{_NOISE_PUNCT_CLASS}*$",
    re.IGNORECASE,
)

# 主题/内容词: 只用于"短句 + 闲聊/问答结构"的辅助识别, 永不单独拦截。
_CHITCHAT_TOPIC_RE = re.compile(
    r"天气|吃什么|吃啥|晚饭|午饭|早饭|宵夜|夜宵|周末|放假|旅游|"
    r"爬山|拍照|头像|诗|电影|奶茶|咖啡|健身|散步|逛街|游戏机|"
    r"全家桶|switch|壁纸|主播|回复|音乐|小说|游戏|星座|综艺",
    re.IGNORECASE,
)
# 外部工具/通识名: 仅当查询同时是短问答结构且无 MemoryCore 操作语义时,
# 才作为低信息候选; 不是全量 how-to 拦截, 目录仍常驻可见。
_GENERIC_OFFTOPIC_RE = re.compile(
    r"(?<![a-z])(?:python|gil|excel|csv|ppt|pptx)(?![a-z])",
    re.IGNORECASE,
)
_SYSTEM_NOISE_RE = re.compile(
    r"^\s*[\[\(【（]\s*(?:IMPORTANT|ASYNC|SYSTEM|BACKGROUND|SUBAGENT|"
    r"TOOL|NOTIFICATION|WARNING|CRITICAL|ERROR|DELEGATION|OUT-OF-BAND)\b",
    re.IGNORECASE,
)
# "短查询"上限: 仅用于结构性闲聊识别; 长句绝不走低信息快路径。
_LOW_INFO_MAX_CHARS = 18
# MemoryCore 操作语义 (规则/对象/规范) — 出现即让位正常召回。
_OPERATIONAL_RULE_CUES = (
    "必须", "不得", "禁止", "务必", "需要", "要求", "规则", "红线",
    "准则", "规范", "流程", "配置", "通知", "确认", "回滚", "审批",
    "发布", "上线", "部署", "交付", "提交", "任务", "服务", "脚本",
    "数据", "接口", "模板", "看板", "策略", "参数", "版本", "系统",
    "机器", "用户", "团队", "值班", "报备", "冻结", "失败", "执行",
    "变更", "改动", "维护", "停机", "附带", "检查", "记忆", "插件",
)
_OPERATIONAL_RE = re.compile(
    "|".join(re.escape(x) for x in _OPERATIONAL_RULE_CUES)
    + "|" + "|".join(re.escape(x) for x in _ACTION_OBJECTS)
)
_SMALLTALK_STRUCTURE_RE = re.compile(
    r"我|你|咱|我们|帮|打算|准备|想|喜欢|不错|挺好|很好|"
    r"好呢|怎么样|吗|呀|哈哈|呵呵|辛苦|顺便|真|太|"
    r"今天|明天|晚上|早上|下午|一下",
    re.IGNORECASE,
)
_EXTERNAL_QUESTION_RE = re.compile(
    r"什么|啥|怎么|如何|为什么|哪个|是否|吗|呢|求助|请教",
    re.IGNORECASE,
)

# --- on_memory_write 自动溢流 (2026-08-03) ---
_OVERFLOW_RETRY_DELTA = 5  # 软阈值触发需比上次溢流后占用再涨 >= 5% (防每写一次空跑)

# --- 动态基线记录 (2026-08-11 单模型: 仅记录不消费, 保留供将来校准) ---
_BASELINE_INIT = 0.70        # 初始基线 (bge 标定, qwen3 分数偏低仅供参考)
_BASELINE_WINDOW = 200       # 滚动样本窗口
_BASELINE_RECALC_EVERY = 50  # 每新增 N 个样本重算一次中位数
# 基线持久化文件
_BASELINE_FILE = os.path.join(os.path.dirname(__file__), "baseline.json")


class MemoryCorePrefetchProvider(MemoryProvider):
    name = "memorycore-prefetch"

    def __init__(self):
        super().__init__()
        # 动态基线状态 (2026-08-05 去掉水位分档, 不再追踪 water_level/tokens)
        self._baseline_lock = threading.Lock()
        self._baseline_samples: List[float] = []
        self._baseline_value: float = _BASELINE_INIT
        self._sample_count_since_recalc: int = 0
        self._load_baseline()
        # 注入去重状态 (2026-08-05): 会话内已注入 id 集合 + 热层全文
        self._injected_ids: set = set()
        self._hot_text: str = ""
        # CACHE-POLICY-V2 缺页路径状态 (全本地, 零冷层 RPC):
        self._directory_cache: List[Dict[str, Any]] = []
        self._last_user_intent: str = ""
        self._last_assistant_action: str = ""
        self._pending_action_query: str = ""
        self._last_action_context: str = ""
        self._action_recall_count: int = 0
        self._action_recall_lag: Optional[float] = None
        # 缺口1 (2026-08-28): 热层归一化文本 — 标点变体内容不再重复注入
        self._hot_norm: str = ""
        # on_memory_write 自动溢流状态
        self._overflow_lock = threading.Lock()
        self._overflow_thread: Optional[threading.Thread] = None
        # target -> 上次溢流后的占用 pct; 软阈值触发需比它再涨 >= _OVERFLOW_RETRY_DELTA
        self._last_overflow_pct: Dict[str, int] = {}
        # Phase 2 (2026-08-16): 直写通道治理状态 — F5 单飞闸 (终审修复):
        # 单工作线程 + 有界队列, 突发写入不扇出 N 个并发冷层 RPC;
        # 队列满则跳过 (下次溢流 reconcile 兜底补盖)。
        self._govern_lock = threading.Lock()
        self._govern_worker: Optional[threading.Thread] = None
        self._govern_queue: "queue.Queue" = queue.Queue(maxsize=128)

    def is_available(self) -> bool:
        return True

    def initialize(self, session_id: str, **kwargs) -> None:
        """会话初始化: 重置注入去重状态, 加载热层全文 (供去重)。"""
        self._injected_ids = set()
        self._hot_text = self._load_hot_layer_text()
        self._hot_norm = normalize_for_compare(self._hot_text)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        """向 agent 注册只读动作召回工具 (MEMORYCORE_ACTION_RECALL=0 时为空)。"""
        if not getattr(_mc_config, "ACTION_RECALL", True):
            return []
        return [{
            "name": _ACTION_RECALL_TOOL,
            "description": (
                "在准备执行交付/发布/通知/删除/重启/改配置等动作前调用，"
                "只读召回相关冷层规则并写回热层；不写冷层、不调用 LLM。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "intent": {
                        "type": "string",
                        "description": "即将执行的动作/意图原文 (可省略, 默认最近用户意图)",
                    }
                },
                "required": [],
            },
        }]

    # -- system_prompt_block 常驻页表/规则目录 (2026-08-04, V2 2026-09-13) ---------

    def system_prompt_block(self) -> str:
        """静态引导 + 动态常驻目录 (≤INDEX_BUDGET_CHARS=800, 零 RPC 零 LLM)。

        目录来源是本地 sidecar 的 stub(cold_id) 句柄, 每轮重新读取本地文件生成;
        不访问冷层, 不调用 LLM, 不参与热层内容预算。冷层不可达时目录仍可用。
        """
        lines = [
            "## MemoryCore 规则/偏好记忆",
            "热层只保留高频条目; 被换出的规则仍有 ≤40 字指针与可直查句柄。",
            "需要历史细节时用 memorycore_recall(query); 句柄可作 handle 参数直查。",
        ]
        if getattr(_mc_config, "ACTION_RECALL", True):
            lines.append(
                "在准备执行交付/发布/上线/通知/删除/重启/改配置等动作前, "
                "调用 memorycore_action_recall(intent) 只读召回相关规则。")
        directory = self._format_directory()
        if directory:
            lines.append(directory)
        return "\n".join(lines)

    def _load_directory(self) -> List[Dict[str, Any]]:
        """从本地 sidecar 读取 stub 句柄目录 (只读本地, 零冷层 RPC)。"""
        records: List[Dict[str, Any]] = []
        try:
            store = LocalStore()
            for target in ("memory", "user"):
                ms = MetaStore(target, memory_path=store.memory_path,
                               user_path=store.user_path)
                for e in store.entries(target):
                    m = ms.get_entry(e) or {}
                    if m.get("type") != "stub" or not m.get("cold_id"):
                        continue
                    topic = _stub_topic(e) or re.sub(r"\s+", "", e)[:8]
                    handle = str(m.get("handle") or ("#" + hashlib.sha256(
                        str(m.get("cold_id")).encode("utf-8")).hexdigest()[:8]))
                    last = (m.get("last_active_at") or m.get("updated_at")
                            or m.get("written_at") or "")
                    records.append({
                        "handle": handle[:9],
                        "topic": topic[:_INDEX_LINE_MAX],
                        "cold_id": m.get("cold_id"),
                        "last_active_at": str(last),
                        "stub": e,
                    })
            records.sort(key=lambda r: str(r.get("last_active_at") or ""),
                         reverse=True)
        except Exception as e:
            logger.debug("directory load failed: %s", e)
        self._directory_cache = records
        return records

    def _format_directory(self, records: Optional[List[Dict[str, Any]]] = None) -> str:
        """生成 ≤INDEX_BUDGET_CHARS 的页表文本; 行按 last_active_at 降序保留。"""
        if records is None:
            records = self._load_directory()
        if not records:
            return ""
        header = "## 常驻规则目录 (句柄直查)"
        lines = []
        used = len(header) + 1
        for r in records:
            topic = str(r.get("topic") or "")[:_INDEX_LINE_MAX]
            handle = str(r.get("handle") or "")[:9]
            if not handle.startswith("#"):
                handle = "#" + handle.lstrip("#")[:8]
            line = f"[{handle}] {topic}"
            if len(line) > 20:
                line = line[:20]
            if used + len(line) + 1 > INDEX_BUDGET_CHARS:
                break
            lines.append(line)
            used += len(line) + 1
        if not lines:
            return ""
        return header + "\n" + "\n".join(lines)

    @staticmethod
    def _compact_query_for_gate(query: str) -> str:
        """去掉空白/标点后用于长度判断; 不做主题词裁剪。"""
        return re.sub(r"[\s，。,.!！?？~～、;；:：…]+", "", query or "")

    @staticmethod
    def _system_noise_tail(query: str) -> Optional[str]:
        """剥离首段 [IMPORTANT...]/[ASYNC...] 等方括号前缀, 返回剩余正文。"""
        q = (query or "").strip()
        m = re.match(r"^[\[\(【（][^\]\)】）]*[\]\)】）]", q)
        if m:
            return q[m.end():].strip()
        return q

    @classmethod
    def _has_operational_signal(cls, query: str) -> bool:
        """动作意图 / 规则规范词 / MemoryCore 对象词任一命中 → 正常召回。"""
        if not query:
            return False
        try:
            # action_trigger_hit 已排除"如何式提问"与无对象的泛动词,
            # 因此 "怎么安装 Python 包" 不会被误判为动作意图。
            if cls._action_trigger_hit(query):
                return True
        except Exception:
            pass
        return bool(_OPERATIONAL_RE.search(query))

    @staticmethod
    def _is_low_information_query(query: str) -> bool:
        """F1 查询侧闸门 (零 LLM, 只读本地字符串, FIX4 P0 收窄版)。

        只有以下近乎确定的噪声才返回 True:
          1. Hermes is_trivial_prompt (英文 ack / 空 / slash 命令);
          2. 整条就是一个或多个确认/低信息 token (fullmatch, 尾随标点允许);
          3. 系统噪声前缀: 剥掉前缀后的正文为空/无操作语义; 正文含规则语义
             则放行 (FIX4 A 组 #31: `[IMPORTANT: 运维] 对外发布必须通知用户`);
          4. 短句 (<=18 字) 且无操作语义, 同时命中"闲聊结构"或
             "外部工具短问答结构"。主题/内容词只是结构助手, 绝不单独拦截:
             裸 `游戏机`/`Python`/`天气`/`周末` 一律正常召回。
        取舍: 存疑时选择注入 — 漏召回的代价 (规则不生效) 远高于
        噪声注入的代价 (上下文占用)。因此长句、含对象/规范词、含动作
        意图、显式句柄/H 共识查询都不进此闸门。
        """
        q = (query or "").strip()
        if not q:
            return True
        if is_trivial_prompt(q):
            return True
        # (2) 严格整条确认/低信息 fullmatch。
        if _LOW_INFO_ACK_RE.match(q) or _LOW_INFO_ACK_PREFIX_RE.match(q):
            return True
        # (3) 系统噪声前缀: 保留前缀识别, 但正文有操作语义则让位。
        if q.startswith(_NOISE_PREFIXES) or _SYSTEM_NOISE_RE.match(q):
            tail = MemoryCorePrefetchProvider._system_noise_tail(q)
            if tail and MemoryCorePrefetchProvider._has_operational_signal(tail):
                return False
            return True
        # (4) 仅短句允许结构式闲聊/外部问答; 长句一律正常召回。
        compact = MemoryCorePrefetchProvider._compact_query_for_gate(q)
        if len(compact) > _LOW_INFO_MAX_CHARS:
            return False
        if MemoryCorePrefetchProvider._has_operational_signal(q):
            return False
        if (_CHITCHAT_TOPIC_RE.search(compact)
                and _SMALLTALK_STRUCTURE_RE.search(compact)):
            return True
        if (_GENERIC_OFFTOPIC_RE.search(compact)
                and _EXTERNAL_QUESTION_RE.search(compact)):
            return True
        return False

    @staticmethod
    def _action_trigger_hit(text: str) -> bool:
        """强动作词字符串匹配 (零 LLM); 只允许在最近用户意图上调用。

        F4: 写/回复/发送 已降为弱词 (见 _ACTION_WEAK_WORDS), 不再单独触发;
        assistant 文本不得作为入参 (调用方契约)。
        泛动词 (修改/配置/安装/清理/更新/迁移/停止/禁用) 必须搭配
        动作对象; 如何式提问不算动作意图 (真实执行由 tool_count>0 兜底)。
        """
        t = text or ""
        if not t:
            return False
        if _ACTION_QUESTION_RE.search(t):
            return False
        for w in _ACTION_WORDS:
            if w not in t:
                continue
            if w in _ACTION_OBJECT_REQUIRED and not any(
                    o in t for o in _ACTION_OBJECTS):
                continue
            return True
        return False

    # -- on_memory_write 镜像溢流 (2026-08-03) --------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """内置 memory 工具写入后镜像回调: 实时查占用, 超阈值自动溢流。

        补上 B 方案失去的镜像收口 — 自动溢流从"只认 store_fact"扩到
        "认所有写入" (内置 memory 工具 / IM gateway / 其他会话)。
        remove 只会降低占用, 不触发; add/replace 后检查。
        溢流放后台线程执行, 不阻塞 memory 工具返回。

        Hermes 调用点: tool_executor.py -> MemoryManager.notify_memory_tool_write
        (agent_runtime_helpers.py:2805), 内置 memory 工具每次写后自动调。
        """
        if action == "remove":
            return  # remove 只降占用; sidecar 孤儿键由下次溢流 reconcile GC
        if target not in ("memory", "user"):
            return
        try:
            # Phase 2 (2026-08-16): 直写通道治理 — add/replace 提交后立即判型,
            # state 型后台迁移冷层 (与占用水位无关), rule 型盖章。
            # 治理逻辑在 core.metadata.direct_write_govern (不复制)。
            if action in ("add", "replace"):
                self._govern_direct_write(target, content, action)
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

    def _govern_direct_write(self, target: str, content: str, action: str) -> None:
        """直写通道治理 (Phase 2, 2026-08-16): 后台线程执行, 不阻塞 memory 工具返回。

        条目已被 Hermes 写入热层, 治理只做判型后的迁移/盖章:
          state 型 → 查重→冷层写成功→删热层; 冷层失败 → 保留热层+盖章兜底
          rule 型 → sidecar 盖章 {rule, written_at=now, origin=hermes}
        """
        if not content or not content.strip():
            return
        with self._govern_lock:
            if self._govern_worker is None or not self._govern_worker.is_alive():
                self._govern_worker = threading.Thread(
                    target=self._govern_worker_loop,
                    daemon=True,
                )
                self._govern_worker.start()
        try:
            self._govern_queue.put_nowait((target, content, action))
        except queue.Full:
            logger.debug("govern queue full, skip (reconcile backstop)")

    def _govern_worker_loop(self) -> None:
        """治理单工作线程 (F5): 串行消费队列, 任意时刻至多 1 个冷层 RPC 在途。"""
        while True:
            item = self._govern_queue.get()
            try:
                self._run_govern_bg(*item)
            finally:
                self._govern_queue.task_done()

    def _run_govern_bg(self, target: str, content: str, action: str) -> None:
        """后台治理执行: 复用 MemoryCore direct_write_govern (治理不复制)。"""
        try:
            store = LocalStore()
            client = ColdStoreClient()  # 默认 10s 超时, 查重要用
            result = direct_write_govern(store, client, target, content, action)
            logger.info("direct-write govern target=%s action=%s result=%s",
                        target, action, result)
        except Exception as e:
            logger.debug("direct-write govern failed: %s", e)

    def _spawn_overflow(self, target: str) -> None:
        """后台线程执行溢流 (同一时间只跑一个)。"""
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
        """后台溢流: 复用 MemoryCore run_overflow (治理逻辑不复制)。

        冷层不可达时 run_overflow 内部 errors 累计并保留本地, 不丢数据。
        完成后记录溢流后占用, 供下次触发判断 (防空跑)。
        """
        try:
            store = LocalStore()
            client = ColdStoreClient()  # 默认 10s 超时, 查重要用
            stat = run_overflow(store, client, target)
            pct_after = int(str(stat.get("usage_after", "0%")).rstrip("%") or 0)
            with self._overflow_lock:
                self._last_overflow_pct[target] = pct_after
            logger.info("overflow auto-done target=%s %s", target, stat)
        except Exception as e:
            logger.debug("overflow bg failed: %s", e)

    # -- prefetch / queue_prefetch (2026-08-05 恢复双通道) -------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """每轮: 常驻目录 + 缺页三通道召回 (动作时刻用合成 query)。

        - 目录/句柄来自本地 sidecar, 保证存在性不依赖 query;
        - 普通 query 走 S/K/H 三通道共识;
        - on_turn_start 产生的动作 query / 工具返回值优先 (若同轮可用)。
        """
        try:
            directory = self._format_directory()
            q = (query or "").strip() or self._last_user_intent
            if self._pending_action_query and not q:
                q = self._pending_action_query
            context = self._recall_sync(q) if q else ""
            parts = [directory] if directory else []
            if context:
                parts.append(context)
            return "\n".join(parts)
        except Exception as e:
            logger.debug("prefetch failed: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """本地目录刷新 (零 RPC); 不缓存后台 recall。"""
        try:
            self._load_directory()
        except Exception as e:
            logger.debug("queue_prefetch directory refresh failed: %s", e)
        return

    # -- 动作触发 / tool-loop hooks (CACHE-POLICY-V2) ---------------------

    def on_turn_start(self, turn_number: int, message: str, **kwargs) -> None:
        """tool-loop 动作检测 (纯字符串, 零 LLM)。

        动作触发只看最近用户意图: `tool_count>0` 或用户意图命中强动作词。
        F4: assistant/当前模型步文本不得触发 (原实现把 message 当动作词
        来源, 纯 assistant 文本也唤醒动作召回并放大写回)。
        合成 `最近用户意图 + 动作提示词` 作为下一次动作 recall 的 query;
        无新用户消息时用上一轮真实用户意图, 解决动作时刻触发错位。
        MEMORYCORE_ACTION_RECALL=0 时只关此路径, 目录/常规召回保留。
        """
        if not getattr(_mc_config, "ACTION_RECALL", True):
            return
        try:
            tool_count = int(kwargs.get("tool_count") or 0)
            intent = (self._last_user_intent or "").strip()
            # F4: message/assistant 文本不参与触发; 只看 tool_count 或
            # 最近用户意图中的强动作词。
            is_action = tool_count > 0 or (
                bool(intent) and self._action_trigger_hit(intent))
            if not is_action:
                return
            self._pending_action_query = (
                f"{intent} {_ACTION_RECALL_CUES}").strip()
            # 即时生成只读动作召回上下文 (目录已在 system_prompt_block 常驻);
            # 失败静默, 最坏退回 agent 主动调 memorycore_action_recall。
            ctx = self._recall_sync(self._pending_action_query,
                                    action_trigger=True)
            self._last_action_context = ctx or ""
            self._action_recall_count += 1
            self._action_recall_lag = 0.0
        except Exception as e:
            logger.debug("on_turn_start action recall failed: %s", e)

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any],
                         **kwargs) -> str:
        """处理 agent 主动动作召回 (只读冷层/本地; 不写冷层)。"""
        if tool_name != _ACTION_RECALL_TOOL or not getattr(
                _mc_config, "ACTION_RECALL", True):
            raise NotImplementedError(
                f"Provider {self.name} does not handle tool {tool_name}")
        intent = str((args or {}).get("intent") or "").strip()
        # F4: 工具兜底只用最近用户意图; assistant 文本不得触发动作召回。
        q = intent or self._last_user_intent
        try:
            context = self._recall_sync(q, action_trigger=True)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)
        return json.dumps({
            "tool": _ACTION_RECALL_TOOL,
            "intent": q,
            "context": context,
            "read_only_cold": True,
        }, ensure_ascii=False)

    # -- 内部: 召回 / reranker / 动态基线 ----------------------------------

    def _recall_sync(self, query: str, *, action_trigger: bool = False) -> str:
        """缺页路径: K=20 候选 + 0.42 下界 + S/K/H 三通道共识 (只读冷层)。

        - S(语义): dense_score ≥ HIT_WEAK_COS=0.42;
        - K(关键词): recall keyword/fts_score>0 或本地 _lex_evidence(sb≥2);
        - H(句柄): 目录 topic/handle 与 query/动作匹配 → 仅用该 stub 的
          cold_id 路径取回全文; 单 S 不写回, 写回至少需要 H 或 K 共识
          (F4: action_trigger 不再作为写回共识; 纯 S+action 只注入不写回);
        - 动作触发: on_turn_start/工具合成的 query 只作为 action_trigger 标记,
          不替代 H/K 写回共识。
        保持只读: bump=False; 不 remember/update/forget。
        F1: 低信息/噪声 query 命中闸门 → 直接返回空 (目录由 prefetch/
        system_prompt_block 常驻提供, 不发起全文 recall)。
        """
        raw = (query or "").strip()
        # FIX4 P0: 动作意图/H 句柄共识必须在 F1 闸门之前让位。action_trigger
        # 来自 on_turn_start/tool_count 的真实执行路径, 不得被低信息门短路;
        # 本地目录 H 共识 (显式句柄或足够长的主题匹配) 同样先放行。
        action_hit = bool(action_trigger) or bool(
            raw and self._action_trigger_hit(raw))
        if (raw and not action_hit
                and self._is_low_information_query(raw)
                and not self._local_handle_consensus(raw)):
            return ""  # F1: 近乎确定的噪声只回目录, 不发起全文 recall
        q = self._preprocess_query(raw)
        if not q and not raw:
            # 仅当调用方显式传空 query 时才回退最近用户意图;
            # 低信息 query 已在上面被拦下, 不得借回退绕过闸门。
            fallback = self._last_user_intent
            if fallback and not self._is_low_information_query(fallback):
                q = self._preprocess_query(fallback)
        if not q and action_trigger and self._pending_action_query:
            pending = self._pending_action_query
            if not self._is_low_information_query(pending):
                q = self._preprocess_query(pending)
        if not q:
            return ""
        log_activity_query(q)  # Phase 3 S4 采集 (失败静默)
        try:
            records = self._load_directory()
            handle_records = [r for r in records
                              if self._handle_match(q, r)]
            handle_ids = {r.get("cold_id") for r in handle_records
                          if r.get("cold_id")}
            client = ColdStoreClient(timeout=_PREFETCH_TIMEOUT)
            # 2026-09-18: 融合默认开, 与治理层共用 core/recall_fusion 内核
            # (此前插件自己拼「引擎序 + 纯 decay」, 治理层的排序改进到不了
            #  每轮注入这条真实流量路径)。池大小: 融合开 → 预注册
            # candidate_k(默认 30); 融合关 → 沿用旧口径 20。
            # 滚动基线的样本口径**不变**: 仍取引擎序前 _RECALL_CANDIDATES 条。
            _fuse_on = _recall_fusion.fusion_enabled()
            _pool_k = (_recall_fusion.candidate_k() if _fuse_on
                       else _RECALL_CANDIDATES)
            results = client.recall_results(q, top_k=_pool_k, bump=False)
            # FIX-P2 (REVIEW-6 L2): 单条候选的异常 dense_score (超大整数/
            # 非有限) 按 0 兜底后再进入 decay/选择链; 有限分数行原样通过,
            # 保证正常输入下注入集合/顺序/文本逐字节不变。
            results = self._normalize_abnormal_dense(results)
            self._record_baseline(results[:_RECALL_CANDIDATES])
            if _fuse_on:
                results = _recall_fusion.fuse_candidates(results, q,
                                                         len(results))
            else:
                results = _apply_decay(results)
            # 句柄直查: 对匹配 stub 主题追加只读 recall, 取回冷层真实 id 结果。
            if handle_records:
                seen = {r.get("id") for r in results}
                for rec in handle_records[:2]:
                    topic = rec.get("topic") or ""
                    if not topic:
                        continue
                    try:
                        hr = client.recall_results(topic, top_k=5, bump=False)
                    except Exception:
                        continue
                    for r in hr:
                        if r.get("id") in handle_ids and r.get("id") not in seen:
                            results.append(r)
                            seen.add(r.get("id"))
            selected = self._select_fault_candidates(
                q, results, handle_ids, action_trigger=action_trigger)
            selected = self._dedupe_injected(selected)
            selected = self._dedupe_hot_layer(selected)
            # Phase 4: 仅 K/H/action 共识命中的 stub 写回全文到热层 (只改本地);
            # 单独 S 命中保留在注入结果, 但绝不写回, 符合"单语义分不写回"口径。
            writeback = [r for r in selected if r.get("_consensus")]
            if writeback:
                try:
                    _store = LocalStore()
                    remaining_wb = restore_stubs_from_results(
                        _store,
                        {"memory": MetaStore(
                            "memory", memory_path=_store.memory_path,
                            user_path=_store.user_path),
                         "user": MetaStore(
                             "user", memory_path=_store.memory_path,
                             user_path=_store.user_path)},
                        writeback)
                    wb_ids = {r.get("id") for r in writeback}
                    restored_ids = wb_ids - {r.get("id") for r in remaining_wb}
                    if restored_ids:
                        selected = [r for r in selected
                                    if r.get("id") not in restored_ids]
                except Exception:
                    pass  # 恢复失败不影响注入 (下轮重试)
            self._mark_injected_audit(selected)
            context = self._format_results(selected)
            # P1: 注入决策完成后只读观测; 任何异常不得改变 prefetch 返回值。
            # F1/F3: 必须把本次 H 句柄 id 集合带到事件构造处, channel 按
            # 与 _recall_channel_of 相同口径逐候选计算; k_source 服从 channel。
            try:
                self._emit_prefetch_probe(q, results, selected, handle_ids)
            except Exception:
                pass
            return context
        except Exception as e:
            logger.debug("memorycore-prefetch sync recall failed: %s", e)
            return ""

    # -- P1: 探针接入 (只读观测, 绝不参与判断/排序/写回) -------------------

    @staticmethod
    def _probe_id_text(value: Any) -> str:
        """探针 id 文本化: None -> ""; 异常 -> "" (不抛)。"""
        if value is None:
            return ""
        try:
            return str(value)[:4096]
        except Exception:
            return ""

    @staticmethod
    def _finite_score(value: Any) -> Optional[float]:
        """L2 分数有限化探测: 类型异常/不可转 float/非有限值 → None。

        与 ``_probe_num`` 共用同一转换 (P0 冻结口径: 非有限值一律归 0),
        供选择链判断"该分数能否作为证据"以及选择前的兜底归一化使用。
        """
        try:
            num = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return num if math.isfinite(num) else None

    @staticmethod
    def _probe_num(value: Any) -> float:
        """探针数值降级: 缺失/类型异常/非有限值一律 0.0 (不抛)。"""
        num = MemoryCorePrefetchProvider._finite_score(value)
        return num if num is not None else 0.0

    def _probe_k_source_of(self, query: str, result: Any,
                           channel: Optional[str] = None) -> str:
        """K 证据来源 (仅观测): cold keyword/fts 优先, 其次本地 bigram。

        F1: 该行 channel 已判 H 时一律返回 "", 保证 channel/k_source 自洽;
        K/S 行沿用 P1 原有证据口径。
        """
        try:
            if channel == "H":
                return ""
            if not isinstance(result, dict):
                return ""
            kw = self._probe_num(result.get("keyword_score")) > 0
            fts = self._probe_num(result.get("fts_score")) > 0
            if kw and fts:
                return "cold_kw_fts"
            if kw:
                return "cold_kw"
            if fts:
                return "cold_fts"
            content = result.get("content")
            if query and isinstance(content, str) and content.strip():
                try:
                    if _lex_evidence(query, content):
                        return "local_lex"
                except Exception:
                    pass
            return ""
        except Exception:
            return ""

    def _probe_recall_channel_of(self, result: Any, query: str,
                                 handle_ids: Any = None) -> str:
        """prefetch 事件 channel 口径 (观测, 与 server._recall_channel_of 一致)。

        H > K > S: id 命中本次 handle_ids 为 H; cold keyword/fts>0 或本地
        bigram 证据为 K; 其余 S。仅用于事件构造, 不参与选择/排序/注入。
        """
        try:
            if not isinstance(result, dict):
                return ""
            if handle_ids:
                try:
                    if result.get("id") in handle_ids:
                        return "H"
                except TypeError:
                    pass
            if (self._probe_num(result.get("keyword_score")) > 0
                    or self._probe_num(result.get("fts_score")) > 0):
                return "K"
            content = result.get("content")
            if query and isinstance(content, str) and content.strip():
                try:
                    if _lex_evidence(query, content):
                        return "K"
                except Exception:
                    pass
            return "S"
        except Exception:
            return ""

    def _build_prefetch_probe_event(self, query: str,
                                    candidates: List[Dict[str, Any]],
                                    selected: List[Dict[str, Any]],
                                    handle_ids: Any = None) -> Dict[str, Any]:
        """构造 prefetch 观测事件: 候选数组 + injected 布尔数组 + selected id 列表。

        ``returned_ids/channel/k_source`` 等数组按候选顺序与 ``injected`` 等长;
        query 绝不落明文, 只落 sha256/长度。

        F1: 先算 channel, 再用 channel 约束 k_source; H 行 k_source 必为 ""。
        F2: injected 按行身份 (``_probe_candidate_index``) 反向标记, 而不是用
        id 字符串 membership 反推; 重复/None id 不再全量误报 True。
        F3: channel/candidate_channels 对每条候选按与 ``_recall_channel_of``
        相同的 H>K>S 口径计算, 未进 selected / 被写回移除的候选不落 ""。
        """
        q = query or ""
        rows = list(candidates or [])
        final = list(selected or [])
        selected_ids = [self._probe_id_text(r.get("id")) if isinstance(r, dict)
                        else self._probe_id_text(r) for r in final]
        selected_keys = list(selected_ids)

        # F2: _select_fault_candidates 在每个被选中行副本上写入原始候选下标;
        # 该行身份随行经过 _dedupe_injected / _dedupe_hot_layer / 写回移除,
        # 最终仍留在 selected 里的下标集合就是真实注入集合。
        selected_by_idx: Dict[int, str] = {}
        all_final_indexed = bool(final)
        for r in final:
            idx = (r.get("_probe_candidate_index")
                   if isinstance(r, dict) else None)
            if (isinstance(idx, int) and not isinstance(idx, bool)
                    and idx >= 0):
                if idx not in selected_by_idx:
                    selected_by_idx[idx] = (
                        r.get("_channel") or r.get("channel") or "")
            else:
                all_final_indexed = False
        selected_idxs = set(selected_by_idx)
        use_row_identity = all_final_indexed and bool(selected_by_idx)

        # 兼容回退: 直接调用事件构造、selected 未带行身份下标时, 维持旧版
        # 按 id 集合判断 injected; 生产 _recall_sync 路径始终走行身份。
        channel_by_id: Dict[str, Any] = {}
        for r in final:
            if isinstance(r, dict):
                rid = self._probe_id_text(r.get("id"))
                if rid not in channel_by_id:
                    channel_by_id[rid] = (r.get("_channel")
                                          or r.get("channel") or "")

        returned_ids: List[str] = []
        dense_scores: List[float] = []
        keyword_scores: List[float] = []
        fts_scores: List[float] = []
        channels: List[str] = []
        k_sources: List[str] = []
        injected: List[bool] = []
        candidate_page_fault: List[bool] = []
        candidates_out: List[Dict[str, Any]] = []
        for row_index, r in enumerate(rows):
            if isinstance(r, dict):
                rid = self._probe_id_text(r.get("id"))
                dense = self._probe_num(r.get("dense_score"))
                kws = self._probe_num(r.get("keyword_score"))
                fts = self._probe_num(r.get("fts_score"))
                # F1/F3: selected 行以真实 _channel 为准; 未 selected 行按
                # 本次 handle_ids + K 证据同口径重算。selected 行 _channel
                # 为空时也回落到重算, 不落空 ""。
                if use_row_identity and row_index in selected_by_idx:
                    ch = selected_by_idx[row_index]
                    if not ch:
                        ch = self._probe_recall_channel_of(
                            r, q, handle_ids)
                elif not use_row_identity and channel_by_id.get(rid):
                    ch = channel_by_id[rid]
                else:
                    ch = self._probe_recall_channel_of(r, q, handle_ids)
                pf = bool(r.get("page_fault"))
                candidates_out.append({
                    "id": rid, "channel": ch, "keyword_score": kws,
                    "fts_score": fts, "dense_score": dense,
                    "page_fault": pf, "handle": r.get("handle"),
                })
            else:
                rid = self._probe_id_text(r)
                dense = 0.0
                kws = 0.0
                fts = 0.0
                ch = ""
                pf = False
                candidates_out.append({
                    "id": rid, "channel": ch, "keyword_score": kws,
                    "fts_score": fts, "dense_score": dense,
                    "page_fault": pf, "handle": None,
                })
            returned_ids.append(rid)
            dense_scores.append(dense)
            keyword_scores.append(kws)
            fts_scores.append(fts)
            channels.append(ch)
            # F1: K 证据必须服从已算出的 channel; H 行强制空串。
            k_sources.append(self._probe_k_source_of(q, r, channel=ch))
            if use_row_identity:
                injected.append(row_index in selected_idxs)
            else:
                injected.append(rid in selected_keys)
            candidate_page_fault.append(pf)

        return {
            "source": "prefetch",
            "query_sha256": _probe_query_sha256(q),
            "query_len": len(q),
            "top_k": _INJECT_TOP_N,
            "candidate_count": len(rows),
            "returned_ids": returned_ids,
            "dense_scores": dense_scores,
            "keyword_scores": keyword_scores,
            "fts_scores": fts_scores,
            "channel": channels,
            "k_source": k_sources,
            "selected": selected_ids,
            "injected": injected,
            "page_fault": bool(any(candidate_page_fault)),
            "restore": 0,
            "candidate_ids": list(returned_ids),
            "candidate_channels": list(channels),
            "candidate_page_fault": list(candidate_page_fault),
            "candidates": candidates_out,
        }

    def _emit_prefetch_probe(self, query: str,
                             candidates: List[Dict[str, Any]],
                             selected: List[Dict[str, Any]],
                             handle_ids: Any = None) -> None:
        """落一条 prefetch 只读观测; 失败完全静默。"""
        try:
            record_recall_probe(self._build_prefetch_probe_event(
                query, candidates, selected, handle_ids))
        except Exception:
            pass


    def _mark_injected_audit(self, results: List[Dict[str, Any]]) -> None:
        """仅写 sidecar last_injected_at 审计; 不刷新 last_active_at/weight。"""
        if not results:
            return
        try:
            now = datetime.now(timezone.utc).isoformat()
            store = LocalStore()
            for target in ("memory", "user"):
                ms = MetaStore(target, memory_path=store.memory_path,
                               user_path=store.user_path)
                id2stub = {}
                for e in store.entries(target):
                    m = ms.get_entry(e) or {}
                    if m.get("type") == "stub" and m.get("cold_id"):
                        id2stub[m["cold_id"]] = e
                for r in results:
                    rid = r.get("id")
                    if rid in id2stub:
                        ms.update_fields(id2stub[rid],
                                         last_injected_at=now)
        except Exception as e:
            logger.debug("last_injected_at audit failed: %s", e)

    def _local_handle_consensus(self, query: str) -> bool:
        """FIX4 P0: 召回前本地 H 共识预检 (零 RPC)。

        仅显式句柄命中, 或长度 >=4 的查询与 stub 目录主题/句柄匹配时返回
        True — 使真实规则的 H 通道查询不被 F1 闸门短路。"系统"/"继续" 这
        类 2 字整条低信息即使与泛化主题前缀有重叠也不放行 (避免用户确认
        语被目录主题反噬)。读取本地 sidecar, 不访问冷层。
        """
        q = (query or "").strip()
        if not q:
            return False
        try:
            records = self._directory_cache or self._load_directory()
        except Exception:
            return False
        for rec in records or []:
            handle = str(rec.get("handle") or "")
            if handle and handle in q:
                return True
        compact = re.sub(r"[\s，。,.!！?？~～、;；:：…]+", "", q)
        if len(compact) < 4:
            return False
        return any(self._handle_match(q, rec) for rec in records or [])

    @staticmethod
    def _shared_bigrams(a: str, b: str) -> int:
        """中文无分词 bigram 交集数 (纯字符串, 零依赖)。"""
        aa = re.sub(r"\s+", "", a or "")
        bb = re.sub(r"\s+", "", b or "")
        if len(aa) < 2 or len(bb) < 2:
            return 0
        ba = {aa[i:i + 2] for i in range(len(aa) - 1)}
        bb_set = {bb[i:i + 2] for i in range(len(bb) - 1)}
        return len(ba & bb_set)

    def _handle_match(self, query: str, rec: Dict[str, Any]) -> bool:
        """目录句柄/主题匹配: 句柄出现或主题串出现或共享 bigram≥2。"""
        q = query or ""
        handle = str(rec.get("handle") or "")
        topic = str(rec.get("topic") or "")
        if handle and handle in q:
            return True
        if topic and (topic in q or q in topic):
            return True
        return self._shared_bigrams(q, topic) >= 2

    def _normalize_abnormal_dense(self, results: list) -> list:
        """FIX-P2 (REVIEW-6 L2): 选择前把无法有限化的 dense_score 归 0。

        仅改写异常行 (不可转 float / 非有限 / 缺失), 让单条坏分数只影响该条
        自己; 可有限化分数一律原样返回, 以保持正常输入行为逐字节不变。
        """
        if not isinstance(results, list):
            return results
        changed = False
        rows = []
        for r in results:
            if (isinstance(r, dict)
                    and self._finite_score(r.get("dense_score")) is None):
                r = dict(r)
                r["dense_score"] = 0.0
                changed = True
            rows.append(r)
        return rows if changed else results

    def _keyword_consensus(self, q: str, result: Dict[str, Any]) -> bool:
        """K 通道: keyword/fts 有限正分 或 本地 bigram 词法证据 (sb≥2)。

        FIX-P2 (REVIEW-6 L2): 与 ``_probe_num``/``_probe_float`` 统一口径;
        float(x) 失败或非有限 (inf/"inf"/"1e400"/nan/超大整数) 一律按 0,
        不作为 K 证据, 也不得因单条异常分数让整条 prefetch 失败。
        """
        kw = self._probe_num(result.get("keyword_score"))
        fts = self._probe_num(result.get("fts_score"))
        if kw > 0 or fts > 0:
            return True
        content = result.get("content") or ""
        if not content:
            return False
        try:
            return _lex_evidence(q, content)
        except Exception:
            return False

    def _select_fault_candidates(self, q: str, results: List[Dict[str, Any]],
                                 handle_ids: set,
                                 *, action_trigger: bool = False) -> List[Dict[str, Any]]:
        """S/K/H 三通道共识 + H>K>S 排序, 注入 ≤_INJECT_TOP_N (沿用现有常量)。"""
        picked = []
        for candidate_index, r in enumerate(results):
            rid = r.get("id")
            # FIX-P2 (REVIEW-6 L2): 与 _probe_num 同口径有限化; 异常/非有限
            # dense 只按 0 参与本条判断, 不拖累其它候选或整条 prefetch。
            dense = self._finite_score(r.get("dense_score"))
            dense_abnormal = dense is None
            if dense is None:
                dense = 0.0
            h = bool(rid in handle_ids)
            k = self._keyword_consensus(q, r)
            s = dense >= HIT_WEAK_COS
            # 缺页候选: 0.42 下界单独即候选 (可注入上下文); 写回热层
            # F4: 至少需要 H 或 K 共识 — action_trigger/纯 S 一律不写回
            # (纯 S+action 只注入, 防闲聊把不相关规则全文换回热层)。
            if not (h or k or s):
                continue
            if h:
                ch = "H"
            elif k:
                ch = "K"
            else:
                ch = "S"
            rr = dict(r)
            if dense_abnormal:
                # L2: 本条异常 dense 归 0, 避免格式化整数值再次拖垮 prefetch;
                # 该行仍可凭 H/K 证据继续参与注入。
                rr["dense_score"] = 0.0
            rr["_channel"] = ch
            rr["_consensus"] = bool(h or k)  # F4: 不含 action_trigger
            rr["_action_recall"] = bool(action_trigger)
            # P1-F2: 仅观测的行身份 (原始候选下标), 不参与选择/排序/注入;
            # 事件构造用它精确区分重复 id 中真正注入的那一行。
            # observation-only bookkeeping; accepted exception per REVIEW-6 L1
            rr["_probe_candidate_index"] = candidate_index
            picked.append(rr)
        order = {"H": 0, "K": 1, "S": 2}

        def _rank_key(r):
            dense_norm = self._finite_score(r.get("dense_score"))
            return (order.get(r.get("_channel"), 3),
                    -(dense_norm if dense_norm is not None else 0.0))

        picked.sort(key=_rank_key)
        return picked[:_INJECT_TOP_N]

    def _filter_by_dense_topn(self, results: list) -> list:
        """单模型 dense 排序注入 (2026-08-11): 按 dense_score 降序取 top-N。

        替代旧 rerank 过滤: qwen3 分数跨查询不可比, 绝对阈值会误杀
        (实测相关 0.3-0.6 vs bge 0.7+, 旧基线 0.63 会把全部拦下)。
        只做相对排序 + 截断, 不设绝对门限。
        """
        if not results:
            return []
        ranked = sorted(results, key=lambda r: r.get("dense_score", 0), reverse=True)
        return ranked[:_INJECT_TOP_N]

    def _record_baseline(self, results: list) -> None:
        """记录 dense batch-max 到滚动基线 (无论 rerank 是否可用, 保持降级路径校准)。"""
        if not results:
            return
        top1 = max(r.get("dense_score", 0) for r in results)
        if top1:
            self._record_score(top1)

    @staticmethod
    def _format_results(results: List[Dict[str, Any]]) -> str:
        """将召回结果格式化为注入文本。

        分数显示与过滤逻辑一致 (2026-08-11 单模型): 显示 dense 分。
        """
        if not results:
            return ""
        lines = []
        for r in results:
            score = r.get("dense_score", 0)
            content = r.get("content", "")
            ch = r.get("_channel") or r.get("channel") or ""
            tag = f"|{ch}" if ch else ""
            lines.append(f"- [{score:.2f}{tag}] {content}")
        return ("## MemoryCore Recall\n"
                "> 以下为候选记忆(按相似度排序, 未经核实); 涉及精确事实/数值时请先交叉核对。\n"
                + "\n".join(lines))

    @staticmethod
    def _preprocess_query(query: str) -> str:
        """query 预处理: strip + 超长截断 (前段主导, 防语义稀释)。

        FIX4 P0: F1 闸门只在 `_recall_sync` 入口执行一次 — 该入口已先
        计算 action_trigger / 本地 H 共识让位, 不得在此二次裸判 gate,
        否则动作/H 共识虽绕过第一道也会被这里截回空串。
        """
        q = (query or "").strip()
        if not q:
            return ""
        if len(q) > _QUERY_MAX_LEN:
            q = q[:_QUERY_MAX_LEN]
        return q

    def _load_hot_layer_text(self) -> str:
        """读取热层 MEMORY.md + USER.md 全文, 供冷层注入去重 (2026-08-05)。"""
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
        """会话内去重 (2026-08-05): 同一会话已注入过的条目不再重复注入。"""
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
        """热层去重: 内容已存在于热层 (MEMORY.md/USER.md) 的不再注入。

        2026-08-28 (缺口1): 归一化比较 (全角标点/空白变体也判重) —
        与 local_store.add 去重共用 normalize_for_compare (单一真相源),
        冷层全文若已是热层条目的标点变体, 不再重复注入。
        """
        if not self._hot_norm:
            return results
        kept = []
        for r in results:
            content = r.get("content", "")
            if content and normalize_for_compare(content) in self._hot_norm:
                continue
            kept.append(r)
        return kept

    # -- 动态基线阈值 (2026-08-05 去掉水位分档, 固定系数) --------------------

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """每轮结束记录最近用户意图/assistant 动作文本 (零成本, 不落盘)。

        CACHE-POLICY-V2: 动作召回在无新用户消息时用最近用户意图 + 动作词
        合成 query; 本方法只做内存记录, 不访问冷层/LLM。
        """
        try:
            u = (user_content or "").strip()
            a = (assistant_content or "").strip()
            if u:
                self._last_user_intent = u[:_QUERY_MAX_LEN]
            if a:
                self._last_assistant_action = a[:_QUERY_MAX_LEN]
            # F4: pending 的存续只看用户意图的动作性, assistant 文本不参与;
            # 普通下一轮清空旧合成 query, 防污染。
            if not u or not self._action_trigger_hit(u):
                self._pending_action_query = ""
        except Exception as e:
            logger.debug("sync_turn failed: %s", e)

    def _record_score(self, top1: float) -> None:
        """每次召回后记录 top1, 样本满 _BASELINE_WINDOW 时滚动淘汰最旧,
        每 _BASELINE_RECALC_EVERY 个新样本重算中位数并持久化。
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
        """返回当前基线。"""
        with self._baseline_lock:
            return self._baseline_value

    def _load_baseline(self) -> None:
        """从持久化文件加载基线。"""
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
        """原子写基线到持久化文件。"""
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


def register(ctx) -> None:
    ctx.register_memory_provider(MemoryCorePrefetchProvider())
