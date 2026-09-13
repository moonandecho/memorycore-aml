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
import sys

from .core.llm_check import main

if __name__ == "__main__":
    sys.exit(main())
