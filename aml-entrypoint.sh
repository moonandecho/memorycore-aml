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

# embedding 模型缺失时拉取 — 带重试与单次超时:
# 首次启动需联网 (~639MB); 网络异常时不无限卡住, 3 次失败后明确报错退出,
# 交由容器重启策略/平台重试 (也可构建期预置模型完全绕开网络依赖)。
if ! ollama list | awk '{print $1}' | grep -q "^${MEMORYCORE_EMBED_MODEL}$"; then
    echo "pulling embedding model: ${MEMORYCORE_EMBED_MODEL} (network required at first start)"
    PULL_OK=0
    for attempt in 1 2 3; do
        echo "pull attempt ${attempt}/3..."
        if timeout 900 ollama pull "$MEMORYCORE_EMBED_MODEL"; then
            PULL_OK=1
            break
        fi
        echo "pull attempt ${attempt} failed; retrying in 5s..."
        sleep 5
    done
    if [ "$PULL_OK" != "1" ]; then
        echo "ERROR: failed to pull ${MEMORYCORE_EMBED_MODEL} after 3 attempts — check network access (or preload the model into the image)" >&2
        exit 1
    fi
fi

# 前台运行 AML 服务 (容器主进程)
exec python -m memorycore.aml_server
