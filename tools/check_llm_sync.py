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

受检范围边界 (终审低危, 2026-09-12): 仅上述 5 个文件做同源哈希校验。
消费者文件 (overflow/maintenance/weekly/server 等) 是"仓库特有"的 — 布局
(core/ vs memorycore.core/)、发行差异 (本地中文注释/发布英文注释、本地邮件
通知节)、后端差异 (MnemosyneClient vs ColdStoreClient) 各有不同, 不做同源
承诺。它们列入下方 UNPROTECTED 清单, 校验输出显式提示"不受同源保护",
漂移防线 = 人工对照 + 三仓本脚本/清单自身 md5 核对。

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

# 不受同源保护的文件 (仓库特有, 允许漂移 — 终审低危修复):
# 本地 weekly 曾出现旧 _load_env (lstrip bug) 而发布版没有, 校验未检出;
# 现显式列出, 校验输出提示人工对照。此清单与 manifest["unprotected"]
# 必须一致 (校验脚本会比对), 漂移防护靠三仓本脚本/清单 md5 一致。
UNPROTECTED = [
    "core/maintenance.py",      # 本地 MnemosyneClient / 发布 ColdStoreClient 后端
    "core/overflow.py",
    "core/llm_judge.py",
    "core/decay.py",
    "core/config.py",
    "core/classifier.py",
    "core/metadata.py",
    "weekly_maintenance.py",    # 本地有邮件通知节; 发布版无 _load_env
    "server.py",
    "trash_store.py",
    "local_store.py",
    "mnemosyne_client.py",     # 仅本地; 发布版对应 cold_store_client.py
    "cold_store_client.py",    # 仅发布版
    "tests/conftest.py",       # 其余测试文件仓库特有
    "tests/",                  # (除受检 3 个测试文件外)
    "README.md", "README.zh-CN.md",
]

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
                        "三家仓库此文件必须完全一致; 不一致 = 同步漂移, 立即修。"
                        "unprotected = 不受同源保护的消费者文件 (仓库特有, 允许漂移, 人工对照)。",
                "files": compute(),
                "unprotected": list(UNPROTECTED)}
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

    # 终审低危: 显式提示不受同源保护的文件 (仓库特有, 允许漂移 — 人工对照)。
    # 清单漂移防护: manifest["unprotected"] 必须与脚本内 UNPROTECTED 一致。
    m_unprotected = json.loads(MANIFEST.read_text(encoding="utf-8")).get(
        "unprotected", [])
    if m_unprotected != UNPROTECTED:
        print("[FAIL] manifest.unprotected 与脚本 UNPROTECTED 不一致 — 重新 --update")
        rc = 1
    print("\n以下文件不受同源保护 (仓库特有, 允许漂移 — 漂移防线为人工对照):")
    for rel in UNPROTECTED:
        print(f"  [UNPROTECTED] {rel}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
