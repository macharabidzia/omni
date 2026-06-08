#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/load_env.sh"

VENV_PATH="${APP_VENV_PATH:-$REPO_ROOT/.venv}"
PYTHON_BIN="$VENV_PATH/bin/python"
UVICORN_BIN="$VENV_PATH/bin/uvicorn"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
QWEN_HOST="${QWEN_HOST:-127.0.0.1}"
QWEN_PORT="${QWEN_PORT:-8091}"
QWEN_MODEL_PATH="${QWEN_MODEL:-${QWEN_MODEL_PATH:-$REPO_ROOT/models/Qwen3-Omni-30B-A3B-Instruct}}"

if [[ ! -x "$UVICORN_BIN" ]]; then
  echo "error: app runtime not found at $UVICORN_BIN" >&2
  echo "hint: run scripts/bootstrap_vast.sh first" >&2
  exit 1
fi

export QWEN_REALTIME_URL="${QWEN_REALTIME_URL:-ws://$QWEN_HOST:$QWEN_PORT/v1/realtime}"
export QWEN_CHAT_URL="${QWEN_CHAT_URL:-http://$QWEN_HOST:$QWEN_PORT/v1/chat/completions}"
export QWEN_HEALTH_URL="${QWEN_HEALTH_URL:-http://$QWEN_HOST:$QWEN_PORT/health}"
export QWEN_MODEL="${QWEN_MODEL:-$QWEN_MODEL_PATH}"
export LIVEKIT_URL="${LIVEKIT_URL:-ws://185.62.58.164:7880}"
export LIVEKIT_API_KEY="${LIVEKIT_API_KEY:-devkey}"
export LIVEKIT_API_SECRET="${LIVEKIT_API_SECRET:-devsecret}"
export LIVEKIT_ROOM="${LIVEKIT_ROOM:-omni-room}"
export LIVEKIT_AGENT_ID="${LIVEKIT_AGENT_ID:-omni-worker}"
export LIVEKIT_BROWSER_IDENTITY_PREFIX="${LIVEKIT_BROWSER_IDENTITY_PREFIX:-browser}"
export LIVEKIT_CONTROL_TOPIC="${LIVEKIT_CONTROL_TOPIC:-omni.control}"
export LIVEKIT_TOKEN_TTL_SECONDS="${LIVEKIT_TOKEN_TTL_SECONDS:-3600}"
export LIVEKIT_INPUT_SAMPLE_RATE="${LIVEKIT_INPUT_SAMPLE_RATE:-16000}"
export LIVEKIT_OUTPUT_SAMPLE_RATE="${LIVEKIT_OUTPUT_SAMPLE_RATE:-48000}"
export LIVEKIT_OUTPUT_FRAME_MS="${LIVEKIT_OUTPUT_FRAME_MS:-20}"
export LIVEKIT_OUTPUT_QUEUE_MS="${LIVEKIT_OUTPUT_QUEUE_MS:-60}"
export LIVEKIT_PREROLL_FRAMES="${LIVEKIT_PREROLL_FRAMES:-8}"

cd "$REPO_ROOT/apps/gateway"
exec "$UVICORN_BIN" src.main:app --host "$GATEWAY_HOST" --port "$GATEWAY_PORT"
