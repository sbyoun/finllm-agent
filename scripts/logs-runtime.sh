#!/usr/bin/env bash
set -euo pipefail

RUNTIME_SERVICE_NAME="${RUNTIME_SERVICE_NAME:-financial-agent-runtime-py-staging}"
LINES="${1:-200}"

exec journalctl --user -u "$RUNTIME_SERVICE_NAME" -n "$LINES" --no-pager
