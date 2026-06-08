#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/load_env.sh"

VENV_PATH="${QWEN_VENV_PATH:-$REPO_ROOT/.venv-qwen}"
MODEL_PATH="${QWEN_MODEL:-${QWEN_MODEL_PATH:-$REPO_ROOT/models/Qwen3-Omni-30B-A3B-Instruct}}"
DEPLOY_CONFIG_PATH="${QWEN_DEPLOY_CONFIG:-${QWEN_DEPLOY_CONFIG_PATH:-$REPO_ROOT/configs/qwen3_omni_single_a100_async_fastaudio.yaml}}"
QWEN_PORT="${QWEN_PORT:-8091}"
QWEN_HOST="${QWEN_HOST:-127.0.0.1}"
QWEN_INIT_TIMEOUT="${QWEN_INIT_TIMEOUT:-1800}"
QWEN_STAGE_INIT_TIMEOUT="${QWEN_STAGE_INIT_TIMEOUT:-600}"
QWEN_SAFETENSORS_LOAD_STRATEGY="${QWEN_SAFETENSORS_LOAD_STRATEGY:-prefetch}"
CACHE_DIR="${HF_HOME:-$REPO_ROOT/.cache/huggingface}"
PATCH_VLLM_ASYNC_REALTIME="${PATCH_VLLM_ASYNC_REALTIME:-1}"

if [[ ! -x "$VENV_PATH/bin/vllm" ]]; then
  echo "error: vLLM runtime not found at $VENV_PATH/bin/vllm" >&2
  echo "hint: run scripts/bootstrap_vast.sh first" >&2
  exit 1
fi

mkdir -p "$CACHE_DIR"
export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"
export HF_HOME="$CACHE_DIR"
export TRANSFORMERS_CACHE="$CACHE_DIR"
export VLLM_USE_V1="${VLLM_USE_V1:-0}"

if [[ "$PATCH_VLLM_ASYNC_REALTIME" == "1" ]]; then
  "$VENV_PATH/bin/python" "$REPO_ROOT/scripts/patch_vllm_async_realtime.py" --venv-path "$VENV_PATH"
  export VLLM_OMNI_ALLOW_REALTIME_ASYNC_CHUNK="${VLLM_OMNI_ALLOW_REALTIME_ASYNC_CHUNK:-1}"
fi

"$VENV_PATH/bin/python" "$REPO_ROOT/scripts/patch_vllm_qwen3_omni_initial_codec_chunk.py" --venv-path "$VENV_PATH"
"$VENV_PATH/bin/python" "$REPO_ROOT/scripts/patch_vllm_realtime_segment_duration.py" --venv-path "$VENV_PATH"
export VLLM_QWEN_REALTIME_SEGMENT_DURATION_S="${VLLM_QWEN_REALTIME_SEGMENT_DURATION_S:-2.0}"

extra_flags=()
if [[ -n "${QWEN_VLLM_FLAGS:-}" ]]; then
  # shellcheck disable=SC2206
  extra_flags=(${QWEN_VLLM_FLAGS})
fi

exec "$VENV_PATH/bin/vllm" serve "$MODEL_PATH" \
  --omni \
  --deploy-config "$DEPLOY_CONFIG_PATH" \
  --host "$QWEN_HOST" \
  --port "$QWEN_PORT" \
  --trust-remote-code \
  --log-stats \
  --stage-init-timeout "$QWEN_STAGE_INIT_TIMEOUT" \
  --init-timeout "$QWEN_INIT_TIMEOUT" \
  --safetensors-load-strategy "$QWEN_SAFETENSORS_LOAD_STRATEGY" \
  "${extra_flags[@]}"
