# MemoryCore AML 参赛镜像 — python + memorycore + ollama (qwen3 embedding) + aml_server
#
# 构建:  docker build -t memorycore-aml .
# 运行:  docker run -p 8000:8000 -v aml-data:/data memorycore-aml
# 冒烟:  curl http://localhost:8000/health
#         curl -X POST http://localhost:8000/add -H "Content-Type: application/json" -d '{...}'
#         curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d '{...}'
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# 系统依赖 (zstd 不再需要 — ollama 二进制直接 COPY, 不跑安装脚本)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 从构建机 COPY ollama (绕开 ollama.com/github 下载, 国内服务器直连不可靠):
#   主二进制 + CPU 推理库 (libggml-cpu*.so / libllama / libmtmd), 不含 cuda
COPY ollama-bin/ /usr/local/
COPY ollama-lib/ /usr/local/lib/ollama/

WORKDIR /app
COPY . /app

RUN pip install --no-cache-dir .

# AML 运行环境 (全部可被 -e 覆盖)
ENV MNEMOSYNE_DATA_DIR=/data \
    MEMORYCORE_EMBED_URL=http://localhost:11434/v1 \
    MEMORYCORE_EMBED_MODEL=qwen3-embedding:0.6b \
    AML_HOST=0.0.0.0 \
    AML_PORT=8000

VOLUME /data
EXPOSE 8000

COPY aml-entrypoint.sh /usr/local/bin/aml-entrypoint.sh
RUN chmod +x /usr/local/bin/aml-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/aml-entrypoint.sh"]
