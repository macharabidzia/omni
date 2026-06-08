#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/load_env.sh"

PUBLIC_IPADDR="${PUBLIC_IPADDR:-127.0.0.1}"
WEB_EXTERNAL_PORT="${WEB_EXTERNAL_PORT:-3000}"
GATEWAY_EXTERNAL_PORT="${GATEWAY_EXTERNAL_PORT:-8080}"
QWEN_EXTERNAL_PORT="${QWEN_EXTERNAL_PORT:-8091}"
WEB_PORT="${WEB_PORT:-17070}"
GATEWAY_PORT="${GATEWAY_PORT:-17080}"
QWEN_PORT="${QWEN_PORT:-17091}"
ENABLE_HTTPS="${ENABLE_HTTPS:-true}"
RESTART_CADDY="${RESTART_CADDY:-1}"

cat > /etc/portal.yaml <<EOF
applications:
  Omni Web:
    hostname: localhost
    external_port: ${WEB_EXTERNAL_PORT}
    internal_port: ${WEB_PORT}
    open_path: /
    name: Omni Web
  Omni Gateway:
    hostname: localhost
    external_port: ${GATEWAY_EXTERNAL_PORT}
    internal_port: ${GATEWAY_PORT}
    open_path: /ready
    name: Omni Gateway
  Omni Qwen:
    hostname: localhost
    external_port: ${QWEN_EXTERNAL_PORT}
    internal_port: ${QWEN_PORT}
    open_path: /health
    name: Omni Qwen
EOF

if [[ "$ENABLE_HTTPS" == "true" ]]; then
  if [[ ! -s /etc/instance.crt || ! -s /etc/instance.key ]]; then
    openssl req \
      -x509 \
      -nodes \
      -newkey rsa:2048 \
      -sha256 \
      -days 3650 \
      -subj "/CN=${PUBLIC_IPADDR}" \
      -addext "subjectAltName=IP:${PUBLIC_IPADDR},IP:127.0.0.1,DNS:localhost" \
      -keyout /etc/instance.key \
      -out /etc/instance.crt
  fi
fi

if [[ "$RESTART_CADDY" == "1" ]]; then
  supervisorctl restart caddy || true
fi
