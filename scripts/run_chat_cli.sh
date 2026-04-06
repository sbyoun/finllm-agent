#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$APP_ROOT"
exec env APP_ROOT="$APP_ROOT" AGENT_REPO_ROOT="${AGENT_REPO_ROOT:-$APP_ROOT}" PYTHONPATH=src \
  /home/ubuntu/alpha-engine/.venv/bin/python scripts/chat_cli.py
