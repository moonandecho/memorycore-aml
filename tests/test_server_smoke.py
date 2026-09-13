"""tests/test_server_smoke.py — 启动冒烟测试 (真实 spawn + 官方 mcp 客户端握手)。

堵住 "117 个单测全绿但服务器跑不起来" 的缺口:
历史教训: server.py 曾带第三方 fastmcp 的 show_banner 参数, import 已换成
官方 mcp 后启动即 TypeError, 但测试只 import 模块、从不启动服务器, 启动
路径零覆盖, 全部绿着漏过。

本测试不 mock、不只做 import 检查:
  真实 spawn server 子进程 (stdio transport) → initialize →
  list_tools → 断言 7 个工具名集合 → 调只读工具 → 干净退出。
"""
import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

EXPECTED_TOOLS = {
    "memorycore_" + "store_fact",
    "memorycore_trigger_overflow",
    "memorycore_run_cold_storage_maintenance",
    "memorycore_get_memory_usage",
    "memorycore_memory_audit",
    "memorycore_get_rule_weight",
    "memorycore_recall",
    "memorycore_set_entry_type",  # v2 (2026-09-12): 人工标注 type/protect override
}


def test_server_starts_and_handshakes():
    """真实 spawn server → initialize → list_tools → 只读工具 → 干净退出。"""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def _main() -> None:
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "memorycore.server"],
            cwd=str(REPO_ROOT),
            env=os.environ.copy())
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                assert init.server_info.name == "memorycore", \
                    f"unexpected server name: {init.server_info.name}"
                tools = await session.list_tools()
                names = {t.name for t in tools.tools}
                assert names == EXPECTED_TOOLS, \
                    f"tool set mismatch: {sorted(names)}"
                # 只读工具: 验证 tools 不是空壳, 真实调用路径可用
                res = await session.call_tool("memorycore_get_memory_usage", {})
                assert not res.is_error, "read-only tool call failed"
                assert any(c.text for c in res.content), "empty tool result"

    asyncio.run(_main())
