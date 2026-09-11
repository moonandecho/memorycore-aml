#!/usr/bin/env python3
"""tools/check_llm_sync.py — 三仓 LLM 共享文件一致性校验 (Phase 6 防漂移)

三家仓库 (本地运行版 ~/.hermes/memorycore / 开源版 origin-memorycore /
参赛版 memorycore-aml) 共享以下设计为"同源"的文件; 三家布局不同
(本地 core/、发布 memorycore/core/), 且发布版 FILE_SOURCES_DEFAULT=False
(必改项 3)。本脚本对"仓库特有"的差异做归一化后比哈希:

归一化规则 (仅替换"布局/发行"差异, 不替换实现):
  - llm_config.py: FILE_SOURCES_DEFAULT 行 (True/False = 本地/发布)
  - llm_check.py:  llm_config import 行 + 用法/示例命令行 (布局差异)
  - 测试文件:      import 行 (core / memorycore.core 布局差异)

用法:
  python3 tools/check_llm_sync.py           # 校验当前仓库 (按 manifest)
  python3 tools/check_llm_sync.py --update  # 写入/更新 tools/llm_sync_manifest.json
  python3 tools/check_llm_sync.py --dump    # 打印各文件归一化哈希 (跨仓比对用)
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "tools/llm_sync_manifest.json"

# 逻辑文件 → (候选路径列表, 归一化规则)
FILES = {
    "llm_config.py": {
        "paths": ["core/llm_config.py", "memorycore/core/llm_config.py"],
        "rules": [
            (re.compile(r"^FILE_SOURCES_DEFAULT = (True|False)$", re.M),
             "FILE_SOURCES_DEFAULT = <本地True/发布False>"),
        ],
    },
    "llm_check.py": {
        "paths": ["core/llm_check.py", "memorycore/llm_check.py"],
        "rules": [
            (re.compile(r"^from \. import llm_config$", re.M),
             "from .core import llm_config  (本地=单点, 发布=包内)"),
            (re.compile(r"^from \.core import llm_config$", re.M),
             "from .core import llm_config  (本地=单点, 发布=包内)"),
            # 用法块整体归一 (本地版 shim 示例行数不同)
            (re.compile(r"^用法 \(.*$.*?^--live 两级验证", re.S | re.M),
             "用法 <本地运行版/发布版>, 仓库根目录下):\n--live 两级验证"),
        ],
    },
    "test_llm_config.py": {
        "paths": ["tests/test_llm_config.py"],
        "rules": [
            (re.compile(r"^from (core|memorycore\.core) import (config as config_mod|llm_config).*$", re.M),
             "from <core|memorycore.core> import <...>"),
            (re.compile(r"^    from (core|memorycore) import llm_check$", re.M),
             "    from <core|memorycore> import llm_check"),
        ],
    },
    "test_e_visible.py": {
        "paths": ["tests/test_e_visible.py"],
        "rules": [
            (re.compile(r"^from (core|memorycore\.core) import (config as config_mod|overflow as ov_mod|metadata as meta_mod).*$", re.M),
             "from <core|memorycore.core> import <...>"),
            (re.compile(r"^    from (core|memorycore\.core) import maintenance as maint$", re.M),
             "    from <core|memorycore.core> import maintenance as maint"),
            (re.compile(r"^    from (trash_store|memorycore\.trash_store) import TrashStore, add_observed$", re.M),
             "    from <trash_store|memorycore.trash_store> import TrashStore, add_observed"),
        ],
    },
    "test_weekly_tidy.py": {
        "paths": ["tests/test_weekly_tidy.py"],
        "rules": [
            (re.compile(r"^    from (core|memorycore\.core) import llm_config$", re.M),
             "    from <core|memorycore.core> import llm_config"),
            (re.compile(r"^from (core|memorycore\.core) import metadata as meta_mod$", re.M),
             "from <core|memorycore.core> import metadata as meta_mod"),
            (re.compile(r"^(import weekly_maintenance as wm|from memorycore import weekly_maintenance as wm)$", re.M),
             "import <weekly_maintenance as wm>"),
        ],
    },
}


def normalize(text: str, rules) -> str:
    for pattern, repl in rules:
        text = pattern.sub(repl, text)
    return text


def sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def compute() -> dict:
    out = {}
    for name, spec in FILES.items():
        found = None
        for rel in spec["paths"]:
            p = ROOT / rel
            if p.is_file():
                found = p
                break
        if found is None:
            out[name] = {"sha256": "<文件不存在>", "missing": True}
            continue
        text = found.read_text(encoding="utf-8")
        out[name] = {"sha256": sha256(normalize(text, spec["rules"])),
                     "found_at": str(found.relative_to(ROOT))}
    return out


def main() -> int:
    if "--update" in sys.argv:
        data = {"note": "三仓 LLM 共享文件归一化哈希清单 (归一化规则见 check_llm_sync.py)。"
                        "三家仓库此文件必须完全一致; 不一致 = 同步漂移, 立即修。",
                "files": compute()}
        MANIFEST.parent.mkdir(exist_ok=True)
        MANIFEST.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8")
        print(f"manifest written: {MANIFEST}")
        return 0

    if "--dump" in sys.argv:
        print(json.dumps(compute(), ensure_ascii=False, indent=2))
        return 0

    if not MANIFEST.is_file():
        print(f"[FAIL] {MANIFEST} 不存在 — 先在本仓跑 --update 生成后提交")
        return 1
    expected = json.loads(MANIFEST.read_text(encoding="utf-8")).get("files", {})
    actual = compute()
    rc = 0
    for name in sorted(set(expected) | set(actual)):
        e = expected.get(name, {}).get("sha256")
        a = actual.get(name, {}).get("sha256")
        if e == a:
            print(f"[OK]   {name}: {a[:16]}…")
        else:
            print(f"[FAIL] {name}:\n  manifest={e}\n  actual ={a}")
            rc = 1
    return rc


if __name__ == "__main__":
    sys.exit(main())
