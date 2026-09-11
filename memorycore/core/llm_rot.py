#!/usr/bin/env python3
"""core/llm_rot.py — LLM 候选轮转游标 (终审低危 D2 长尾饿死修复)

实测背景 (2026-09-12, /tmp/l2_starvation_experiment.py 临时热层/临时冷层):
  每轮 LLM 上限 (默认 8) 截断的候选无持久标记; 下一轮候选列表按稳定顺序
  (文件序 / 枚举序) 重建 → 头部候选若持续"成功但不可消解" (压缩校验不过 /
  去重判 not_duplicate), 每轮都从同一头部消耗预算, 尾部候选永久饿死
  (实测: 10 候选 × 2 轮, 尾部 2 条零次尝试 — 修复前)。

机制:
  每个命名空间维护一个单调递增的轮转偏移 (持久化), 候选列表按
  `offset % len` 轮转后处理 → 任一候选在 ceil(N/C) 轮内至少被尝试一次,
  与头部是否消解无关。读/写失败 → 偏移 0 (降级为修复前行为, 不阻塞治理)。

  命名空间: "overflow:memory" / "overflow:user" (热层主循环),
  "cold:dedup" (冷层 Step 2b 模糊去重组),
  "cold:reversal" (冷层 Step 4b 模糊反转对)。

  状态文件: 与 trash.json 同级 (发布版路径经 MEMORYCORE_LLM_ROT_PATH
  覆盖); 单文件 JSON 原子写 + fcntl 排他锁 (与 trash_store 同款),
  防 weekly 与 MCP 工具并发丢失更新。
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, List

log = logging.getLogger("memorycore.llm_rot")

ROT_PATH = Path(
    os.environ.get(
        "MEMORYCORE_LLM_ROT_PATH",
        os.path.expanduser("~/.memorycore/llm_rot.json"),
    )
)


def _load() -> Dict[str, Any]:
    try:
        if not ROT_PATH.exists():
            return {}
        raw = ROT_PATH.read_text(encoding="utf-8")
        if not raw.strip():
            return {}
        return json.loads(raw)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("LLMROT: 轮转状态读取失败 (%s) → 本轮偏移 0 (同修复前)", e)
        return {}


def _save(data: Dict[str, Any]) -> None:
    try:
        ROT_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(ROT_PATH.parent), prefix=".llmrot-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=1)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, ROT_PATH)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except OSError as e:
        log.warning("LLMROT: 轮转状态写失败 (%s) → 本轮不推进偏移 (同修复前)", e)


@contextmanager
def _file_lock():
    """跨进程/跨线程排他锁 (与 trash_store 同款 fcntl.flock)。

    load→modify→save 的读改写序列在此锁内串行化, 防 weekly 与 MCP 工具
    并发 rotate 丢失偏移更新。锁文件从不被 os.replace 换 inode (锁域稳定)。
    """
    lock_path = ROT_PATH.with_suffix(ROT_PATH.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "a+", encoding="utf-8")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except (OSError, IOError):
            pass
        fd.close()


def rotate(namespace: str, items: List) -> List:
    """按命名空间轮转候选列表, 返回新列表并推进持久偏移。

    n <= 1: 恒等返回, 不推进偏移 (轮转无意义)。
    读/写失败: 偏移 0 / 不推进, 行为同修复前 (轮转是公平性增强,
    失败降级不影响治理正确性)。
    """
    n = len(items)
    if n <= 1:
        return list(items)
    with _file_lock():
        data = _load()
        try:
            off = int(data.get(namespace, 0)) % n
        except (TypeError, ValueError):
            off = 0
        data[namespace] = (off + 1) % n
        _save(data)
        return items[off:] + items[:off]
