#!/usr/bin/env python3
"""core/llm_check.py — LLM 通路自检 (零网络默认档 / --live 通路验证档)

用法 (发布版, 仓库根目录下):
  python -m memorycore.llm_check            # 零网络: 解析 + 脱敏来源 + 三态结论
  python -m memorycore.llm_check --live     # 追加通路验证 (见计费提示)
  python -m memorycore.llm_check --json     # JSON 输出 (供脚本消费)
  python -m memorycore.llm_check --live --json

--live 两级验证 (评审 D2):
  1) 先 GET {base_url}/models 带 Authorization — 零 token 成本验证 auth+连通
     (DeepSeek/MiMo 均支持该端点);
  2) 失败再回退一次 max_tokens=1 的 completion (成本 ~1e-5 元级, 可忽略)。

计费提示: --live 的 completion 回退会产生一次微小 token 费用; GET /models
不计费。请勿无谓反复跑 --live。

输出语义 (评审必改项 5): "已解析" 仅代表配置解析成功; 只有 --live 验证通过
才许出现 "通路已验证"。默认档零网络, 永不外呼。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Optional

from .core import llm_config


def _resolve_report() -> dict:
    """零网络解析: 配置 + 脱敏来源 + 三态结论。"""
    cfg = llm_config.resolve(force=True)
    if not cfg.enabled:
        status = "disabled"
    elif not cfg.key:
        status = "unconfigured"
    else:
        status = "resolved"  # 仅解析成功, 不代表通路可用
    return {
        "status": status,
        "enabled": cfg.enabled,
        "configured": cfg.configured,
        "source": cfg.source,
        "tried": cfg.tried,
        "model": cfg.model,
        "base_url": cfg.base_url,
        "masked_key": cfg.masked_key,
        "file_sources": llm_config.file_sources_enabled(),
    }


def _live_check(cfg: llm_config.LLMConfig) -> dict:
    """--live 两级验证 (评审 D2)。"""
    import urllib.error
    import urllib.request

    # 1) GET /models: 零 token 成本验证 auth + 连通
    try:
        req = urllib.request.Request(
            cfg.base_url.rstrip("/") + "/models",
            headers={"Authorization": f"Bearer {cfg.key}"},
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return {"live": "ok", "method": "GET /models", "http": 200}
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return {"live": "failed", "method": "GET /models",
                    "category": "auth_failed", "detail": f"HTTP {e.code}"}
        # 其它 HTTP (如 404) → 落到 completion 回退
    except Exception as e:
        return {"live": "failed", "method": "GET /models",
                "category": "network_unreachable",
                "detail": type(e).__name__}

    # 2) completion max_tokens=1 回退 (成本 ~1e-5 元级)
    try:
        data = llm_config.chat(cfg, {
            "model": cfg.model,
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 1,
        }, timeout=10)
        if data.get("choices"):
            return {"live": "ok", "method": "completion max_tokens=1", "http": 200}
        return {"live": "failed", "method": "completion max_tokens=1",
                "category": "bad_response", "detail": "no choices"}
    except llm_config.LLMError as e:
        return {"live": "failed", "method": "completion max_tokens=1",
                "category": e.category, "detail": e.detail}


def _fmt(report: dict) -> str:
    lines = [
        "MemoryCore LLM 通路自检" + (" (--live)" if "live" in report else " (zero-network)"),
        f"  总开关: {'开启' if report['enabled'] else '关闭 (MEMCORE_LLM_ENABLED=0)'}",
        f"  文件来源: {'开启' if report['file_sources'] else '关闭 (MEMCORE_LLM_FILE_SOURCES)'}",
    ]
    if report["status"] == "disabled":
        lines.append("  结论: 总开关关闭 — 全部 LLM 步骤跳过")
        return "\n".join(lines)
    if report["status"] == "unconfigured":
        lines.append(f"  配置: 未配置（已尝试: {report['tried']}）")
        lines.append("  结论: 未配置 — 压缩/下沉确认/合并全部跳过 (静默降级已修复: 此状态可见)")
        return "\n".join(lines)
    lines += [
        "  配置: 已解析",
        f"  来源: {report['source']}  (脱敏, 无 key)",
        f"  模型: {report['model']}",
        f"  base_url: {report['base_url']}",
        f"  密钥: {report['masked_key']} (脱敏)",
        "  结论: 已解析 — 仅代表配置解析成功; 通路是否可用请加 --live 验证",
    ]
    if "live" in report:
        live = report["live"]
        if live["live"] == "ok":
            lines.append(f"  通路验证: 通过 ({live['method']}, HTTP {live.get('http', 200)})")
            lines.append("  最终结论: 通路已验证 ✓")
        else:
            lines.append(f"  通路验证: 失败 ({live['category']}: {live.get('detail', '')})")
            lines.append(f"  最终结论: 通路不可用 ✗ (类别: {live['category']}; "
                         "检查 key/base_url/网络)")
    return "\n".join(lines)


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="llm_check",
        description="MemoryCore LLM 通路自检: 默认零网络只做解析+脱敏来源+三态结论。",
        epilog="计费提示: --live 的 completion 回退会产生一次 max_tokens=1 的"
               "微小 token 费用 (~1e-5 元级); GET /models 不计费。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--live", action="store_true",
                        help="追加通路验证: 先 GET /models (零 token 成本), "
                             "失败再回退一次 max_tokens=1 completion")
    parser.add_argument("--json", action="store_true",
                        help="JSON 输出 (供脚本消费)")
    args = parser.parse_args(argv)

    report = _resolve_report()
    if args.live and report["configured"]:
        report["live"] = _live_check(llm_config.resolve(force=True))

    if args.json:
        print(json.dumps(report, ensure_ascii=False))
    else:
        print(_fmt(report))
    # 退出码: 已解析=0; 未配置/开关关=1; live 失败=2 (脚本可判)
    if report.get("live", {}).get("live") == "failed":
        return 2
    if not report["configured"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
