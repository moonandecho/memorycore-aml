# AGENTS.md — origin-memorycore

## 项目定位

MemoryCore —— LLM Agent 记忆层治理方案(MIT 开源版)。
- 治理层 = MemoryCore(本仓库), 存储层 = Mnemosyne(远端), 上层 = Hermes/cron。
- 核心机制: 热层/冷层分层、写入路由、语义去重、容量溢流、回收站兜底、双通道召回。

## 开发约定(必须遵守)

1. **git commit 随时提交** — 每完成一个逻辑阶段就 `git add -A && git commit -m "<描述>"`。多次小提交优于一次大提交, 可追溯。禁止攒一堆改动一次性提交。
2. **测试必须真实落盘** — 禁止只跑内联 `python3 -c` 不写文件。测试文件要进 git、可复现、有断言 (`git show <commit> --stat` 必须能看到测试文件)。
3. **测试隔离生产路径** — 模块级 `Path(...)` 默认值在 import 时绑定真实路径, 测试必须用 tempfile 覆盖后再写; 跑完检查生产文件无污染 (如 trash.json 不得出现 mock id)。
4. **中英 README 同步** — 改 README.md 必须同步更新 README.zh-CN.md, 不得只改英文。
5. **隐私红线** — 代码/文档/commit message 中不得出现真实本地路径、域名、IP、API key、token。发布前全仓 grep 清零。
6. **外部依赖必须实测** — 涉及 ollama/embedding/远端服务的代码, 必须实测"依赖不可达"场景 (空缓存 + 断网/端点不可达), 不能只验 import 通过; "返回成功但数据坏了"比直接报错更危险, 要有零向量/空结果守卫。
7. **兼容性声明必须实测** — "import 可过/无回归"要在实际解析出的依赖环境里跑通才算数, 不凭逻辑推断; 裸 `>=` 依赖约束可能拉到破坏性新版本, 需检查。
8. **验证再告知** — 改完必须确认文件已更新/测试已跑/效果已生效, 才能说"完成"。

## 常用命令

- 测试: `python -m pytest tests/ -q`
- 语法检查: `python -m py_compile <file>` 或直接 import 冒烟
- 发布前隐私扫描: 全仓 grep 路径/域名/IP/key/token 模式, 结果为零才算干净

## 工作流

- 修改仓库代码时, 每阶段 commit 后保留在服务器 (不 push), 经审查后再推 GitHub。
- 本文件与核心规则 (Hermes pi-workflow-rules) 冲突时, 以本文件为准 (项目级高于全局)。
