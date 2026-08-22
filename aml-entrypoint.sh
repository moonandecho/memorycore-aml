#!/bin/sh
# AML 容器入口: 启动容器内 ollama → 等待就绪 → 拉取 embedding 模型 → 前台跑 aml_server
set -e

ollama serve &
OLLAMA_PID=$!

# 等待 ollama API 就绪 (最多 60s)
READY=0
for _ in $(seq 1 60); do
    if curl -fsS http://localhost:11434/api/tags >/dev/null 2>&1; then
        READY=1
        break
    fi
    sleep 1
done
if [ "$READY" != "1" ]; then
    echo "ollama did not become ready in 60s" >&2
    exit 1
fi

# embedding 模型缺失时拉取
if ! ollama list | awk '{print $1}' | grep -q "^${MEMORYCORE_EMBED_MODEL}$"; then
    echo "pulling embedding model: ${MEMORYCORE_EMBED_MODEL}"
    ollama pull "$MEMORYCORE_EMBED_MODEL"
fi

# 前台运行 AML 服务 (容器主进程)
exec python -m memorycore.aml_server
