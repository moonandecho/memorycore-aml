#!/usr/bin/env python3
"""core/metadata.py — 热层条目元数据 (sidecar JSON) + 直写通道治理核心

Phase 2 (2026-08-16): 给热层条目挂 type(rule/state) + written_at 元数据,
sidecar 存储 (不碰 § 分隔的 .md 格式, Hermes MemoryStore 零影响),
溢流按"年龄+类型"确定性退役, 替代纯关键词判定 (根治词表两周一复发)。

职责:
1. MetaStore — sidecar 读写 (原子写 + flock), sha256 键控,
   reconcile (legacy 补盖 / 孤儿 GC; 已有键不重判)
2. entry_age_days — 按类型取年龄 (state 用 written_at, rule 用 updated_at)
3. direct_write_govern — Hermes 直写通道治理核心 (prefetch 插件
   on_memory_write 回调复用; 治理逻辑不复制)

安全模型:
- sidecar 写失败 / 损坏 → 退化为 legacy 处理 (关键词兜底), 不阻塞溢流;
- 元数据只影响退役精度, 从不删 .md 条目本身 (删除只发生在冷层写成功的溢流路径);
- state 误判 rule → 条目滞留, 30 天后仅压缩; rule 误判 state → 进冷层可召回。
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("memorycore.metadata")

from .classifier import (classify_entry_type, should_keep_local,
                         judge_engine_enabled)
from .judge import judge_entry
from .config import (MEMORY_FILE, USER_FILE, META_SUFFIX,
                     ACTIVITY_LOG_RETENTION_DAYS,
                     ACTIVITY_LOG_MAX_BYTES, ACTIVITY_LOG_FILE,
                     ACTIVITY_WINDOW_DAYS, STUB_PREFIX,
                     WEIGHT_INIT)
# E8 (2026-09-12): ACTIVITY_LOG_ENABLED 由模块尾部 __getattr__ 惰性委托
# core.config (运行中改 env 即时生效; monkeypatch.setattr 覆盖仍有效)。
from . import config as _cfg

_EMBEDDED_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")
_VALID_ENTRY_TYPES = ("rule", "state", "stub")


def _safe_entry_type(content: str, entry_type: Any) -> str:
    """P2 (FIX8): stamp 入口类型安全默认。

    非法/None type 绝不能落成 `type:null` 使条目永久脱离候选池:
      - stub 前缀优先 → "stub";
      - 否则复用 classify_entry_type(content) 的公开安全映射 (ambiguous→rule);
      - 判型异常/返回非法值 → 兜底 "rule" (rule 参与统一候选池, 可退役)。
    """
    if entry_type in _VALID_ENTRY_TYPES:
        return entry_type
    if isinstance(content, str) and content.lstrip().startswith(STUB_PREFIX):
        return "stub"
    try:
        _t = classify_entry_type(content)
        if _t in _VALID_ENTRY_TYPES:
            return _t
    except Exception:
        pass
    return "rule"


def _judge_meta_fields(jr) -> Dict[str, Any]:
    """JudgeResult → sidecar 可落盘字段 (短摘要, 不回放原文)。"""
    sig = jr.signals if isinstance(jr.signals, dict) else {}
    # signals 只存短审计摘要, 总长软上限 ≤512 字节
    try:
        raw = json.dumps(sig, ensure_ascii=False)
        if len(raw.encode("utf-8")) > 512:
            sig = {k: v for k, v in list(sig.items())[:8]}
            sig["_truncated"] = True
            raw = json.dumps(sig, ensure_ascii=False)
            if len(raw.encode("utf-8")) > 512:
                sig = {"_truncated": True}
    except (TypeError, ValueError):
        sig = {"_unserializable": True}
    return {
        "judge_decision": jr.decision,
        "judge_band": jr.band,
        "judge_confidence": jr.confidence,
        "judge_signals": sig,
        "judge_reason": (jr.reason or "")[:160],
        "judge_policy": "v3",
    }


def _judge_hold_kwargs(jr, now: Optional[datetime] = None) -> Dict[str, Any]:
    """ambiguous 的 hold 元数据 (review_at +7d)。非 ambiguous 不补 hold 键。"""
    kw = _judge_meta_fields(jr)
    if jr.decision == "ambiguous":
        days = int(getattr(_cfg, "JUDGE_AMBIGUOUS_REVIEW_DAYS", 7))
        kw["judge_review_at"] = (now or datetime.now(timezone.utc)) \
            + timedelta(days=days)
        kw["judge_review_count"] = 0
        kw["judge_reviewed_at"] = None
        kw["judge_resolution"] = None
    return kw



# ---- 时间工具 --------------------------------------------------------------

# FIX7 I5: stamp() 哨兵 — 区分"未传 = 保留"与"显式 None = 清空".
_UNSET = object()


def _iso(dt: datetime) -> str:
    """datetime → ISO8601 (UTC, +00:00)。"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(ts: str) -> Optional[datetime]:
    """ISO8601 → aware datetime; 解析失败返回 None。"""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


# ---- FIX6 R2: 唯一时间锚点归一化入口 ----------------------------------------
# 所有"元数据时间戳 → 判据锚点"必须经过这里; 不在此之后重复 parse/比较.
_TS_ANOMALY_LOCK = threading.Lock()
_TS_ANOMALY_TOTAL = 0
_TS_ANOMALY_RECENT: "deque[Dict[str, Any]]" = deque(maxlen=16)


def _record_ts_anomaly(reason: str, field: str, sha: str, raw: Any,
                       now: datetime) -> None:
    """记录一条时间戳异常 (进程内可见审计; 不抛异常, 不影响主路径)。"""
    global _TS_ANOMALY_TOTAL
    sha8 = (sha or "")[:8]
    rec = {
        "at": _iso(now),
        "reason": reason,
        "field": field,
        "sha8": sha8,
        "raw": str(raw)[:64],
    }
    with _TS_ANOMALY_LOCK:
        _TS_ANOMALY_TOTAL += 1
        _TS_ANOMALY_RECENT.append(rec)
    log.warning("TS_ANOMALY %s field=%s sha=%s raw=%s now=%s -> 不授予可信锚点",
                reason, field, sha8 or "-", str(raw)[:64], _iso(now))


def _ts_anchor_status(ts: Any, now: Optional[datetime] = None, *,
                      field: str = "ts", sha: str = "",
                      sink: Optional[Dict[str, Any]] = None,
                      allow_future: bool = False
                      ) -> Tuple[Optional[datetime], bool]:
    """_ts_anchor 的实现体, 额外返回 `anomalous` 标志。

    普通判据用 _ts_anchor 拿到"夹到 now"后的锚点; 软驻留的定性选择需要
    知道该锚点是否来自未来异常 (不可信), 因此通过本入口读取标志, 避免
    第二个时间解析/比较实现。
    """
    if ts is None:
        return None, False
    if isinstance(ts, str):
        if not ts.strip():
            return None, False
        raw = ts.strip()
    else:
        raw = ts
    dt = _parse_iso(str(raw))
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    if dt is None:
        if sha or isinstance(raw, str):
            _record_ts_anomaly("unparsable", field, sha, raw, now)
            if isinstance(sink, dict):
                sink["ts_anomaly"] = int(sink.get("ts_anomaly", 0)) + 1
        return None, True
    tol_s = float(getattr(_cfg, "TS_ANOMALY_TOLERANCE_SECONDS", 300))
    if allow_future:
        # 计划性未来时间戳 (如 judge_review_at 复审期限) 是设计值, 不属
        # 于损坏锚点; 只校验可解析, 不夹到 now, 不计 ts_anomaly.
        return dt, False
    if dt > now + timedelta(seconds=tol_s):
        _record_ts_anomaly("future", field, sha, raw, now)
        if isinstance(sink, dict):
            sink["ts_anomaly"] = int(sink.get("ts_anomaly", 0)) + 1
        return now, True
    if dt > now:
        return now, False
    return dt, False


def _ts_anchor(ts: Any, now: Optional[datetime] = None, *,
               field: str = "ts", sha: str = "",
               sink: Optional[Dict[str, Any]] = None,
               allow_future: bool = False) -> Optional[datetime]:
    """唯一时间锚点归一化入口 (R2, 2026-09-13)。

    规则:
      - None / 空 / 解析失败 → None (调用方回退保守语义);
      - 解析失败也计入 ts_anomaly 全局审计 (可见, 不静默);
      - ts > now + TS_ANOMALY_TOLERANCE_SECONDS (默认 300s) → 计入
        ts_anomaly + log.warning (含条目 sha 前 8 位) + 夹到 now;
      - ts 在 (now, now+tolerance] 容差带内 → 按 now 处理, 不计异常;
      - 其余返回原 aware UTC 时间。
    """
    return _ts_anchor_status(ts, now, field=field, sha=sha, sink=sink,
                             allow_future=allow_future)[0]


def _ts_anomaly_snapshot() -> Dict[str, Any]:
    """返回进程内时间戳异常审计快照 (只读, 供 memory_usage / rule_weight)。"""
    with _TS_ANOMALY_LOCK:
        return {"count": _TS_ANOMALY_TOTAL,
                "recent": [dict(x) for x in _TS_ANOMALY_RECENT]}


def _reset_ts_anomaly() -> None:
    """测试隔离用: 清空进程内时间戳异常审计。"""
    global _TS_ANOMALY_TOTAL
    with _TS_ANOMALY_LOCK:
        _TS_ANOMALY_TOTAL = 0
        _TS_ANOMALY_RECENT.clear()


def parse_embedded_date(content: str) -> Optional[datetime]:
    """条目内嵌日期 (第一个 20xx-xx-xx) → UTC 当日零点; 无/解析失败 → None。

    legacy 迁移的 written_at 来源 (近似, 与真实写入时刻最多差一天,
    对 7 天 TTL 可忽略)。
    """
    m = _EMBEDDED_DATE_RE.search(content or "")
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group(0), "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def entry_age_days(meta: Dict[str, Any],
                   now: Optional[datetime] = None,
                   entry: str = "") -> Optional[int]:
    """按类型取条目年龄 (天): state 用 written_at, rule 用 updated_at。

    R2 (FIX6): 所有时间戳必须走 _ts_anchor 统一归一化 — 未来/解析失败
    不再各自 return 0/None, 未来时间戳会被夹到 now 并计入 ts_anomaly。
    rule 的 updated_at 是"最后写入/编辑时间" — 热层每轮全量注入,
    真实引用不可观测, 以"未更新时长"作失效代理 (⚠️ 假设, 设计 §5 已标注)。
    """
    etype = meta.get("type")
    field = "written_at" if etype == "state" else "updated_at"
    ts = meta.get(field) or meta.get("written_at")
    now = now or datetime.now(timezone.utc)
    anchor = _ts_anchor(ts, now, field=field,
                        sha=hashlib.sha256((entry or "").encode()).hexdigest()
                        if entry else "")
    if anchor is None:
        return None
    return max((now - anchor).days, 0)


# ---- MetaStore -------------------------------------------------------------

class MetaStore:
    """热层 sidecar 元数据存储 (每 target 一个 json 文件)。

    键 = sha256(条目内容.strip()); 值 = {type, written_at, updated_at, origin}。
    条目内容变化 → 键变化 → 旧键成孤儿 (reconcile GC), 新内容按新条目判型。
    与 .md 文件同目录 (MEMORY.meta.json / USER.meta.json), 独立 .lock 文件,
    原子写 (tempfile + os.replace), 跨进程 flock 互斥 (gateway/CLI 双实例)。
    """

    def __init__(self, target: str,
                 memory_path: Path = MEMORY_FILE,
                 user_path: Path = USER_FILE):
        self.target = target
        data_path = user_path if target == "user" else memory_path
        # MEMORY.md -> MEMORY.meta.json (设计 §3.1)
        self.meta_path = data_path.with_suffix(META_SUFFIX)
        self.lock_path = Path(str(self.meta_path) + ".lock")

    # -- 读 (无锁, os.replace 原子性保证读到完整旧/新版本) -------------

    def get_entry(self, content: str) -> Optional[Dict[str, Any]]:
        """取单条元数据 (副本); 无/文件损坏 → None。"""
        data = self._load_unlocked()
        return dict(data.get(self._hash(content), {}) or {}) or None

    def _load_unlocked(self) -> Dict[str, Any]:
        try:
            with open(self.meta_path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {}

    # -- 写 (锁内 load-modify-save, 防双进程丢更新) ---------------------

    def stamp(self, content: str, entry_type: str,
              written_at: Any = _UNSET,
              updated_at: Any = _UNSET,
              origin: Any = _UNSET,
              importance: Any = _UNSET,
              weight: Any = _UNSET,
              last_active_at: Any = _UNSET,
              last_scan_at: Any = _UNSET,
              cold_id: Any = _UNSET,
              type_override: Any = _UNSET,
              type_source: Any = _UNSET,
              protect_override: Any = _UNSET,
              protected: Any = _UNSET,
              last_strong_hit_at: Any = _UNSET,
              last_weak_hit_at: Any = _UNSET,
              schema: Any = _UNSET,
              judge_decision: Any = _UNSET,
              judge_band: Any = _UNSET,
              judge_confidence: Any = _UNSET,
              judge_signals: Any = _UNSET,
              judge_reason: Any = _UNSET,
              judge_review_at: Any = _UNSET,
              judge_review_count: Any = _UNSET,
              judge_reviewed_at: Any = _UNSET,
              judge_resolution: Any = _UNSET,
              judge_resolved_at: Any = _UNSET,
              judge_policy: Any = _UNSET,
              last_recall_hit_at: Any = _UNSET,
              last_injected_at: Any = _UNSET,
              last_evicted_at: Any = _UNSET,
              writeback_count: Any = _UNSET,
              retire_count: Any = _UNSET,
              handle: Any = _UNSET,
              # FIX8 B2: 仅保留为 legacy optional sidecar 键 (I5 三态兼容);
              # 不参与任何候选排序/让位/结构计数, 生产选择器不再读取.
              grace_defer_count: Any = _UNSET,
              reconcile_anchor_fallback: Any = _UNSET) -> Dict[str, Any]:
        """盖章/更新一条元数据 (FIX6 R1 + FIX7 哨兵语义)。

        字段语义 (FIX7 I5):
          - 未传 (默认 `_UNSET`) = **保留**既有键; 新键按核心默认补齐;
          - 显式传 None = **清空**该可选键 (不再被旧值粘住);
          - 显式非 None = 覆盖。

        出生时刻粘性 (FIX6 R1 + FIX7 I3):
          - 显式传非 None `written_at` → 以显式值为准 (重置出生时刻的入口);
          - 既有键未显式传 non-None → 原样继承; 旧值缺失时保持缺失,
            **不会静默续 now**, 因此不会重获新鲜窗口乘法;
          - 新键未显式传 non-None → now。

        出生重置联动 (FIX7 I4): 显式传非 None `written_at` 且未显式传
        `reconcile_anchor_fallback` 时, 清除旧的 True — "显式重置出生时刻"
        不能再被旧 fallback 标记永久阻止新鲜乘法。需要保留"换形仍不信出生"
        语义的调用方 (`_meta_to_stamp_kwargs` / stub-sink) 应显式传 True。

        updated_at 是本次盖章动作时间, 未传/显式 None 时刷新 now (与旧实现
        一致); 压缩路径必须显式传原 updated_at 才保持旧年龄。

        I5 三态例外 (FIX8 P5 文档口径, 避免调用方误用):
          - `written_at`: 新 key 未传/None → now; 既有 key 且有非空值 →
            未传/None 均保留 (出生粘性); 既有 key 但旧值缺失 → 保持缺失,
            不续 now。只有显式非 None 才算出生重置并清 reconcile 兜底标记。
          - `updated_at`: 未传/显式 None 都刷新 now (盖章动作时间语义);
            要保旧年龄必须显式传原值。
          - `judge_reviewed_at` / `judge_resolution`: `judge_decision ==
            "ambiguous"` 时显式 None 保留 null (审计要求); 非 ambiguous
            时显式 None 清键。
          - 其余可选键: 未传保留 / 显式 None 清空 / 非 None 覆盖。
          - `grace_defer_count` (FIX8 B2): 仅作为 legacy optional 键保留
            I5 三态；没有任何生产选择/计数逻辑读取它。
        P2 (FIX8): `entry_type` 不是 rule/state/stub 时走 `_safe_entry_type`
        安全默认, 永不写 type:null。
        """
        # P2 (FIX8): type 入口校验 —— 非法/None 走安全默认, 不写 type:null.
        entry_type = _safe_entry_type(content, entry_type)
        now = datetime.now(timezone.utc)
        h = self._hash(content)
        with self._lock():
            data = self._load_unlocked()
            _old_raw = data.get(h)
            existed = h in data and isinstance(_old_raw, dict)
            old = _old_raw if isinstance(_old_raw, dict) else {}
            meta: Dict[str, Any] = dict(old)

            # ---- 核心字段: 显式覆盖 / 保留旧值 / 新键默认 ----
            meta["type"] = entry_type
            _explicit_birth = (written_at is not _UNSET
                               and written_at is not None)
            if _explicit_birth:
                meta["written_at"] = _iso(written_at)
            elif not existed:
                # 新键 (旧兼容): 未传/显式 None 都视为"写入时刻 = now"。
                meta["written_at"] = _iso(now)
            else:
                # FIX7 I3: 既有键且旧出生缺失/None → 保持缺失, 不续 now。
                if not meta.get("written_at"):
                    meta.pop("written_at", None)
            # updated_at 是"本次盖章动作时间": 未传/显式 None 均刷新 now。
            if updated_at is _UNSET or updated_at is None:
                meta["updated_at"] = _iso(now)
            else:
                meta["updated_at"] = _iso(updated_at)
            if origin is not _UNSET:
                if origin is None:
                    meta.pop("origin", None)
                else:
                    meta["origin"] = origin
            elif not meta.get("origin"):
                meta["origin"] = "hermes"
            if importance is not _UNSET:
                if importance is None:
                    meta.pop("importance", None)
                else:
                    meta["importance"] = importance
            elif meta.get("importance") is None:
                meta["importance"] = 0.8
            if weight is not _UNSET:
                if weight is None:
                    meta.pop("weight", None)
                else:
                    meta["weight"] = float(weight)
            elif meta.get("weight") is None:
                meta["weight"] = WEIGHT_INIT
            if last_active_at is not _UNSET:
                if last_active_at is None:
                    meta.pop("last_active_at", None)
                else:
                    meta["last_active_at"] = _iso(last_active_at)
            elif not meta.get("last_active_at"):
                # FIX6 R2: 旧出生锚点的读取也必须走统一时间入口。
                _birth = _ts_anchor(meta.get("written_at"), now,
                                    field="written_at", sha=h)
                _fallback = (_birth if entry_type != "rule" and _birth
                             else now)
                meta["last_active_at"] = _iso(_fallback)

            # ---- 可选时间戳: _UNSET 保留, None 清空, 其他覆盖 ----
            for _k, _v in (("last_scan_at", last_scan_at),
                           ("last_strong_hit_at", last_strong_hit_at),
                           ("last_weak_hit_at", last_weak_hit_at),
                           ("last_recall_hit_at", last_recall_hit_at),
                           ("last_injected_at", last_injected_at),
                           ("last_evicted_at", last_evicted_at),
                           ("judge_review_at", judge_review_at),
                           ("judge_resolved_at", judge_resolved_at)):
                if _v is _UNSET:
                    continue
                if _v is None:
                    meta.pop(_k, None)
                else:
                    meta[_k] = _iso(_v)
            # 7.4 审计落盘: ambiguous 路径即使显式传 None 也必须保留
            # `judge_reviewed_at: null`; 非 ambiguous 时 None 才是清键.
            _ambiguous_final = (
                (judge_decision == "ambiguous")
                if judge_decision is not _UNSET
                else meta.get("judge_decision") == "ambiguous")
            if judge_reviewed_at is not _UNSET:
                if judge_reviewed_at is None:
                    if _ambiguous_final:
                        meta["judge_reviewed_at"] = None
                    else:
                        meta.pop("judge_reviewed_at", None)
                else:
                    meta["judge_reviewed_at"] = _iso(judge_reviewed_at)
            elif _ambiguous_final and "judge_reviewed_at" not in meta:
                meta["judge_reviewed_at"] = None

            # ---- 可选标量/对象: _UNSET 保留, None 清空, 其他覆盖 ----
            for _k, _v in (("cold_id", cold_id),
                           ("type_override", type_override),
                           ("type_source", type_source),
                           ("protect_override", protect_override),
                           ("handle", handle),
                           ("judge_decision", judge_decision),
                           ("judge_band", judge_band)):
                if _v is _UNSET:
                    continue
                if _v is None:
                    meta.pop(_k, None)
                else:
                    meta[_k] = _v
            if protected is not _UNSET:
                if protected is None:
                    meta.pop("protected", None)
                else:
                    meta["protected"] = bool(protected)
            if schema is not _UNSET:
                if schema is None:
                    meta.pop("schema", None)
                else:
                    meta["schema"] = int(schema)
            if judge_confidence is not _UNSET:
                if judge_confidence is None:
                    meta.pop("judge_confidence", None)
                else:
                    meta["judge_confidence"] = judge_confidence
            if judge_signals is not _UNSET:
                if judge_signals is None:
                    meta.pop("judge_signals", None)
                else:
                    meta["judge_signals"] = judge_signals
            if judge_reason is not _UNSET:
                if judge_reason is None:
                    meta.pop("judge_reason", None)
                else:
                    meta["judge_reason"] = judge_reason
            if judge_policy is not _UNSET:
                if judge_policy is None:
                    meta.pop("judge_policy", None)
                else:
                    meta["judge_policy"] = judge_policy
            if judge_review_count is not _UNSET:
                if judge_review_count is None:
                    meta.pop("judge_review_count", None)
                else:
                    meta["judge_review_count"] = int(judge_review_count)
            if writeback_count is not _UNSET:
                if writeback_count is None:
                    meta.pop("writeback_count", None)
                else:
                    meta["writeback_count"] = int(writeback_count)
            if retire_count is not _UNSET:
                if retire_count is None:
                    meta.pop("retire_count", None)
                else:
                    meta["retire_count"] = int(retire_count)
            # FIX8 B2 legacy 兼容: 字段本身不承载机制, 仅保留 I5 未传/None/
            # 非 None 三态; 选择器与 enforcement 对读取/写入全部删除.
            if grace_defer_count is not _UNSET:
                if grace_defer_count is None:
                    meta.pop("grace_defer_count", None)
                else:
                    meta["grace_defer_count"] = int(grace_defer_count)
            # ---- judge_resolution 的 null 语义: ambiguous 保留 null,
            #      非 ambiguous 显式 None 清键 (FIX7 I7 改型出口依赖). ----
            if judge_resolution is not _UNSET:
                if judge_resolution is None:
                    if meta.get("judge_decision") == "ambiguous":
                        meta["judge_resolution"] = None
                    else:
                        meta.pop("judge_resolution", None)
                else:
                    meta["judge_resolution"] = judge_resolution
            elif meta.get("judge_decision") == "ambiguous" \
                    and "judge_resolution" not in meta:
                meta["judge_resolution"] = None
            if meta.get("judge_decision") == "ambiguous":
                meta.setdefault("judge_policy", "v3")

            # ---- FIX7 I4: 显式出生重置清除旧 reconcile 兜底标记 ----
            if reconcile_anchor_fallback is not _UNSET:
                if reconcile_anchor_fallback is None:
                    meta.pop("reconcile_anchor_fallback", None)
                else:
                    meta["reconcile_anchor_fallback"] = bool(
                        reconcile_anchor_fallback)
            elif _explicit_birth:
                # 未显式传标记而显式重置出生 → 旧 True 不得粘住。
                meta.pop("reconcile_anchor_fallback", None)

            data[h] = meta
            self._save_unlocked(data)
        return dict(meta)

    def update_fields(self, content: str, **fields: Any) -> Optional[Dict[str, Any]]:
        """局部更新已有 sidecar 键 (周治理 ambiguous 终审/人工标注用)。

        条目键不存在 → 返回 None (由 reconcile 补盖, 不在此伪造判型)。
        None 值会删除该键 (用于清 judge_review_at)。
        """
        h = self._hash(content)
        with self._lock():
            data = self._load_unlocked()
            if h not in data:
                return None
            meta = data[h]
            for k, v in fields.items():
                if v is None:
                    meta.pop(k, None)
                else:
                    meta[k] = v
            data[h] = meta
            self._save_unlocked(data)
            return dict(meta)

    def reconcile(self, entries: List[str],
                  now: Optional[datetime] = None) -> Dict[str, int]:
        """幂等 reconcile: legacy 补盖 + 孤儿 GC (已有键不重判, 不重新判型)。

        对每条现有条目: 无元数据 → v2 判型补盖 (written_at=内嵌日期优先, 否则 now;
        type_source="lexical_v2"); **已有键不重判** (保留人工/LLM/迁移标注,
        历史误判由 tools/retype_20260912.py 一次性迁移纠正, 见 DESIGN §Q5);
        sidecar 中条目已不存在的键 → GC。只写 sidecar, 永不改 .md。
        返回 {"stamped": n, "gc": n}。
        """
        now = now or datetime.now(timezone.utc)
        with self._lock():
            data = self._load_unlocked()
            current: Dict[str, bool] = {}
            stamped = 0
            for e in entries:
                h = self._hash(e)
                current[h] = True
                if h not in data:
                    # Phase 3 S4: stub 指针前缀识别 (盖章失败时 reconcile 兜底,
                    # 防 stub 被 classify_entry_type 误判为 rule 参与放弃路径)
                    _embedded = parse_embedded_date(e)
                    if _embedded is not None:
                        # P3 (FIX8): 内嵌日期统一走 _ts_anchor(field=
                        # "embedded_date") 再写入; 未来日期夹 now 且
                        # ts_anomaly 可见, 不再写成 2099 制造永久新鲜窗口.
                        _embedded = _ts_anchor(_embedded, now,
                                               field="embedded_date", sha=h)
                    anchor = _embedded or now
                    # FIX6 R1/评审次要#4: 无内嵌日期的 legacy 补章不能
                    # 伪装成新写入; 标记 reconcile 兜底, 新鲜乘数不授予.
                    _reconcile_fallback = _embedded is None
                    if e.startswith(STUB_PREFIX):
                        entry_meta = {
                            "type": "stub",
                            "written_at": _iso(anchor),
                            "updated_at": _iso(anchor),
                            "origin": "legacy",
                            "type_source": "lexical_v2",
                            "reconcile_anchor_fallback": _reconcile_fallback,
                            "weight": WEIGHT_INIT,
                            "last_active_at": _iso(anchor),
                            "last_scan_at": _iso(anchor),
                        }
                    elif judge_engine_enabled():
                        # SAFE-JUDGE v3: 新条目才判型; ambiguous 写 type=rule +
                        # judge_decision=ambiguous + review_at, 永不冷迁。
                        jr = judge_entry(e)
                        entry_meta = {
                            "type": jr.public_type,
                            "written_at": _iso(anchor),
                            "updated_at": _iso(anchor),
                            "origin": "legacy",
                            "type_source": ("judge_v3_ambiguous"
                                            if jr.decision == "ambiguous"
                                            else "judge_v3"),
                            "reconcile_anchor_fallback": _reconcile_fallback,
                            "weight": WEIGHT_INIT,
                            "last_active_at": _iso(anchor),
                            "last_scan_at": _iso(anchor),
                        }
                        _hold = _judge_hold_kwargs(jr, now=now)
                        if isinstance(_hold.get("judge_review_at"), datetime):
                            _hold["judge_review_at"] = _iso(
                                _hold["judge_review_at"])
                        entry_meta.update(_hold)
                    else:
                        entry_meta = {
                            "type": classify_entry_type(e),
                            "written_at": _iso(anchor),
                            "updated_at": _iso(anchor),
                            "origin": "legacy",
                            "type_source": "lexical_v2",
                            "reconcile_anchor_fallback": _reconcile_fallback,
                            "weight": WEIGHT_INIT,
                            "last_active_at": _iso(anchor),
                            "last_scan_at": _iso(anchor),
                        }
                    data[h] = entry_meta
                    stamped += 1
            gc = 0
            for h in list(data.keys()):
                if h not in current:
                    del data[h]
                    gc += 1
            self._save_unlocked(data)
        return {"stamped": stamped, "gc": gc}

    # -- 内部 ---------------------------------------------------------

    @staticmethod
    def _hash(content: str) -> str:
        return hashlib.sha256((content or "").strip().encode("utf-8")).hexdigest()

    def _save_unlocked(self, data: Dict[str, Any]) -> None:
        """原子写: 临时文件 + os.replace (与 LocalStore 同模式)。"""
        self.meta_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.meta_path.parent),
                                   prefix=".memorycore-meta-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.meta_path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def _lock(self):
        """跨进程排他锁 (lock 文件 inode 固定, 与 .md 锁同模式)。"""
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = open(self.lock_path, "a+", encoding="utf-8")
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _LockGuard(fd)


class _LockGuard:
    """flock 上下文管理器 (释放后关闭 fd)。"""

    def __init__(self, fd):
        self._fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        except OSError:
            pass
        self._fd.close()


# ---- 直写通道治理核心 ------------------------------------------------------

def direct_write_govern(store, client, target: str, content: str,
                        action: str = "add",
                        type_hint: Optional[str] = None) -> Dict[str, Any]:
    """Hermes 直写通道治理 (on_memory_write 回调复用, 设计 §3.6)。

    Hermes 内置 memory 工具 add/replace 提交成功后调用 (条目已在热层):
      - rule 型 → sidecar 盖章 {rule, written_at=now, origin=hermes}, 条目留热层;
      - state 型 → 立即迁移冷层: recall 查重 → same 跳过写直接删热层 /
        similar merge-update 后删热层 / 无匹配 remember 后删热层;
        任一冷层失败 → 保留热层 + 盖章 {state, written_at=now} 作 7 天到期兜底
        (由溢流正常路径退役)。任何情况不丢数据。
      - remove → no-op (回调只拿子串, 孤儿键由下次 reconcile GC)。

    type_hint (v2, 2026-09-12): 人工标注透传 classify_entry_type
    (type_override 优先于词法, 不传时行为与旧版一致)。

    store/client: LocalStore / MnemosyneClient (调用方构造, 测试可注入 mock)。
    """
    content = (content or "").strip()
    if not content:
        return {"status": "skip_empty"}
    if action not in ("add", "replace"):
        return {"status": "skip_action", "action": action}

    metastore = MetaStore(target, memory_path=store.memory_path,
                          user_path=store.user_path)
    _hint = type_hint if type_hint in ("state", "rule") else None
    _use_v3 = judge_engine_enabled()
    _jr = None
    _jkw: Dict[str, Any] = {}
    if _use_v3:
        # SAFE-JUDGE v3: 判型一次成型; 不再 classify + should_keep_local 双判,
        # ambiguous 只盖章留热层, 绝不 recall/remember。
        _jr = judge_entry(content, type_hint=_hint)
        etype = _jr.public_type
        _jkw = _judge_hold_kwargs(_jr)
    else:
        etype = classify_entry_type(content, type_hint=_hint)
        # B-1 (2026-09-12 评审修复, 仅 legacy 回滚路径): 判型 state → 立即
        # 冷迁前加 should_keep_local 二次否决。
        if etype == "state" and _hint is None and should_keep_local(content):
            etype = "rule"
    # Phase 3: stub 指针前缀优先 (恢复/编辑的指针保持 stub 型, 不参与放弃路径)
    if content.startswith(STUB_PREFIX):
        etype = "stub"
        _jr = None
        _jkw = {}

    # §6.2 审计: 新判型路径必须落 type_source=judge_v3* (人工标注优先).
    _type_source = "lexical_v2"
    if _hint is not None:
        _type_source = "manual_override"
    elif _use_v3 and _jr is not None:
        _type_source = ("judge_v3_ambiguous"
                        if _jr.decision == "ambiguous" else "judge_v3")

    if _jr is not None and _jr.decision == "ambiguous":
        # B-2/B-3 安全默认: public_type 已是 rule; 留热层 + review_at, 零冷层调用。
        metastore.stamp(content, "rule", origin="hermes",
                        type_source=_type_source, **_jkw)
        return {"status": "held_ambiguous",
                "judge_decision": "ambiguous",
                "judge_review_at": (_jkw.get("judge_review_at").isoformat()
                                    if _jkw.get("judge_review_at") else None),
                "detail": "判型模糊: 留热层, 周治理异步终审"}

    if etype == "rule":
        metastore.stamp(content, "rule", origin="hermes",
                        type_source=_type_source, **_jkw)
        # Phase 4: 直写通道写后预算检查 (Hermes 内置 memory 工具 add/replace;
        # 惰性导入防环 — overflow.py 顶层导入本模块)。后台线程调用时自动异步。
        try:
            from .overflow import enforce_rule_budget
            enforce_rule_budget(store, client, target, metastore, {})
        except Exception:
            pass  # 预算挤权失败不阻塞直写 (下轮溢流兜底)
        return {"status": "stamped_rule"}
    if etype == "stub":
        metastore.stamp(content, "stub", origin="hermes",
                        type_source=_type_source)
        return {"status": "stamped_stub"}

    # state: 冷迁移 (与 overflow _handle_cold_migration 同安全语义)
    # 惰性导入防环: overflow.py 顶层导入本模块, 这里运行时再取共享原语。
    from .overflow import _recall_safe, _find_best_match, _merge_two_entries

    try:
        existing = _recall_safe(client, content)
    except Exception:
        metastore.stamp(content, "state", origin="hermes",
                        type_source=_type_source, **_jkw)
        return {"status": "kept_hot_backstop", "reason": "cold_unreachable"}

    if existing:
        matched = _find_best_match(content, existing)
        if matched:
            if matched["level"] == "same":
                # 冷层已有相同事实 → 不重复写, 直接删热层
                if store.remove_by_exact(target, content).get("success"):
                    return {"status": "migrated_same"}
                metastore.stamp(content, "state", origin="hermes",
                                type_source=_type_source, **_jkw)
                return {"status": "kept_hot_backstop",
                        "reason": "local_remove_failed"}
            merged = _merge_two_entries(content, matched["content"])
            try:
                r = client.update(matched["id"], merged)
                if r.get("status") == "updated":
                    if store.remove_by_exact(target, content).get("success"):
                        return {"status": "migrated_merged"}
                    metastore.stamp(content, "state", origin="hermes",
                                    type_source=_type_source, **_jkw)
                    return {"status": "kept_hot_backstop",
                            "reason": "local_remove_failed"}
            except Exception:
                pass  # update 失败 → 降级 remember (与 overflow 同语义)

    try:
        r = client.remember(content, importance=0.6, scope="global")
        if r.get("status") == "stored":
            if store.remove_by_exact(target, content).get("success"):
                return {"status": "migrated_new"}
            metastore.stamp(content, "state", origin="hermes",
                            type_source=_type_source, **_jkw)
            return {"status": "kept_hot_backstop",
                    "reason": "local_remove_failed"}
    except Exception:
        pass

    metastore.stamp(content, "state", origin="hermes",
                    type_source=_type_source, **_jkw)
    return {"status": "kept_hot_backstop", "reason": "cold_write_failed"}


# ---- Phase 3 S4: 主题活性日志 (prefetch/recall 查询采集, 设计 §4.4) --------

def log_activity_query(query: str) -> None:
    """追加一条查询到活动日志 ({ts, query}, 截前 200 字); 任何失败静默忽略。

    采集面: memorycore-prefetch 每轮 prefetch(query) + memorycore_recall(query)。
    隐私: 仅本地 Mac, 45 天滚动 / 256KB 上限, ACTIVITY_LOG_ENABLED=0 关闭
    (关闭时 S4 stub-sink 整体禁用, 机制降级为现状)。
    """
    if not _cfg.ACTIVITY_LOG_ENABLED:
        return
    q = (query or "").strip()
    if not q:
        return
    try:
        ACTIVITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"ts": _iso(datetime.now(timezone.utc)),
                           "query": q[:200]}, ensure_ascii=False)
        # 追加与滚动压缩共用同一把 flock: os.replace 换 inode, 裸 append
        # 会写进被替换掉的旧文件 → 丢行 (低概率, 但一把锁即可根治)
        lock_path = Path(str(ACTIVITY_LOG_FILE) + ".lock")
        fd = open(lock_path, "a+", encoding="utf-8")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with open(ACTIVITY_LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            fd.close()
    except Exception:
        return
    try:
        if os.path.getsize(ACTIVITY_LOG_FILE) > ACTIVITY_LOG_MAX_BYTES:
            _compact_activity_log()
    except Exception:
        pass


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写文本 (临时文件 + os.replace, 与 LocalStore 同模式)。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent),
                               prefix=".memorycore-act-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _compact_activity_log() -> None:
    """滚动压缩: 只留近 RETENTION 天且总量 ≤ 80% 上限 (flock 防双进程竞争)。"""
    lock_path = Path(str(ACTIVITY_LOG_FILE) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        try:
            with open(ACTIVITY_LOG_FILE, encoding="utf-8") as f:
                lines = f.read().splitlines()
        except OSError:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(
            days=ACTIVITY_LOG_RETENTION_DAYS)
        kept = []
        for ln in reversed(lines):
            try:
                ts = _ts_anchor(json.loads(ln).get("ts"),
                                 field="activity_ts")
            except Exception:
                continue
            if ts is not None and ts >= cutoff:
                kept.append(ln)
        kept.reverse()
        budget = int(ACTIVITY_LOG_MAX_BYTES * 0.8)
        tail = []
        used = 0
        for ln in reversed(kept):
            used += len(ln.encode("utf-8")) + 1
            if used > budget:
                break
            tail.append(ln)
        tail.reverse()
        _atomic_write_text(ACTIVITY_LOG_FILE,
                           "\n".join(tail) + ("\n" if tail else ""))
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        fd.close()


def load_recent_queries(days: Optional[int] = None) -> List[str]:
    """近 N 天活动查询文本 (S4 休眠判定输入)。

    日志缺失/损坏/无窗口内样本 → [] (调用方按"全活跃"保守处理, stub 不执行)。
    """
    return [q for _, q in load_recent_queries_with_ts(days)]


def load_recent_queries_with_ts(days: Optional[int] = None) -> List[tuple]:
    """近 N 天活动查询 (ts, query) 元组列表 — Phase 4 命中扫描增量输入。

    与 load_recent_queries 同解析逻辑, 额外返回 ts (供 last_scan_at 增量过滤)。
    日志缺失/损坏 → []。
    """
    if not _cfg.ACTIVITY_LOG_ENABLED:
        return []
    days = ACTIVITY_WINDOW_DAYS if days is None else days
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: List[tuple] = []
    try:
        with open(ACTIVITY_LOG_FILE, encoding="utf-8") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                    ts = _ts_anchor(d.get("ts"), field="activity_ts")
                    if ts is not None and ts >= cutoff:
                        q = (d.get("query") or "").strip()
                        if q:
                            out.append((ts, q))
                except Exception:
                    continue
    except OSError:
        return []
    return out

# ---- E8: env 开关惰性委托 (2026-09-12) --------------------------------------
def __getattr__(name):
    if name == "ACTIVITY_LOG_ENABLED":
        return getattr(_cfg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
