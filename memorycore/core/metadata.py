#!/usr/bin/env python3
"""core/metadata.py — 热层条目元数据 (sidecar JSON) + 直写通道治理核心

Phase 2 (2026-08-16): 给热层条目挂 type(rule/state) + written_at 元数据,
sidecar 存储 (不碰 § 分隔的 .md 格式, Hermes MemoryStore 零影响),
溢流按"年龄+类型"确定性退役, 替代纯关键词判定 (根治词表两周一复发)。

职责:
1. MetaStore — sidecar 读写 (原子写 + flock), sha256 键控,
   reconcile (legacy 补盖 / 孤儿 GC / 内容变更重新判型)
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
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .classifier import classify_entry_type
from .config import MEMORY_FILE, USER_FILE, META_SUFFIX

_EMBEDDED_DATE_RE = re.compile(r"20\d\d-\d\d-\d\d")


# ---- 时间工具 --------------------------------------------------------------

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
                   now: Optional[datetime] = None) -> Optional[int]:
    """按类型取条目年龄 (天): state 用 written_at, rule 用 updated_at。

    rule 的 updated_at 是"最后写入/编辑时间" — 热层每轮全量注入,
    真实引用不可观测, 以"未更新时长"作失效代理 (⚠️ 假设, 设计 §5 已标注)。
    时间戳缺失/解析失败 → None (调用方回退保守处理)。
    """
    etype = meta.get("type")
    field = "written_at" if etype == "state" else "updated_at"
    ts = meta.get(field) or meta.get("written_at")
    if not ts:
        return None
    dt = _parse_iso(str(ts))
    if dt is None:
        return None
    now = now or datetime.now(timezone.utc)
    return max((now - dt).days, 0)  # 负值 (未来日期笔误) 钳制为 0


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
              written_at: Optional[datetime] = None,
              updated_at: Optional[datetime] = None,
              origin: str = "hermes") -> Dict[str, Any]:
        """盖章/覆盖盖章一条元数据 (updated_at 缺省 = now)。返回写入的元数据。

        updated_at 参数供测试回填历史时间 (rule 压缩年龄门) 与特殊场景使用。
        """
        now = datetime.now(timezone.utc)
        meta = {
            "type": entry_type,
            "written_at": _iso(written_at or now),
            "updated_at": _iso(updated_at or now),
            "origin": origin,
        }
        with self._lock():
            data = self._load_unlocked()
            data[self._hash(content)] = meta
            self._save_unlocked(data)
        return meta

    def reconcile(self, entries: List[str],
                  now: Optional[datetime] = None) -> Dict[str, int]:
        """幂等 reconcile: legacy 补盖 + 孤儿 GC + 内容变更重新判型。

        对每条现有条目: 无元数据 → 判型补盖 (written_at=内嵌日期优先, 否则 now);
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
                    etype = classify_entry_type(e)
                    data[h] = {
                        "type": etype,
                        "written_at": _iso(parse_embedded_date(e) or now),
                        "updated_at": _iso(now),
                        "origin": "legacy",
                    }
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
                        action: str = "add") -> Dict[str, Any]:
    """Hermes 直写通道治理 (on_memory_write 回调复用, 设计 §3.6)。

    Hermes 内置 memory 工具 add/replace 提交成功后调用 (条目已在热层):
      - rule 型 → sidecar 盖章 {rule, written_at=now, origin=hermes}, 条目留热层;
      - state 型 → 立即迁移冷层: recall 查重 → same 跳过写直接删热层 /
        similar merge-update 后删热层 / 无匹配 remember 后删热层;
        任一冷层失败 → 保留热层 + 盖章 {state, written_at=now} 作 7 天到期兜底
        (由溢流正常路径退役)。任何情况不丢数据。
      - remove → no-op (回调只拿子串, 孤儿键由下次 reconcile GC)。

    store/client: LocalStore / MnemosyneClient (调用方构造, 测试可注入 mock)。
    """
    content = (content or "").strip()
    if not content:
        return {"status": "skip_empty"}
    if action not in ("add", "replace"):
        return {"status": "skip_action", "action": action}

    metastore = MetaStore(target, memory_path=store.memory_path,
                          user_path=store.user_path)
    etype = classify_entry_type(content)

    if etype == "rule":
        metastore.stamp(content, "rule", origin="hermes")
        return {"status": "stamped_rule"}

    # state: 冷迁移 (与 overflow _handle_cold_migration 同安全语义)
    # 惰性导入防环: overflow.py 顶层导入本模块, 这里运行时再取共享原语。
    from .overflow import _recall_safe, _find_best_match, _merge_two_entries

    try:
        existing = _recall_safe(client, content)
    except Exception:
        metastore.stamp(content, "state", origin="hermes")
        return {"status": "kept_hot_backstop", "reason": "cold_unreachable"}

    if existing:
        matched = _find_best_match(content, existing)
        if matched:
            if matched["level"] == "same":
                # 冷层已有相同事实 → 不重复写, 直接删热层
                if store.remove_by_exact(target, content).get("success"):
                    return {"status": "migrated_same"}
                metastore.stamp(content, "state", origin="hermes")
                return {"status": "kept_hot_backstop",
                        "reason": "local_remove_failed"}
            merged = _merge_two_entries(content, matched["content"])
            try:
                r = client.update(matched["id"], merged)
                if r.get("status") == "updated":
                    if store.remove_by_exact(target, content).get("success"):
                        return {"status": "migrated_merged"}
                    metastore.stamp(content, "state", origin="hermes")
                    return {"status": "kept_hot_backstop",
                            "reason": "local_remove_failed"}
            except Exception:
                pass  # update 失败 → 降级 remember (与 overflow 同语义)

    try:
        r = client.remember(content, importance=0.6, scope="global")
        if r.get("status") == "stored":
            if store.remove_by_exact(target, content).get("success"):
                return {"status": "migrated_new"}
            metastore.stamp(content, "state", origin="hermes")
            return {"status": "kept_hot_backstop",
                    "reason": "local_remove_failed"}
    except Exception:
        pass

    metastore.stamp(content, "state", origin="hermes")
    return {"status": "kept_hot_backstop", "reason": "cold_write_failed"}
