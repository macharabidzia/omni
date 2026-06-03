#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
MODEL_ID="${MODEL_ID:-Qwen/Qwen3-Omni-30B-A3B-Instruct}"
MODEL_NAME="${MODEL_ID##*/}"
LOCAL_DIR="${LOCAL_DIR:-$ROOT_DIR/models/$MODEL_NAME}"
MAX_WORKERS="${MAX_WORKERS:-8}"

if ! command -v hf >/dev/null 2>&1; then
  echo "error: 'hf' was not found on PATH. Install it with: python3 -m pip install -U \"huggingface_hub[cli]\"" >&2
  exit 1
fi

export HF_HUB_DISABLE_XET="${HF_HUB_DISABLE_XET:-1}"

mkdir -p "$(dirname "$LOCAL_DIR")"

echo "Downloading $MODEL_ID into $LOCAL_DIR"
echo "HF_HUB_DISABLE_XET=$HF_HUB_DISABLE_XET MAX_WORKERS=$MAX_WORKERS"

exec hf download "$MODEL_ID" --local-dir "$LOCAL_DIR" --max-workers "$MAX_WORKERS" "$@"
