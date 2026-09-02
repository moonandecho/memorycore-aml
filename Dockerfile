# MemoryCore AML 参赛镜像 — python + memorycore + ollama (qwen3-embedding 预置) + aml_server
#
# 构建:  docker build -t memorycore-aml .
# 运行:  docker run -p 8000:8000 -v aml-data:/data memorycore-aml
# 冒烟:  curl http://localhost:8000/health
#         curl -X POST http://localhost:8000/add -H "Content-Type: application/json" -d '{...}'
#         curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d '{...}'
#
# 说明:  构建期预置 qwen3-embedding:0.6b (~639MB) 进镜像, 容器启动零网络依赖;
#        aml-entrypoint.sh 仍保留"模型缺失时联网拉取"的兜底分支。

# ---- 阶段 1: 预取 embedding 模型 (ollama runtime + pull, 产物 COPY 进主镜像) ----
FROM python:3.12-slim AS model-fetcher

ENV DEBIAN_FRONTEND=noninteractive

# 仅需 curl 做 ollama 就绪探测
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 从构建机 COPY ollama (绕开 ollama.com/github 下载, 国内服务器直连不可靠):
#   主二进制 → /usr/local/bin (PATH 内) + CPU 推理库 → /usr/local/lib/ollama
COPY ollama-bin/ollama /usr/local/bin/ollama
COPY ollama-lib/ /usr/local/lib/ollama/

# 构建期拉取 embedding 模型: 启动 ollama → 等待就绪 → pull (3 次重试)。
# 失败即构建失败 (评测平台构建阶段网络通常可用; 若此处失败, 问题暴露在构建期而非评测期)。
RUN (ollama serve &) && \
    READY=0; \
    for _ in $(seq 1 30); do \
        if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then READY=1; break; fi; \
        sleep 1; \
    done; \
    if [ "$READY" != "1" ]; then echo "ERROR: ollama did not become ready" >&2; exit 1; fi; \
    PULL_OK=0; \
    for attempt in 1 2 3; do \
        echo "pulling qwen3-embedding:0.6b (attempt ${attempt}/3)"; \
        if ollama pull qwen3-embedding:0.6b; then PULL_OK=1; break; fi; \
        sleep 5; \
    done; \
    if [ "$PULL_OK" != "1" ]; then echo "ERROR: qwen3-embedding:0.6b pull failed after 3 attempts" >&2; exit 1; fi; \
    pkill ollama || true

# ---- 阶段 2: 主镜像 ----
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ollama runtime → /usr/local (PATH 内) + CPU 推理库
COPY ollama-bin/ollama /usr/local/bin/ollama
COPY ollama-lib/ /usr/local/lib/ollama/

# 预置 embedding 模型 (阶段 1 产物; ollama 默认模型目录 /root/.ollama)
COPY --from=model-fetcher /root/.ollama /root/.ollama

WORKDIR /app
COPY . /app

# 阿里云 pip 镜像 (国内服务器 pypi.org 直连慢/卡; 清华 403 失效)
RUN pip install --no-cache-dir -i https://mirrors.aliyun.com/pypi/simple/ .

# AML 运行环境 (全部可被 -e 覆盖)
# 注意: mnemosyne 模块级变量在 import 时读取, 必须直接设 MNEMOSYNE_* 变量,
#       不能依赖 config.py 的 MEMORYCORE_* 转发 (转发只对显式 import 生效)
ENV MNEMOSYNE_DATA_DIR=/data \
    MEMORYCORE_EMBED_URL=http://localhost:11434/v1 \
    MEMORYCORE_EMBED_MODEL=qwen3-embedding:0.6b \
    MNEMOSYNE_EMBEDDING_API_URL=http://localhost:11434/v1 \
    MNEMOSYNE_EMBEDDING_MODEL=qwen3-embedding:0.6b \
    AML_HOST=0.0.0.0 \
    AML_PORT=8000

VOLUME /data
EXPOSE 8000

COPY aml-entrypoint.sh /usr/local/bin/aml-entrypoint.sh
RUN chmod +x /usr/local/bin/aml-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/aml-entrypoint.sh"]
