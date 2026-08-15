# MemoryCore AML 参赛镜像 — python + memorycore + ollama (qwen3 embedding) + aml_server
#
# 构建:  docker build -t memorycore-aml .
# 运行:  docker run -p 8000:8000 -v aml-data:/data memorycore-aml
# 冒烟:  curl http://localhost:8000/health
#         curl -X POST http://localhost:8000/add -H "Content-Type: application/json" -d '{...}'
#         curl -X POST http://localhost:8000/search -H "Content-Type: application/json" -d '{...}'
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

# 系统依赖 + ollama (官方安装脚本, 容器内以 `ollama serve` 手动启动)
# zstd 必须装: ollama 安装脚本用 zstd 解压发行包
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates zstd \
    && curl -fsSL https://ollama.com/install.sh | sh \
    && rm -rf /var/lib/apt/lists/*

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
