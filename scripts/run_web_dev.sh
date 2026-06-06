#!/usr/bin/env bash

set -euo pipefail

REPO_ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
WEB_PORT="${WEB_PORT:-5173}"
GATEWAY_HOST="${GATEWAY_HOST:-127.0.0.1}"
GATEWAY_PORT="${GATEWAY_PORT:-8000}"

export VITE_DEV_PROXY_HTTP_TARGET="${VITE_DEV_PROXY_HTTP_TARGET:-http://$GATEWAY_HOST:$GATEWAY_PORT}"

cd "$REPO_ROOT/apps/web"
exec npm run dev -- --host 0.0.0.0 --port "$WEB_PORT"
