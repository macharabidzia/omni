#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/load_env.sh"

PUBLIC_IPADDR="${PUBLIC_IPADDR:-127.0.0.1}"
WEB_EXTERNAL_PORT="${WEB_EXTERNAL_PORT:-3000}"
GATEWAY_EXTERNAL_PORT="${GATEWAY_EXTERNAL_PORT:-8080}"
QWEN_EXTERNAL_PORT="${QWEN_EXTERNAL_PORT:-8091}"
WEB_PUBLIC_PORT="${VAST_TCP_PORT_3000:-$WEB_EXTERNAL_PORT}"
GATEWAY_PUBLIC_PORT="${VAST_TCP_PORT_8080:-$GATEWAY_EXTERNAL_PORT}"
QWEN_PUBLIC_PORT="${VAST_TCP_PORT_8091:-$QWEN_EXTERNAL_PORT}"
URL_SCHEME="http"
if [[ "${ENABLE_HTTPS:-false}" == "true" ]]; then
  URL_SCHEME="https"
fi
TOKEN_SUFFIX=""
if [[ "${ENABLE_AUTH:-true}" != "false" && -n "${OPEN_BUTTON_TOKEN:-}" ]]; then
  TOKEN_SUFFIX="?token=${OPEN_BUTTON_TOKEN}"
fi

supervisorctl status omni-qwen omni-gateway omni-worker omni-web || true
echo "web_url=${URL_SCHEME}://$PUBLIC_IPADDR:$WEB_PUBLIC_PORT/$TOKEN_SUFFIX"
echo "gateway_ready_url=${URL_SCHEME}://$PUBLIC_IPADDR:$GATEWAY_PUBLIC_PORT/ready$TOKEN_SUFFIX"
echo "qwen_health_url=${URL_SCHEME}://$PUBLIC_IPADDR:$QWEN_PUBLIC_PORT/health$TOKEN_SUFFIX"
curl -fsS "http://127.0.0.1:${GATEWAY_PORT:-8080}/ready" || true
