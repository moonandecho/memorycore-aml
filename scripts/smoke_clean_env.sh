#!/usr/bin/env bash
# smoke_clean_env.sh — 证明参赛系统不依赖任何 Agent 框架（含 Hermes）：
# 在"最小环境"（env -i + 仅必要变量）与空 HOME 下启动服务，跑通 Add / Search / Health，
# 并断言 HOME 未被写入、日志无 hermes 字样。
#
# 用法:
#   bash scripts/smoke_clean_env.sh            # 用默认端口 8899
#   PORT=8898 bash scripts/smoke_clean_env.sh
#
# 依赖: 本机 ollama 提供 embedding（MNEMOSYNE_EMBEDDING_MODEL 指定的模型需已就绪）。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="${PY:-$REPO/.venv/bin/python}"
PORT="${PORT:-8899}"
KEY="${KEY:-cleanenv}"
MODEL="${MNEMOSYNE_EMBEDDING_MODEL:-qwen3-embedding:0.6b}"
EMB_URL="${MNEMOSYNE_EMBEDDING_API_URL:-http://127.0.0.1:11434/v1}"
WORK="$(mktemp -d /tmp/aml-cleanenv.XXXXXX)"
HOME_DIR="$WORK/home"; DATA_DIR="$WORK/data"
mkdir -p "$HOME_DIR" "$DATA_DIR"
LOG="$WORK/service.log"

cleanup() { [ -n "${PID:-}" ] && kill "$PID" 2>/dev/null || true; }
trap cleanup EXIT

echo "== 最小环境启动（env -i，空 HOME）=="
env -i HOME="$HOME_DIR" PATH=/usr/bin:/bin TMPDIR=/tmp \
    MNEMOSYNE_DATA_DIR="$DATA_DIR" AML_HOST=127.0.0.1 AML_PORT="$PORT" AML_API_KEY="$KEY" \
    MNEMOSYNE_EMBEDDING_MODEL="$MODEL" MEMORYCORE_EMBED_MODEL="$MODEL" \
    MNEMOSYNE_EMBEDDING_API_URL="$EMB_URL" MEMORYCORE_EMBED_URL="$EMB_URL" \
    "$PY" -m memorycore.aml_server >"$LOG" 2>&1 &
PID=$!

for _ in $(seq 1 90); do
  code=$(curl -s -o /dev/null -w '%{http_code}' -m 3 "http://127.0.0.1:$PORT/health" || true)
  [ "$code" = "200" ] && break
  sleep 1
done
[ "${code:-}" = "200" ] || { echo "FAIL: /health 未就绪"; tail -20 "$LOG"; exit 1; }
echo "  /health 200"

echo "== Add / Search 全链路 =="
curl -s -m 120 -X POST "http://127.0.0.1:$PORT/add" -H 'content-type: application/json' -H "X-Api-Key: $KEY" \
  -d '{"request_id":"cleanenv:1","user_id":"cleanenv:u","session_id":"s","messages":[{"role":"user","content":"Clean environment check: the vault code is TANGERINE-7."}]}' \
  | grep -q '"success":true' || { echo "FAIL: Add 未成功"; exit 1; }
echo "  /add 200 success=true"

curl -s -m 60 -X POST "http://127.0.0.1:$PORT/search" -H 'content-type: application/json' -H "X-Api-Key: $KEY" \
  -d '{"query":"vault code","user_id":"cleanenv:u","top_k":3}' | grep -q 'TANGERINE' \
  || { echo "FAIL: Search 未命中刚写入的记忆"; exit 1; }
echo "  /search 命中"

echo "== 断言：空 HOME 未被写入 / 日志无 hermes =="
[ "$(ls -A "$HOME_DIR" | wc -l)" = "0" ] || { echo "FAIL: HOME 被写入"; ls -la "$HOME_DIR"; exit 1; }
grep -qi hermes "$LOG" && { echo "FAIL: 日志出现 hermes"; exit 1; }
echo "  HOME 零写入、日志无 hermes"

echo "PASS: 干净环境全链路通过（不依赖任何 Agent 框架）"
