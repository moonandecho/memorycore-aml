#!/usr/bin/env python3
"""core/llm_config.py — LLM 配置唯一真相源 (惰性解析 + 脱敏 + 观测三态 + 安全阀)

2026-09-12 评审 v2 落地 (5 条必改全部落实; 三家仓库共享, tools/check_llm_sync.sh
校验一致性):

设计要点:
  - resolve() 调用时解析, 绝不 import 时冻结 (根治 config.LLM_API_KEY 模块
    常量在 import 时绑定一次的缺陷); 结果带 TTL 缓存 (默认 30s) 防 .env
    并发修改抖动 (评审 B2)。
  - 来源链 (必改项 1): ① env LLM_API_KEY → ② env LLM_BASE_URL/LLM_MODEL
    (配置项, 非密钥, 作为任何文件来源 key 的覆盖层) → ③ 文件 ~/.hermes/.env
    (白名单键) → ④ 文件 ~/.hermes/config.yaml 的 model.* (仅当其中 provider
    与当前一致; key/base_url/model 三者一起门控, 禁止跨源混搭)。
    provider 映射只在文件层 (DEEPSEEK_API_KEY / XIAOMI_API_KEY), env 层不做
    provider 映射。
  - 文件来源 (必改项 3): 发布版默认关闭 (MEMCORE_LLM_FILE_SOURCES=1 才读
    ~/.hermes/.env / config.yaml, 防参赛版静默捡 key 外呼), 本地运行版默认
    开启。本模块唯一发行差异 = FILE_SOURCES_DEFAULT 一行。
  - source 只含 "文件路径:键名" / "env:键名", 绝不含 key 本体 (必改项 4);
    mask() 为全仓统一脱敏函数, 任何日志/报告/异常拼接 key 前必须过它。
  - 观测三态 (必改项 5): "未配置（已尝试: …）" / "已解析（来源 …）" /
    "通路已验证" — 后者仅在真实调用成功后出现, 禁止把"已解析"报成"可用"。
  - 安全阀 (评审 C2): MEMCORE_LLM_ENABLED=0 总开关; 每轮调用上限
    (MEMCORE_LLM_MAX_CALLS 默认 8; 冷层治理 MEMCORE_LLM_COLD_MAX_CALLS
    独立上限); 连续失败退避 (本轮任一调用失败后剩余候选全部跳过)。

用法:
  from core import llm_config            # 本地运行版
  from memorycore.core import llm_config # 发布版

  with llm_config.guard_session(stat=stat):
      cfg = llm_config.acquire("压缩")
      if cfg is None:
          return None
      try:
          data = llm_config.chat(cfg, payload, timeout=15.0)
      except llm_config.LLMError as e:
          llm_config.note_failure(e.category, e.detail)
          return None
      llm_config.note_success()
"""
from __future__ import annotations

import contextvars
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

log = logging.getLogger("memorycore.llm")

# ---- 缺省值与 provider 白名单 (文件层专用) ---------------------------------

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 15.0

# 文件层 <PROVIDER>_API_KEY 白名单: key 键名 → (默认 base_url, 默认 model)。
# provider 映射只在文件层 (必改项 1); env 层只认 LLM_API_KEY。
# 缺省值同 Hermes 同源: deepseek-v4-flash / api.xiaomimimo.com v1 + mimo-v2.5。
PROVIDERS: Dict[str, Tuple[str, str]] = {
    "DEEPSEEK": ("https://api.deepseek.com", "deepseek-v4-flash"),
    "XIAOMI": ("https://api.xiaomimimo.com/v1", "mimo-v2.5"),
}
DEFAULT_PROVIDER = "deepseek"  # LLM_PROVIDER 未设置时的缺省

# 文件来源默认开关: 本地运行版 True / 发布版 False — 本模块唯一发行差异
# (发布版改为 False, 见必改项 3; tools/check_llm_sync.sh 对此行做归一化)。
FILE_SOURCES_DEFAULT = False

ENV_FILE = Path(os.path.expanduser("~/.hermes/.env"))
CONFIG_YAML = Path(os.path.expanduser("~/.hermes/config.yaml"))

# ~/.hermes/.env 白名单键 (必改项 4): 通用 LLM_ 键 + provider key 键名
_ENV_FILE_KEYS: Set[str] = {
    "LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL", "LLM_PROVIDER",
} | {f"{p}_API_KEY" for p in PROVIDERS}

# 解析 TTL (秒): 防 .env 并发修改抖动 (评审 B2/B7-6)
TTL_SECONDS = float(os.environ.get("MEMCORE_LLM_CONFIG_TTL", "30") or 30)

# 每轮调用上限缺省 (评审 C2): 溢流/周整理共用 MEMCORE_LLM_MAX_CALLS=8,
# 冷层治理独立上限 MEMCORE_LLM_COLD_MAX_CALLS=8
DEFAULT_MAX_CALLS = 8


# ---- 配置对象与解析 --------------------------------------------------------

@dataclass(frozen=True)
class LLMConfig:
    """LLM 配置 (惰性解析产物)。

    source 只含 "文件路径:键名" / "env:键名" / "disabled:…", 绝不含 key 本体。
    """
    key: str = ""
    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    source: str = ""        # 命中的来源 (脱敏, 无 key)
    tried: str = ""         # 已尝试的来源链 (脱敏, 无 key)
    enabled: bool = True    # 总开关 MEMCORE_LLM_ENABLED
    resolved_at: float = 0.0

    @property
    def configured(self) -> bool:
        """配置可用 = 总开关开 + key 非空。仅代表"解析成功", 不代表通路可用。"""
        return self.enabled and bool(self.key)

    @property
    def masked_key(self) -> str:
        return mask(self.key)


def mask(key: str) -> str:
    """统一脱敏 (必改项 4): 任何日志/报告/异常拼接 key 前必须过它。"""
    if not key:
        return "<空>"
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:4]}...{key[-4:]}"


def file_sources_enabled() -> bool:
    """文件来源开关 (必改项 3): 发布版默认关, 本地版默认开; env 可覆盖。"""
    default = "1" if FILE_SOURCES_DEFAULT else "0"
    return os.environ.get("MEMCORE_LLM_FILE_SOURCES", default) != "0"


def parse_dotenv(path, whitelist) -> Dict[str, str]:
    """解析 .env 白名单键 (全仓唯一实现, 修评审 B3 三个坑)。

    规则:
      - 空行 / # 注释行跳过
      - "export " 前缀精确切 (startswith; 原 lstrip("export ") 是字符集剥离,
        会啃掉以 e/x/p/o/r/t 开头的键名)
      - 键名必须精确命中 whitelist, 其余静默忽略 (白名单语义)
      - 值: 引号包住 → 取引号内 (引号内 CRLF/含 = /含 # 均原样保留);
            裸值 → 剥离尾部 " #comment" 行内注释
      - 空值不收录 (原 setdefault 会把空串写进缺省位)
    """
    out: Dict[str, str] = {}
    if not path or not Path(path).is_file():
        return out
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError:
        return out
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k not in whitelist:
            continue
        v = v.strip()
        if v[:1] in ("'", '"'):
            quote = v[0]
            end = v.find(quote, 1)
            if end == -1:
                end = len(v)
            v = v[1:end]
        else:
            if " #" in v:
                v = v.split(" #", 1)[0]
            v = v.rstrip()
        v = v.strip()
        if v == "":
            continue  # B3-3: 空值不收录, 缺省值不被空串覆盖
        out[k] = v
    return out


def _env_or(name: str, default: str) -> str:
    v = os.environ.get(name, "").strip()
    return v or default


def _load_config_yaml_model() -> Optional[Dict[str, str]]:
    """读 ~/.hermes/config.yaml 的 model 段 (失败返回 None)。

    E6 可见化: 解析/IO 异常打 warning (进程内一次), 不再 except: pass 静默。
    """
    if not CONFIG_YAML.is_file():
        return None
    try:
        import yaml
        with open(CONFIG_YAML, encoding="utf-8") as f:
            m = yaml.safe_load(f)
        return m.get("model") if isinstance(m, dict) else None
    except Exception as e:
        _warn_once("llm.config_yaml_error",
                   "LLM: config.yaml 读取失败 (%s) → 跳过该来源", e)
        return None


_warned: Set[str] = set()


def _warn_once(tag: str, fmt: str, *args) -> None:
    if tag in _warned:
        return
    _warned.add(tag)
    log.warning(fmt, *args)


def _resolve_uncached() -> LLMConfig:
    tried: List[str] = ["env:LLM_API_KEY"]

    # ---- 总开关 (安全阀 1) ----
    if os.environ.get("MEMCORE_LLM_ENABLED", "1") == "0":
        return LLMConfig(key="", source="disabled:MEMCORE_LLM_ENABLED",
                         tried=", ".join(tried), enabled=False)

    # ---- ① env LLM_API_KEY ----
    env_key = os.environ.get("LLM_API_KEY", "").strip()
    if env_key:
        return LLMConfig(
            key=env_key,
            base_url=_env_or("LLM_BASE_URL", DEFAULT_BASE_URL),
            model=_env_or("LLM_MODEL", DEFAULT_MODEL),
            source="env:LLM_API_KEY",
            tried=", ".join(tried),
            resolved_at=time.time())

    # ---- ② env LLM_BASE_URL/LLM_MODEL (配置项覆盖层, 非密钥) ----
    env_base = os.environ.get("LLM_BASE_URL", "").strip()
    env_model = os.environ.get("LLM_MODEL", "").strip()
    env_provider = os.environ.get("LLM_PROVIDER", "").strip().lower()

    if not file_sources_enabled():
        return LLMConfig(
            key="",
            base_url=env_base or DEFAULT_BASE_URL,
            model=env_model or DEFAULT_MODEL,
            source="",
            tried=", ".join(
                tried + [f"file:{ENV_FILE}(未启用, MEMCORE_LLM_FILE_SOURCES)",
                         f"file:{CONFIG_YAML}:model.*(未启用)"]),
            resolved_at=time.time())

    # ---- ③ 文件 ~/.hermes/.env (白名单) ----
    data = parse_dotenv(ENV_FILE, _ENV_FILE_KEYS)
    tried.append(f"file:{ENV_FILE}(白名单)")
    provider = env_provider or data.get("LLM_PROVIDER", "").strip().lower() \
        or DEFAULT_PROVIDER

    fkey = data.get("LLM_API_KEY", "").strip()
    provider_key = ""
    provider_name = ""
    if not fkey:
        # 优先取与当前 provider 一致的 key, 否则取第一个命中的白名单 provider key
        pref = f"{provider.upper()}_API_KEY"
        for p in ([pref] if pref in _ENV_FILE_KEYS else []) + \
                [f"{name}_API_KEY" for name in PROVIDERS if f"{name}_API_KEY" != pref]:
            pk = data.get(p, "").strip()
            if pk:
                provider_key = pk
                provider_name = p[: -len("_API_KEY")]  # "DEEPSEEK_API_KEY" → "DEEPSEEK"
                break
    if fkey or provider_key:
        key = fkey or provider_key
        src_key = "LLM_API_KEY" if fkey else f"{provider_name}_API_KEY"
        fbase = data.get("LLM_BASE_URL", "").strip()
        fmodel = data.get("LLM_MODEL", "").strip()
        # provider key 与自身默认 base_url/model 配对校验 (B7-1: 防 401 混配)
        pbase, pmodel = PROVIDERS.get(provider_name,
                                      (DEFAULT_BASE_URL, DEFAULT_MODEL))
        return LLMConfig(
            key=key,
            base_url=env_base or fbase or pbase or DEFAULT_BASE_URL,
            model=env_model or fmodel or pmodel or DEFAULT_MODEL,
            source=f"file:{ENV_FILE}:{src_key}",
            tried=", ".join(tried),
            resolved_at=time.time())

    # ---- ④ 文件 ~/.hermes/config.yaml 的 model.* (provider 门控) ----
    tried.append(f"file:{CONFIG_YAML}:model.*")
    yml = _load_config_yaml_model()
    if yml:
        y_provider = str(yml.get("provider") or "").strip().lower()
        if y_provider == provider:
            y_key = str(yml.get("api_key") or "").strip()
            if y_key:
                ybase = str(yml.get("base_url") or "").strip()
                ymodel = str(yml.get("default") or "").strip()
                return LLMConfig(
                    key=y_key,
                    base_url=env_base or ybase or DEFAULT_BASE_URL,
                    model=env_model or ymodel or DEFAULT_MODEL,
                    source=f"file:{CONFIG_YAML}:model.api_key",
                    tried=", ".join(tried),
                    resolved_at=time.time())
            log.debug("LLM: config.yaml model.provider 一致但 api_key 为空 → 未配置")
        else:
            # 必改项 1 后半: provider 不一致 → key/base_url/model 整组跳过,
            # 绝不跨源混搭
            log.debug("LLM: config.yaml model.provider=%r 与当前 %r 不一致 → 整组跳过",
                      y_provider, provider)

    return LLMConfig(
        key="",
        base_url=env_base or DEFAULT_BASE_URL,
        model=env_model or DEFAULT_MODEL,
        source="",
        tried=", ".join(tried),
        resolved_at=time.time())


# ---- 惰性解析 + TTL 缓存 ----------------------------------------------------

_cache: Dict[str, object] = {"cfg": None, "expires": 0.0}


def resolve(force: bool = False) -> LLMConfig:
    """惰性解析 LLM 配置 (调用时生效, import 不冻结)。带 TTL 缓存。

    注意: 配置只是"解析成功", 通路是否真正可用以真实调用成功为准
    (观测三态, 必改项 5)。
    """
    now = time.time()
    if not force and _cache["cfg"] is not None and now < _cache["expires"]:
        return _cache["cfg"]  # type: ignore[return-value]
    cfg = _resolve_uncached()
    _cache["cfg"] = cfg
    _cache["expires"] = now + TTL_SECONDS
    return cfg


def invalidate_cache() -> None:
    """清 TTL 缓存 (测试/显式失效用)。"""
    _cache["cfg"] = None
    _cache["expires"] = 0.0


# ---- HTTP 调用 (全仓唯一实现, 语义错误分类) ----------------------------------

class LLMError(Exception):
    """LLM 调用失败, 语义分类 (负路径断言按类别, 不断言精确文本)。

    category ∈ auth_failed / timeout / network_unreachable / http_error /
    bad_response。错误文本绝不包含 key / Authorization 头。
    """

    def __init__(self, category: str, detail: str = ""):
        super().__init__(f"{category}: {detail}")
        self.category = category
        self.detail = detail


def chat(cfg: LLMConfig, payload: dict, timeout: Optional[float] = None) -> dict:
    """POST {base_url}/chat/completions。成功返回响应 JSON, 失败抛 LLMError。"""
    import json as _json
    import socket
    import urllib.error
    import urllib.request

    t = float(timeout if timeout is not None
              else os.environ.get("LLM_TIMEOUT", str(DEFAULT_TIMEOUT)))
    req = urllib.request.Request(
        cfg.base_url.rstrip("/") + "/chat/completions",
        data=_json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=t) as resp:
            return _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        cat = "auth_failed" if e.code in (401, 403) else "http_error"
        raise LLMError(cat, f"HTTP {e.code}") from e
    except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
        if isinstance(e, (TimeoutError, socket.timeout)):
            raise LLMError("timeout", f"timeout after {t}s") from e
        reason = getattr(e, "reason", None)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise LLMError("timeout", f"timeout after {t}s") from e
        raise LLMError("network_unreachable",
                       f"{type(reason).__name__ if reason else type(e).__name__}") \
            from e
    except OSError as e:
        raise LLMError("network_unreachable", type(e).__name__) from e
    except (ValueError, KeyError, IndexError) as e:
        raise LLMError("bad_response", type(e).__name__) from e


# ---- 每轮安全阀 + 观测 (评审 C2 / 必改项 5) ---------------------------------

_current_guard: contextvars.ContextVar[Optional["LLMGuard"]] = \
    contextvars.ContextVar("llm_guard", default=None)
_fallback_guard: Optional["LLMGuard"] = None


def _fallback() -> "LLMGuard":
    """无会话时 (如 e2e 脚本直调 judge_*) 的兜底护栏: 有退避无上限。"""
    global _fallback_guard
    if _fallback_guard is None:
        _fallback_guard = LLMGuard(stat=None, max_calls=None)
    return _fallback_guard


class LLMGuard:
    """每轮 LLM 调用护栏: 总开关 + 调用上限 + 连续失败退避 + 观测计数。

    状态写入 stat["llm"] (进 MCP 工具返回 / weekly 报告):
      {status, enabled, configured, source, model, calls, success, failures,
       skipped_cap, skipped_backoff, max_calls, backoff, last_error, tried}
    status ∈ disabled / unconfigured / verified / degraded / configured。
    """

    def __init__(self, stat: Optional[dict] = None,
                 max_calls: Optional[int] = None, name: str = ""):
        self.stat = stat
        self.max_calls = max_calls
        self.name = name
        self.calls = 0
        self.success = 0
        self.failures = 0
        self.skipped_cap = 0
        self.skipped_backoff = 0
        self.backoff = False
        self.last_error = ""
        self.last_block = ""
        self._token = None
        self._unconfigured_logged = False
        self._resolved_logged = False
        self._verified_logged = False

    # -- 护栏 -----------------------------------------------------------

    def acquire(self, consumer: str) -> Optional[LLMConfig]:
        """尝试获取一次 LLM 调用资格。被拦 (开关/未配置/上限/退避) → None。

        consumer: 用途名 (如 "压缩"/"下沉确认"/"合并"), 进观测文案。
        """
        cfg = resolve()
        self.last_block = ""
        if not cfg.enabled:
            self.last_block = "disabled"
            if not self._unconfigured_logged:
                log.warning("LLM: 总开关关闭 (MEMCORE_LLM_ENABLED=0) → %s已跳过",
                            consumer)
                self._unconfigured_logged = True
            return None
        if not cfg.key:
            self.last_block = "unconfigured"
            if not self._unconfigured_logged:
                log.warning("LLM: 未配置（已尝试: %s）→ %s已跳过",
                            cfg.tried, consumer)
                self._unconfigured_logged = True
            return None
        if self.backoff:
            self.last_block = "backoff"
            self.skipped_backoff += 1
            log.warning("LLM: 本轮已失败退避（last: %s）→ %s已跳过",
                        self.last_error, consumer)
            return None
        if self.max_calls is not None and self.calls >= self.max_calls:
            self.last_block = "cap"
            self.skipped_cap += 1
            log.warning("LLM: 达到本轮调用上限 %d → %s已跳过",
                        self.max_calls, consumer)
            return None
        if not self._resolved_logged:
            # 观测三态之二: 已解析 — 仅代表配置解析成功, 不是"通路可用"
            log.info("LLM: 已解析（来源 %s, 模型 %s）", cfg.source, cfg.model)
            self._resolved_logged = True
        self.calls += 1
        return cfg

    def note_success(self) -> None:
        """真实调用成功后调用 (观测三态之三: 通路已验证)。"""
        self.success += 1
        if not self._verified_logged:
            log.info("LLM: 通路已验证（成功 %d/%d）", self.success, self.calls)
            self._verified_logged = True

    def note_failure(self, category: str = "error", detail: str = "") -> None:
        """调用失败后调用: 计数 + 触发本轮退避 (剩余候选全部跳过)。"""
        self.failures += 1
        self.last_error = category + (f": {detail}" if detail else "")
        self.backoff = True
        log.warning("LLM: 调用失败（%s）→ 本轮退避, 剩余候选全部跳过",
                    self.last_error)

    def block_kind(self) -> str:
        """上一次 acquire 被拦的原因: '' / disabled / unconfigured / cap / backoff。"""
        return self.last_block

    # -- 落盘 -----------------------------------------------------------

    def snapshot(self) -> dict:
        cfg = resolve()
        if not cfg.enabled:
            status = "disabled"
        elif not cfg.key:
            status = "unconfigured"
        elif self.success > 0:
            status = "verified"
        elif self.failures > 0:
            status = "degraded"
        else:
            status = "configured"
        return {
            "status": status,
            "enabled": cfg.enabled,
            "configured": cfg.configured,
            "source": cfg.source,
            "model": cfg.model,
            "tried": cfg.tried,
            "calls": self.calls,
            "success": self.success,
            "failures": self.failures,
            "skipped_cap": self.skipped_cap,
            "skipped_backoff": self.skipped_backoff,
            "max_calls": self.max_calls,
            "backoff": self.backoff,
            "last_error": self.last_error,
        }

    def close(self) -> dict:
        """结束会话: 状态写入 stat["llm"] 并复位 contextvar。返回快照。"""
        snap = self.snapshot()
        if isinstance(self.stat, dict):
            self.stat["llm"] = snap
        if self._token is not None:
            _current_guard.reset(self._token)
            self._token = None
        return snap


def start_session(stat: Optional[dict] = None,
                  max_calls: Optional[int] = None,
                  name: str = "") -> LLMGuard:
    """开始一轮 LLM 会话 (run_overflow / run_maintenance / weekly 入口调用)。

    返回 guard; 结束时必须调 guard.close() (写入 stat["llm"] + 复位)。
    嵌套会话安全 (close 恢复外层 guard)。max_calls=None → 取 env 缺省上限
    (显式传 None 想无限上限的场景请直接构造 LLMGuard)。
    """
    if max_calls is None:
        max_calls = default_max_calls()
    guard = LLMGuard(stat=stat, max_calls=max_calls, name=name)
    guard._token = _current_guard.set(guard)
    return guard


@contextmanager
def guard_session(stat: Optional[dict] = None,
                  max_calls: Optional[int] = None,
                  name: str = ""):
    """start_session 的 with 形式 (测试 / 嵌套消费方用)。"""
    guard = start_session(stat=stat, max_calls=max_calls, name=name)
    try:
        yield guard
    finally:
        guard.close()


def _current() -> LLMGuard:
    return _current_guard.get() or _fallback()


def acquire(consumer: str) -> Optional[LLMConfig]:
    """消费点入口: 取配置 + 过护栏。None → 按各自保守语义降级。"""
    return _current().acquire(consumer)


def note_success() -> None:
    _current().note_success()


def note_failure(category: str = "error", detail: str = "") -> None:
    _current().note_failure(category, detail)


def block_kind() -> str:
    """当前会话最近一次 acquire 被拦原因 (维护保守回退语义用)。"""
    return _current().block_kind()


def default_max_calls() -> int:
    """每轮调用上限缺省 (env 可覆盖)。"""
    try:
        return int(os.environ.get("MEMCORE_LLM_MAX_CALLS", str(DEFAULT_MAX_CALLS)))
    except ValueError:
        return DEFAULT_MAX_CALLS


def cold_max_calls() -> int:
    """冷层治理独立上限 (评审 C2)。"""
    try:
        return int(os.environ.get("MEMCORE_LLM_COLD_MAX_CALLS",
                                  str(DEFAULT_MAX_CALLS)))
    except ValueError:
        return DEFAULT_MAX_CALLS


def format_status(snap: dict) -> str:
    """观测三态 → 报告单行文案 (weekly 报告 / MCP 输出共用)。"""
    if not snap.get("enabled"):
        return "总开关关闭 (MEMCORE_LLM_ENABLED=0) — 全部 LLM 步骤已跳过"
    if not snap.get("configured"):
        return (f"未配置（已尝试: {snap.get('tried', '')}）"
                "→ 压缩/下沉确认/合并已跳过")
    s = f"{snap.get('status')}（来源 {snap.get('source')}, 模型 {snap.get('model')}）"
    if snap.get("calls"):
        s += (f"; 调用 {snap['calls']}/{snap.get('max_calls')}"
              f" 成功 {snap.get('success', 0)} 失败 {snap.get('failures', 0)}"
              f" (退避 {snap.get('skipped_backoff', 0)}"
              f" 上限 {snap.get('skipped_cap', 0)})")
        if snap.get("backoff"):
            s += f"; 本轮退避中: {snap.get('last_error')}"
    return s
