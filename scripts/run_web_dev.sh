#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
# shellcheck disable=SC1091
source "$REPO_ROOT/scripts/load_env.sh"

WEB_PORT="${WEB_PORT:-3000}"
WEB_HOST="${WEB_HOST:-127.0.0.1}"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
GATEWAY_PORT="${GATEWAY_PORT:-8080}"

export VITE_DEV_PROXY_HTTP_TARGET="${VITE_DEV_PROXY_HTTP_TARGET:-http://$GATEWAY_HOST:$GATEWAY_PORT}"

. /opt/nvm/nvm.sh

cd "$REPO_ROOT/apps/web"
exec npm run dev -- --host "$WEB_HOST" --port "$WEB_PORT"
