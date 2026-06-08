#!/usr/bin/env bash

OMNI_ENV_FILE="${OMNI_ENV_FILE:-${WORKSPACE:-/workspace}/.env}"

if [[ -f "$OMNI_ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1090
  source "$OMNI_ENV_FILE"
  set +a
fi
