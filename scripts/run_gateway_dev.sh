#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
GATEWAY_PORT="${GATEWAY_PORT:-8000}"
QWEN_HOST="${QWEN_HOST:-127.0.0.1}"
QWEN_PORT="${QWEN_PORT:-8091}"
QWEN_MODEL_PATH="${QWEN_MODEL_PATH:-$REPO_ROOT/models/Qwen3-Omni-30B-A3B-Instruct}"

export QWEN_REALTIME_URL="${QWEN_REALTIME_URL:-ws://$QWEN_HOST:$QWEN_PORT/v1/realtime}"
export QWEN_CHAT_URL="${QWEN_CHAT_URL:-http://$QWEN_HOST:$QWEN_PORT/v1/chat/completions}"
export QWEN_HEALTH_URL="${QWEN_HEALTH_URL:-http://$QWEN_HOST:$QWEN_PORT/health}"
export QWEN_MODEL="${QWEN_MODEL:-$QWEN_MODEL_PATH}"

cd "$REPO_ROOT/apps/gateway"
exec uvicorn src.main:app --host 0.0.0.0 --port "$GATEWAY_PORT"
