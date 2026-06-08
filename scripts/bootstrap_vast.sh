#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
OMNI_ENV_FILE="${OMNI_ENV_FILE:-${WORKSPACE:-/workspace}/.env}"
APP_VENV_PATH="${APP_VENV_PATH:-$REPO_ROOT/.venv}"
QWEN_VENV_PATH="${QWEN_VENV_PATH:-$REPO_ROOT/.venv-qwen}"
OMNI_PYTHON_VERSION="${OMNI_PYTHON_VERSION:-3.12.13}"
TORCH_INDEX_URL="${TORCH_INDEX_URL:-https://download.pytorch.org/whl/cu129}"
VLLM_WHEEL_URL="${VLLM_WHEEL_URL:-https://github.com/vllm-project/vllm/releases/download/v0.20.2/vllm-0.20.2%2Bcu129-cp38-abi3-manylinux_2_31_x86_64.whl}"
MODEL_PATH_DEFAULT="$REPO_ROOT/models/Qwen3-Omni-30B-A3B-Instruct"

if [[ ! -f "$OMNI_ENV_FILE" ]]; then
  cp "$REPO_ROOT/.env.example" "$OMNI_ENV_FILE"
fi

set -a
# shellcheck disable=SC1090
source "$OMNI_ENV_FILE"
set +a

QWEN_MODEL="${QWEN_MODEL:-$MODEL_PATH_DEFAULT}"

if ! command -v ffmpeg >/dev/null 2>&1; then
  apt-get update
  apt-get install -y ffmpeg
fi

uv python install "$OMNI_PYTHON_VERSION"

if [[ -x "$APP_VENV_PATH/bin/python" ]]; then
  APP_VENV_VERSION=$("$APP_VENV_PATH/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
  if [[ "$APP_VENV_VERSION" != "$OMNI_PYTHON_VERSION" ]]; then
    rm -rf "$APP_VENV_PATH"
  fi
fi

if [[ ! -x "$APP_VENV_PATH/bin/python" ]]; then
  uv venv --python "$OMNI_PYTHON_VERSION" "$APP_VENV_PATH"
fi

if ! "$APP_VENV_PATH/bin/python" -c "import fastapi, livekit, livekit.api, uvicorn" >/dev/null 2>&1; then
  (
    cd "$REPO_ROOT/apps/gateway"
    uv pip install --python "$APP_VENV_PATH/bin/python" -e '.[dev]'
  )
  uv pip install --python "$APP_VENV_PATH/bin/python" "huggingface_hub[cli]"
fi

if [[ -x "$QWEN_VENV_PATH/bin/python" ]]; then
  QWEN_VENV_VERSION=$("$QWEN_VENV_PATH/bin/python" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}')")
  if [[ "$QWEN_VENV_VERSION" != "$OMNI_PYTHON_VERSION" ]]; then
    rm -rf "$QWEN_VENV_PATH"
  fi
fi

if [[ ! -x "$QWEN_VENV_PATH/bin/python" ]]; then
  uv venv --python "$OMNI_PYTHON_VERSION" "$QWEN_VENV_PATH"
fi

if ! "$QWEN_VENV_PATH/bin/python" -c "import torch, vllm, vllm_omni" >/dev/null 2>&1; then
  uv pip install --python "$QWEN_VENV_PATH/bin/python" --upgrade pip setuptools wheel
  uv pip install --python "$QWEN_VENV_PATH/bin/python" \
    torch==2.11.0 \
    torchvision==0.26.0 \
    torchaudio==2.11.0 \
    --index-url "$TORCH_INDEX_URL"
  uv pip install --python "$QWEN_VENV_PATH/bin/python" "$VLLM_WHEEL_URL" vllm-omni==0.20.0 "huggingface_hub[cli]"
fi

. /opt/nvm/nvm.sh
(cd "$REPO_ROOT/apps/web" && npm ci)

mkdir -p "${HF_HOME:-/workspace/.hf_home}"
mkdir -p "$(dirname "$QWEN_MODEL")"

if [[ "${PREFETCH_MODEL:-1}" == "1" && ! -e "$QWEN_MODEL/config.json" ]]; then
  PATH="$QWEN_VENV_PATH/bin:$PATH" "$REPO_ROOT/scripts/prefetch_qwen_model.sh"
fi

echo "bootstrap_complete app_venv=$APP_VENV_PATH qwen_venv=$QWEN_VENV_PATH model=$QWEN_MODEL"
