#!/usr/bin/env python3
"""local_store.py — MemoryCore 读写 Hermes 本地热层 MEMORY.md/USER.md

必须与 Hermes MemoryStore (tools/memory_tool.py) 逐字节兼容:
- 条目分隔符 ENTRY_DELIMITER = "\n§\n"
- UTF-8 无 BOM
- 原子写 (os.replace) + fcntl.flock 跨进程锁
- 语义对齐: add 重复检测 / replace 整条替换 / remove 按子串定位
否则 Hermes 的 drift guard 会检测到外部写入并拒绝后续操作。
"""
import fcntl
import os
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ENTRY_DELIMITER = "\n§\n"
MEMORY_DIR = Path(os.path.expanduser("~/.hermes/memories"))
MEMORY_FILE = MEMORY_DIR / "MEMORY.md"
USER_FILE = MEMORY_DIR / "USER.md"


def normalize_for_compare(content: str) -> str:
    """内容归一化 (仅用于比较, 写入仍存原始内容) — 2026-08-28 可复用原语。

    规则:
      1. 全角字符 → 半角: U+3000 (全角空格) → 空格; U+FF01-U+FF5E → 减 0xFEE0
         (覆盖 ，！？：；（）～及全角字母数字); 显式映射表覆盖 CJK 标点块
         (。、—…「」『』【】) 与排版引号 (“”‘’)
      2. 连续空白折叠为单空格 (正则空白+ → 单空格)
      3. 删除紧跟标点后的空格 (\", xxx\" 与 \"，xxx\" 变体等价 — 半角逗号输入
         习惯性带空格, 全角不带, 不处理则仍能绕过去重)
      4. strip

    供 local_store.add 去重 / prefetch 热层去重 / stub 查找等场景共用
    (单一真相源)。写入仍存原始内容, 归一化只进比较。
    """
    _EXTRA_MAP = {
        0x3001: ",",    # 、
        0x3002: ".",    # 。
        0x300C: "[", 0x300D: "]",   # 「」
        0x300E: "[", 0x300F: "]",   # 『』
        0x3010: "[", 0x3011: "]",   # 【】
        0x2018: "'", 0x2019: "'",   # ’‘
        0x201C: '"', 0x201D: '"',   # “”
        0x2026: "...",              # …
    }
    out = []
    for ch in (content or ""):
        o = ord(ch)
        if o == 0x3000:
            out.append(" ")
        elif 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))
        else:
            out.append(_EXTRA_MAP.get(o, ch))
    text = re.sub(r"\s+", " ", "".join(out))
    # 步骤 3: 删除紧跟半角标点后的空格 (折叠后至多单空格)
    text = re.sub(r"(?<=[,.;:!?~])\s", "", text)
    return text.strip()


class LocalStore:
    """本地热层读写 (与 Hermes MemoryStore 格式兼容)。"""

    def __init__(self, memory_path: Path = MEMORY_FILE, user_path: Path = USER_FILE):
        self.memory_path = memory_path
        self.user_path = user_path

    # -- 读取 ----------------------------------------------------

    def read_raw(self, target: str) -> Tuple[str, bool]:
        """读文件原始文本。返回 (raw, read_ok)。read_ok=False 表示文件存在但读失败。"""
        path = self._path_for(target)
        if not path.exists():
            return "", True
        try:
            return path.read_text(encoding="utf-8"), True
        except (OSError, IOError, UnicodeDecodeError):
            return "", False

    def entries(self, target: str) -> List[str]:
        """解析文件为条目列表 (§ 分隔, 去空白, 去重)。"""
        raw, ok = self.read_raw(target)
        if not ok or not raw.strip():
            return []
        entries = [e.strip() for e in raw.split(ENTRY_DELIMITER) if e.strip()]
        return list(dict.fromkeys(entries))

    def char_count(self, target: str) -> int:
        """条目总字符数 (与 Hermes len(join(entries)) 口径一致)。"""
        entries = self.entries(target)
        if not entries:
            return 0
        return len(ENTRY_DELIMITER.join(entries))

    def usage_pct(self, target: str, limit: int = 5000) -> int:
        """占用百分比 (chars 口径, 与 memory 工具注入显示一致)。"""
        return min(100, int((self.char_count(target) / limit) * 100)) if limit > 0 else 0

    # -- 写 (原子写 + fcntl 锁, 与 Hermes 兼容) -------------------

    def add(self, target: str, content: str) -> Dict[str, object]:
        """追加一条。重复检测; 超限拒绝 (与 Hermes add 语义一致)。"""
        content = content.strip()
        if not content:
            return {"success": False, "error": "Content cannot be empty."}

        path = self._path_for(target)
        with self._file_lock(path):
            entries = self.entries(target)
            norm = normalize_for_compare(content)
            if any(normalize_for_compare(e) == norm for e in entries):
                return {"success": False, "error": "Entry already exists (no duplicate added)."}
            limit = self._char_limit(target)
            new_total = len(ENTRY_DELIMITER.join(entries + [content]))
            if new_total > limit:
                return {
                    "success": False,
                    "error": f"Memory at {self.char_count(target):,}/{limit:,} chars. "
                             f"Adding this entry ({len(content)} chars) would exceed the limit.",
                }
            entries.append(content)
            self._write_entries(path, entries)
        return {"success": True, "target": target}

    def replace(self, target: str, old_text: str, new_content: str) -> Dict[str, object]:
        """找到含 old_text 子串的条目, 整条替换为 new_content (与 Hermes 语义一致)。"""
        old_text = old_text.strip()
        new_content = new_content.strip()
        if not old_text or not new_content:
            return {"success": False, "error": "old_text and new_content required."}

        path = self._path_for(target)
        with self._file_lock(path):
            entries = self.entries(target)
            idx = self._find_entry_index(entries, old_text)
            if idx is None:
                return {"success": False, "error": f"Entry containing '{old_text[:40]}' not found."}
            limit = self._char_limit(target)
            new_entries = entries[:idx] + [new_content] + entries[idx + 1:]
            new_total = len(ENTRY_DELIMITER.join(new_entries))
            if new_total > limit:
                return {"success": False, "error": f"Replacement would exceed limit {limit:,} chars."}
            self._write_entries(path, new_entries)
        return {"success": True, "target": target}

    def remove(self, target: str, old_text: str) -> Dict[str, object]:
        """找到含 old_text 子串的条目, 删除该条 (与 Hermes remove 语义一致)。"""
        old_text = old_text.strip()
        if not old_text:
            return {"success": False, "error": "old_text required."}

        path = self._path_for(target)
        with self._file_lock(path):
            entries = self.entries(target)
            idx = self._find_entry_index(entries, old_text)
            if idx is None:
                return {"success": False, "error": f"Entry containing '{old_text[:40]}' not found."}
            removed = entries.pop(idx)
            self._write_entries(path, entries)
        return {"success": True, "removed": removed[:60], "target": target}

    def remove_by_exact(self, target: str, content: str) -> Dict[str, object]:
        """按完整内容精确删除 (用于溢流后清理已迁移条目)。"""
        path = self._path_for(target)
        with self._file_lock(path):
            entries = self.entries(target)
            if content not in entries:
                return {"success": False, "error": "Exact entry not found."}
            entries.remove(content)
            self._write_entries(path, entries)
        return {"success": True, "target": target}

    # -- 内部 ----------------------------------------------------

    def _path_for(self, target: str) -> Path:
        return self.user_path if target == "user" else self.memory_path

    def _char_limit(self, target: str) -> int:
        return 5000  # config memory_char_limit / user_char_limit (均 5000)

    @staticmethod
    def _find_entry_index(entries: List[str], old_text: str) -> Optional[int]:
        for i, e in enumerate(entries):
            if old_text in e:
                return i
        return None

    def _write_entries(self, path: Path, entries: List[str]) -> None:
        """原子写: 写临时文件 → os.replace (与 Hermes atomic_write_text 同模式)。"""
        content = ENTRY_DELIMITER.join(entries) + ("\n" if entries else "")
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".memorycore-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @staticmethod
    @contextmanager
    def _file_lock(path: Path):
        """跨进程排他锁 — 独立 .lock 文件 (与 Hermes MemoryStore 完全同款)。

        锁文件 (path 同目录 .lock) 从不被 replace, inode 固定, 锁域稳定;
        且与 Hermes 内置 MemoryStore (tools/memory_tool.py _file_lock) 锁的是
        同一文件 → CLI (Hermes) 与 gateway/其他进程 (MemoryCore) 真正互斥。
        注意: 不能锁数据文件本身 — os.replace 换 inode 会打破锁域
        (旧 fd 锁旧 inode, 新 open 拿新 inode, 双进程同时进临界区 → 丢数据,
        Task 3.1 实测 3 进程丢 115/180 条)。
        """
        lock_path = path.with_suffix(path.suffix + ".lock")
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


if __name__ == "__main__":
    store = LocalStore()
    for t in ("memory", "user"):
        print(f"{t}: {len(store.entries(t))} 条, {store.char_count(t)} chars, {store.usage_pct(t)}%")
