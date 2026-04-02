#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/home/ubuntu/financial-agent-runtime-py"
PYTHON_BIN="/home/ubuntu/alpha-engine/.venv/bin/python"
APP_MODULE="agent_runtime.api.app:app"
ENV_FILE="${ENV_FILE:-.env}"

cd "$APP_ROOT"

if [[ -f "$ENV_FILE" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ENV_FILE"
  set +a
fi

if [[ -z "${HOST:-}" ]]; then
  export HOST=127.0.0.1
fi

if [[ -z "${PORT:-}" ]]; then
  export PORT=8001
fi

exec env PYTHONPATH="$APP_ROOT/src" \
  "$PYTHON_BIN" -m uvicorn "$APP_MODULE" --host "$HOST" --port "$PORT"
