#!/usr/bin/env python3
"""core/recall_probe.py — P0 只读召回观测探针 (fail-silent)。

本轮只观测、不改排序、不改冷/热层任何写路径。

设计边界 (冻结口径):
  * 开关 ``MEMORYCORE_RECALL_PROBE=1`` 才写; 默认 ``0`` = 零文件创建。
  * 独立 JSONL, 绝不写 ``activity.jsonl`` (后者是 S4 活性判定输入)。
  * 只落观测字段白名单; 默认不落 query 明文 (只保留 sha256/长度)。
  * 45 天 / 256KB 滚动截断, 与 activity.jsonl 同款容量口径。
  * 任何异常都静默丢弃; 通过 ``get_probe_metrics()`` 暴露尝试/成功/失败计数,
    测试与后续运维可观察失败率, 但调用方不应依赖探针成功。

落盘路径:
  * 默认 ``<core.config.MEMORY_DIR>/recall_probe.jsonl``；
  * 测试/排障可用 ``MEMORYCORE_RECALL_PROBE_FILE`` 覆盖 (仅路径, 不改行为);
    覆盖路径同样受“拒绝符号链接 / 拒绝 activity.jsonl”校验。
  * ``MEMORYCORE_RECALL_PROBE_FILE`` 为空/全空白时按未设置处理, 回退默认
    路径 ``<MEMORY_DIR>/recall_probe.jsonl`` (不报错, 不写相对路径)。
  * ``MEMORYCORE_RECALL_PROBE_COMPACT=0`` 可禁用滚动/截断 (只读调试开关;
    文件写入路径与事件字段不受影响)。

TODO(P1): 热层 prefetch 侧接入
(``hermes-plugin/memorycore-prefetch/__init__.py::_recall_sync``) 留待下一轮；
本轮明确不改插件, 避免影响 K/H/S 共识与写回行为。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import stat
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import config as _config
from .config import (ACTIVITY_LOG_MAX_BYTES as PROBE_MAX_BYTES,
                     ACTIVITY_LOG_RETENTION_DAYS as PROBE_RETENTION_DAYS)

# ---- 容量口径 ----
# 单一来源: core/config.py 的 ACTIVITY_LOG_* (45d / 256KB), 与活动日志同款,
# 满足 AGENTS.md "机制常量集中 config" 约定; 不另立同名字面量。
PROBE_FILE_NAME = "recall_probe.jsonl"

# 单条事件级上限: 超过即字段截断; 截断后仍超则丢弃并计 errors。
PROBE_MAX_EVENT_BYTES = 64 * 1024
# IMP-3: 滚动预算与 record/_compact 一样由 PROBE_MAX_BYTES*0.8 派生。
# 把“单条事件上限不得超过滚动预算”的出厂常量关系显式固化: 出厂常量下
# 65536 <= 209715, 下面的断言在 import 时真实求值为真。
PROBE_ROLLING_BUDGET_BYTES = int(PROBE_MAX_BYTES * 0.8)
assert PROBE_MAX_EVENT_BYTES <= PROBE_ROLLING_BUDGET_BYTES, (
    "probe budget invariant violated: "
    f"PROBE_MAX_EVENT_BYTES={PROBE_MAX_EVENT_BYTES} > "
    f"PROBE_ROLLING_BUDGET_BYTES={PROBE_ROLLING_BUDGET_BYTES}")
PROBE_MAX_STRING_CHARS = 4096
PROBE_MAX_ARRAY_ITEMS = 256

_ENV_ENABLED = "MEMORYCORE_RECALL_PROBE"
_ENV_PATH = "MEMORYCORE_RECALL_PROBE_FILE"
_ENV_COMPACT = "MEMORYCORE_RECALL_PROBE_COMPACT"
_ENV_ROLLING = "MEMORYCORE_RECALL_PROBE_ROLLING"
_ENV_NO_COMPACT = "MEMORYCORE_RECALL_PROBE_NO_COMPACT"
_ENV_DISABLE_COMPACT = "MEMORYCORE_RECALL_PROBE_DISABLE_COMPACT"

# 事件字段白名单: 防止调用方误传 query/content 等明文进落盘。
_EVENT_FIELDS = (
    "ts",
    "source",
    "query_sha256",
    "query_len",
    "top_k",
    "candidate_count",
    "returned_ids",
    "dense_scores",
    "keyword_scores",
    "fts_scores",
    "channel",
    "selected",
    "page_fault",
    "restore",
    "latency_ms",
    "error",
    # R3: restore 前候选快照 (只含 id/channel/分数/page_fault, 无正文)
    "candidate_ids",
    "candidate_channels",
    "candidate_page_fault",
    "candidates",
    "restored_ids",
    "truncated",
)

_ARRAY_FIELDS = (
    "returned_ids", "dense_scores", "keyword_scores", "fts_scores",
    "channel", "selected", "candidate_ids", "candidate_channels",
    "candidate_page_fault", "candidates", "restored_ids",
)

_METRICS: Dict[str, int] = {
    "attempts": 0, "written": 0, "errors": 0, "dropped": 0}


class ProbePathError(PermissionError):
    """探针目标路径命中冻结护栏 (符号链接 / activity.jsonl)。"""


def reset_probe_metrics() -> None:
    """测试辅助: 清零探针计数器 (只读观测, 不影响行为)。"""
    for _k in ("attempts", "written", "errors", "dropped"):
        _METRICS[_k] = 0


def get_probe_metrics() -> Dict[str, int]:
    """返回探针写入尝试/成功/失败计数 (fail-silent 可观测)。"""
    return dict(_METRICS)


def _enabled() -> bool:
    return os.environ.get(_ENV_ENABLED, "0").strip() == "1"


def _compact_enabled() -> bool:
    """默认开; 任一滚动调试开关显式置 0 时禁用滚动/截断。

    别名: ``MEMORYCORE_RECALL_PROBE_COMPACT`` / ``MEMORYCORE_RECALL_PROBE_ROLLING``。
    """
    for env_name in (_ENV_COMPACT, _ENV_ROLLING):
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip().lower() in ("0", "false", "no", "off"):
            return False
    for env_name in (_ENV_NO_COMPACT, _ENV_DISABLE_COMPACT):
        raw = os.environ.get(env_name)
        if raw is not None and raw.strip().lower() not in ("", "0", "false", "no", "off"):
            return False
    return True


def _probe_file() -> Path:
    override = (os.environ.get(_ENV_PATH) or "").strip()
    if override:
        return Path(override)
    return Path(_config.MEMORY_DIR) / PROBE_FILE_NAME


def _activity_paths() -> List[Path]:
    """activity 主日志 + 其 lock; 两者都不得被探针覆盖。

    发布版 config 有 ``ACTIVITY_LOG_FILE``; 若某版本缺该常量, 退化为
    ``<MEMORY_DIR>/activity.jsonl`` (仍按本仓库实际日志文件守卫, 不放行)。
    """
    try:
        base = Path(_config.ACTIVITY_LOG_FILE)
    except AttributeError:
        try:
            base = Path(_config.MEMORY_DIR) / "activity.jsonl"
        except Exception:
            return []
    except Exception:
        return []
    return [base, Path(str(base) + ".lock")]


def _resolved(path: Path) -> Path:
    try:
        return Path(os.path.realpath(str(path)))
    except Exception:
        return path


def _blocked_inode_match(st: os.stat_result) -> bool:
    """st_dev + st_ino 与 activity / activity.lock 任一相同即命中。

    硬链接在 lstat 下是普通文件、realpath 仍是探针路径; 只有 inode 比对能收口。
    """
    for blocked in _activity_paths():
        try:
            bst = os.stat(str(blocked))
        except OSError:
            # activity / lock 不存在时没有可比较的 inode, 退化为 realpath 比较。
            continue
        if (st.st_dev, st.st_ino) == (bst.st_dev, bst.st_ino):
            return True
    return False


def _fd_hits_blocked(fd: int) -> bool:
    """已打开 fd 的 inode 是否命中 activity / activity.lock (TOCTOU 兜底)。"""
    try:
        st = os.fstat(fd)
    except OSError:
        return False
    return _blocked_inode_match(st)


def _reject_probe_path(path: Path) -> bool:
    """路径命中防护返回 True: 符号链接、inode 命中 activity/lock 或 realpath 相同。"""
    # 1) 最终路径若已存在且为符号链接, 一律拒绝 (不 follow)。
    try:
        st = os.lstat(str(path))
        if stat.S_ISLNK(st.st_mode):
            return True
        # IMP-1/MIN-2: 已存在的 FIFO/目录/socket/设备等非普通文件一律拒绝。
        # 该判断发生在创建 <path>.lock 之前, 因此拒绝后不会留下 lock 残留。
        if not stat.S_ISREG(st.st_mode):
            return True
    except FileNotFoundError:
        pass
    except OSError:
        # 不可 lstat 的路径交给后续写失败 fail-silent。
        pass
    # 2) 解析 realpath 后与 activity.jsonl / lock 比较 (覆盖 env override;
    #    activity / lock 不存在时同样适用)。
    real = _resolved(path)
    for blocked in _activity_paths():
        if real == _resolved(blocked):
            return True
    # 3) 目标已存在时按 st_dev/st_ino 比对 (S1 硬链接同类绕过)。
    try:
        st = os.stat(str(path))
    except OSError:
        return False
    return _blocked_inode_match(st)


def _open_lock_handle(lock_path: Path):
    """以 O_NOFOLLOW 打开探针 lock 文本句柄; 先过路径守卫, 再 fstat 兜底。"""
    if _reject_probe_path(lock_path):
        raise ProbePathError(f"probe lock path rejected: {lock_path}")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    raw_fd = os.open(str(lock_path), flags, 0o600)
    try:
        if _fd_hits_blocked(raw_fd):
            raise ProbePathError(f"probe lock fd rejected: {lock_path}")
    except BaseException:
        try:
            os.close(raw_fd)
        except OSError:
            pass
        raise
    return os.fdopen(raw_fd, "a+", encoding="utf-8")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any, limit: int = 280) -> Optional[str]:
    if value is None:
        return None
    return str(value)[:limit]


def _probe_id(value: Any) -> str:
    """id 契约: None -> ""; 其余保留可表示值 (bytes 走 str 降级为文本)。"""
    if value is None:
        return ""
    text = str(value)
    return text[:PROBE_MAX_STRING_CHARS]


def _safe_float(value: Any, default: float = 0.0) -> float:
    """非有限值 (nan/inf/"nan"/超大 1e400) 一律归 default, 保证严格 JSON。"""
    try:
        if isinstance(value, bool):
            num = float(value)
        else:
            num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return num if math.isfinite(num) else default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(num):
        return default
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(num)


def _json_safe(value: Any, _depth: int = 0) -> Any:
    """把任意对象降级为 json.dumps 必定成功的结构 (不丢整条事件)。"""
    if _depth > 6:
        return _clean_text(value)
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else 0.0
    if isinstance(value, dict):
        return {str(k): _json_safe(v, _depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v, _depth + 1) for v in value]
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", errors="replace")[:PROBE_MAX_STRING_CHARS]
        except Exception:
            return str(value)[:PROBE_MAX_STRING_CHARS]
    try:
        json.dumps(value)
        return value
    except Exception:
        return _clean_text(value, PROBE_MAX_STRING_CHARS)


def _cap_array(items: Any, limit: int = PROBE_MAX_ARRAY_ITEMS) -> Any:
    if isinstance(items, (list, tuple)):
        return list(items)[:limit]
    return items


def _shrink_value(key: str, value: Any) -> Any:
    value = _json_safe(value)
    if key in ("returned_ids", "candidate_ids", "restored_ids", "channel",
               "candidate_channels"):
        value = _cap_array(value)
        if isinstance(value, list):
            return [x[:PROBE_MAX_STRING_CHARS] if isinstance(x, str) else x
                    for x in value]
        return value
    if key in ("dense_scores", "keyword_scores", "fts_scores",
               "candidate_page_fault", "selected"):
        return _cap_array(value)
    if key == "candidates":
        value = _cap_array(value)
        if isinstance(value, list):
            out = []
            for item in value:
                if isinstance(item, dict):
                    allowed = ("id", "channel", "keyword_score", "fts_score",
                               "dense_score", "page_fault", "handle")
                    out.append({k: _shrink_value({
                        "id": "returned_ids"}.get(k, k), item.get(k))
                                for k in allowed if k in item})
                else:
                    out.append(_json_safe(item))
            return out
        return value
    return value


def _normalise_event(entry: Dict[str, Any]) -> Dict[str, Any]:
    """只提取白名单字段; 强制 query 只以 hash/len 形式出现。"""
    raw = entry if isinstance(entry, dict) else {}
    event: Dict[str, Any] = {}
    for key in _EVENT_FIELDS:
        if key in raw:
            event[key] = _shrink_value(key, raw.get(key))
    # query 明文一律丢弃, 只允许调用方传入的 query_sha256/query_len。
    event.pop("query", None)
    event.pop("content", None)
    event.setdefault("ts", _now_iso())
    if not event.get("source"):
        event["source"] = "server_recall"
    if "error" in event:
        event["error"] = _clean_text(event.get("error"))
    event["query_sha256"] = _clean_text(event.get("query_sha256"), 128)
    event["query_len"] = max(0, _safe_int(event.get("query_len"), 0))
    event["top_k"] = _safe_int(event.get("top_k"), 0)
    event["candidate_count"] = _safe_int(event.get("candidate_count"), 0)
    event["restore"] = max(0, _safe_int(event.get("restore"), 0))
    event["page_fault"] = bool(event.get("page_fault"))
    event["latency_ms"] = round(max(0.0, _safe_float(
        event.get("latency_ms"), 0.0)), 3)
    return event


def _needs_truncation(raw: Dict[str, Any]) -> bool:
    """判断原始输入是否触发了字段级上限 (用于落盘可见化)。"""
    for key in _ARRAY_FIELDS:
        value = raw.get(key)
        if isinstance(value, (list, tuple)):
            if len(value) > PROBE_MAX_ARRAY_ITEMS:
                return True
            for item in value:
                if isinstance(item, str) and len(item) > PROBE_MAX_STRING_CHARS:
                    return True
        elif isinstance(value, str) and len(value) > PROBE_MAX_STRING_CHARS:
            return True
    for key in ("error", "source", "query_sha256"):
        value = raw.get(key)
        if isinstance(value, str) and len(value) > 512:
            return True
    return False


def _fit_event(entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """规范化 + 逐级截断到单条 64KB; 无法容纳时返回 None。"""
    raw = entry if isinstance(entry, dict) else {}
    event = _normalise_event(entry)
    if _needs_truncation(raw):
        event["truncated"] = True
    for _ in range(6):
        try:
            blob = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        except Exception:
            event = {"source": "server_recall", "error": "probe_build_error"}
            break
        if len(blob.encode("utf-8")) <= PROBE_MAX_EVENT_BYTES:
            return event
        # 仍超限: 先缩短数组元素/长文本, 再退化为纯标量。
        event["truncated"] = True
        for key in _ARRAY_FIELDS:
            if key in event:
                arr = _cap_array(event.get(key), max(1, PROBE_MAX_ARRAY_ITEMS // 4))
                if isinstance(arr, list):
                    arr = [
                        (x[:1024] if isinstance(x, str) else
                         (str(x)[:1024] if not isinstance(x, (int, float, bool)) else x))
                        for x in arr]
                event[key] = arr
        for key in ("error", "source", "query_sha256"):
            if key in event and isinstance(event.get(key), str):
                event[key] = event[key][:512]
    return None


def _append_line(path: Path, line: str) -> None:
    """追加一行; 数据路径与 <path>.lock 同受守卫, open 均带 O_NOFOLLOW。

    调用前须先过 ``_reject_probe_path(path)``; lock 路径与 activity/lock 的
    samefile / inode 命中同样 fail-silent, 绝不通过 lock 路径创建 activity。
    """
    if _reject_probe_path(path):
        raise ProbePathError(f"probe path rejected: {path}")
    lock_path = Path(str(path) + ".lock")
    lock_fd = _open_lock_handle(lock_path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if _reject_probe_path(path) or _reject_probe_path(lock_path):
            raise ProbePathError("probe path changed after lock acquisition")
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        # IMP-1: 数据 fd 一律非阻塞, 防御“最后一道路径守卫之后目标被换成
        # FIFO/设备”。FIFO 无 reader 时 O_WRONLY|O_NONBLOCK 立即 ENXIO,
        # 由 record_recall_probe 的 fail-silent 外层计入 errors, 绝不阻塞。
        if hasattr(os, "O_NONBLOCK"):
            flags |= os.O_NONBLOCK
        fd = os.open(str(path), flags, 0o600)
        try:
            if _fd_hits_blocked(fd):
                raise ProbePathError(f"probe fd rejected: {path}")
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            raise
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=".memorycore-recall-probe-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _compact(path: Path) -> int:
    """滚动截断: 只留近 45 天且总量 ≤ 80% 上限 (与 activity 同口径)。

    R2/S3: 最新一行本身超 budget 时只丢该行并继续保留更旧历史;
    禁止出现“tail 为空 → 原子写把整文件清空”的静默清空。
    返回本次实际删除的行数 (``dropped`` 计数来源)。
    """
    lock_path = Path(str(path) + ".lock")
    if _reject_probe_path(path) or _reject_probe_path(lock_path):
        raise ProbePathError("probe path/lock rejected during compact")
    lock_fd = _open_lock_handle(lock_path)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if _reject_probe_path(path) or _reject_probe_path(lock_path):
            raise ProbePathError("probe path/lock changed during compact")
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return 0
        cutoff = datetime.now(timezone.utc) - timedelta(days=PROBE_RETENTION_DAYS)
        kept: List[str] = []
        for ln in reversed(lines):
            try:
                ts_raw = (json.loads(ln) or {}).get("ts")
                if not ts_raw:
                    continue
                ts = datetime.fromisoformat(str(ts_raw).replace("Z", "+00:00"))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    kept.append(ln)
            except Exception:
                continue
        kept.reverse()
        budget = int(PROBE_MAX_BYTES * 0.8)
        used = 0
        tail: List[str] = []
        for ln in reversed(kept):
            nbytes = len(ln.encode("utf-8")) + 1
            # IMP-2: 单行本身 > budget 时必须丢该行并继续向更老扫描, 不能因为
            # tail 已有更新行就 break(否则可容纳的更老历史会被一并清理)。
            if nbytes > budget:
                continue
            if used + nbytes > budget:
                break
            used += nbytes
            tail.append(ln)
        tail.reverse()
        if not tail and kept:
            # 全部单行都超 budget: 保留最新一条, 绝不把文件清空。
            tail = [kept[-1]]
        dropped = len(lines) - len(tail)
        content = "\n".join(tail) + ("\n" if tail else "")
        _atomic_write_text(path, content)
        return dropped
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        except OSError:
            pass
        lock_fd.close()


def record_recall_probe(entry: Dict[str, Any]) -> None:
    """记录一次召回观测; 永远不抛 (fail-silent)。

    开关关 = 直接返回, 不建文件/不建目录; 开关开 = 追加独立 JSONL,
    超过容量阈值再滚动截断 (可用 ``MEMORYCORE_RECALL_PROBE_COMPACT=0`` 禁用)。
    任何写失败/路径护栏命中只累计 ``errors``。

    S3 metrics 口径:
      * ``written`` = 本次真正 append 成功的事件数 (不再 append 后回读判定);
      * ``dropped`` = 本次滚动实际删掉的行数 (滚动删旧行是设计内, 不计 errors);
      * 最新一行自身超过滚动预算时不 append、保留历史并累计 ``errors``。
    """
    line: Optional[str] = None
    try:
        if not _enabled():
            return
        _METRICS["attempts"] += 1
        path = _probe_file()
        if _reject_probe_path(path):
            raise ProbePathError(f"probe path rejected: {path}")
        event = _fit_event(entry)
        if event is None:
            # 截断后仍超过单条上限: 丢弃该行, 但不能丢历史/清文件。
            raise ValueError("probe event exceeds size limit")
        line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
        line_bytes = len(line.encode("utf-8"))
        if line_bytes > PROBE_MAX_EVENT_BYTES:
            raise ValueError("probe event exceeds size limit")
        # IMP-3 防御性分支: 出厂常量有
        # PROBE_MAX_EVENT_BYTES(64KiB) <= PROBE_ROLLING_BUDGET_BYTES(209715),
        # 且 line_bytes 已被 _fit_event/上方检查钳到 <= PROBE_MAX_EVENT_BYTES,
        # 故此分支在出厂常量下恒不可达; 仅当 PROBE_MAX_BYTES 被显式改小
        # (或 PROBE_MAX_EVENT_BYTES 被改大) 时才可达。
        if _compact_enabled() and line_bytes > int(PROBE_MAX_BYTES * 0.8):
            # 最新一行自身超过滚动预算: 不落盘, 保留历史, errors += 1。
            raise ValueError("probe event exceeds rolling budget")
        _append_line(path, line)
        # append 成功即真正新增一条事件; 后续滚动删的是更老历史, 不影响 written。
        _METRICS["written"] += 1
        if _compact_enabled():
            try:
                if path.stat().st_size > PROBE_MAX_BYTES:
                    _METRICS["dropped"] += _compact(path)
            except OSError:
                pass
    except Exception:
        _METRICS["errors"] += 1
        return


def query_sha256(query: str) -> str:
    """稳定 query 摘要 (探针与评估共用, 不落明文)。"""
    return hashlib.sha256((query or "").encode("utf-8")).hexdigest()
