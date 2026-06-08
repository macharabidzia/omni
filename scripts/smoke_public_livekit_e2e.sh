#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/load_env.sh"

INPUT_PATH="${1:-}"
OUTPUT_PREFIX="${2:-$REPO_ROOT/tmp/public-livekit-e2e}"

if [[ -z "$INPUT_PATH" ]]; then
  echo "usage: scripts/smoke_public_livekit_e2e.sh <input-audio-file> [output-prefix]" >&2
  exit 2
fi

if [[ ! -f "$INPUT_PATH" ]]; then
  echo "error: input audio file not found: $INPUT_PATH" >&2
  exit 1
fi

if [[ ! -x "$REPO_ROOT/.venv/bin/python" ]]; then
  echo "error: app venv not found at $REPO_ROOT/.venv/bin/python" >&2
  echo "hint: run scripts/bootstrap_vast.sh first" >&2
  exit 1
fi

mkdir -p "$(dirname "$OUTPUT_PREFIX")"

NORMALIZED_INPUT="${OUTPUT_PREFIX}.input.wav"
CHECK_JSON="${OUTPUT_PREFIX}.check.json"
CAPTURE_JSON="${OUTPUT_PREFIX}.capture.json"
CAPTURE_WAV="${OUTPUT_PREFIX}.capture.wav"
ANALYSIS_JSON="${OUTPUT_PREFIX}.analysis.json"

PUBLIC_IPADDR="${PUBLIC_IPADDR:-127.0.0.1}"
WEB_EXTERNAL_PORT="${WEB_EXTERNAL_PORT:-3000}"
WEB_PUBLIC_PORT="${VAST_TCP_PORT_3000:-$WEB_EXTERNAL_PORT}"
URL_SCHEME="http"
if [[ "${ENABLE_HTTPS:-false}" == "true" ]]; then
  URL_SCHEME="https"
fi
WEB_URL="${URL_SCHEME}://${PUBLIC_IPADDR}:${WEB_PUBLIC_PORT}/"
if [[ "${ENABLE_AUTH:-true}" != "false" && -n "${OPEN_BUTTON_TOKEN:-}" ]]; then
  WEB_URL="${WEB_URL}?token=${OPEN_BUTTON_TOKEN}"
fi

ffmpeg -y -i "$INPUT_PATH" -ac 1 -ar 16000 -c:a pcm_s16le "$NORMALIZED_INPUT" >/dev/null 2>&1

. /opt/nvm/nvm.sh

(
  cd "$REPO_ROOT/apps/web"
  node scripts/playwright-livekit-check.mjs "$WEB_URL" "$NORMALIZED_INPUT" "$CHECK_JSON"
  node scripts/playwright-livekit-capture.mjs "$WEB_URL" "$NORMALIZED_INPUT" "$CAPTURE_JSON" "$CAPTURE_WAV"
)

"$REPO_ROOT/.venv/bin/python" "$REPO_ROOT/scripts/analyze_capture_audio.py" \
  "$CAPTURE_WAV" \
  --output-json "$ANALYSIS_JSON"

echo "web_url=$WEB_URL"
echo "normalized_input=$NORMALIZED_INPUT"
echo "check_json=$CHECK_JSON"
echo "capture_json=$CAPTURE_JSON"
echo "capture_wav=$CAPTURE_WAV"
echo "analysis_json=$ANALYSIS_JSON"
