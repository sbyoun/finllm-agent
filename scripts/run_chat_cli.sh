#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$APP_ROOT"
ALPHA_ENGINE_ROOT="${ALPHA_ENGINE_ROOT:-/home/ubuntu/alpha-engine}"
PYTHON_BIN="${PYTHON_BIN:-$ALPHA_ENGINE_ROOT/.venv/bin/python}"

exec env APP_ROOT="$APP_ROOT" AGENT_REPO_ROOT="${AGENT_REPO_ROOT:-$APP_ROOT}" PYTHONPATH=src \
  "$PYTHON_BIN" scripts/chat_cli.py
