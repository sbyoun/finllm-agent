#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/home/ubuntu/finllm/dev/runtime-dev"
RUNTIME_SERVICE_NAME="${RUNTIME_SERVICE_NAME:-financial-agent-runtime-py}"
RUNTIME_PORT="${RUNTIME_PORT:-8010}"

systemctl --user restart "$RUNTIME_SERVICE_NAME"
systemctl --user status "$RUNTIME_SERVICE_NAME" --no-pager --lines=20 || true
echo

for _ in $(seq 1 20); do
  if curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/health" >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/health"
    echo
    exit 0
  fi
  sleep 1
done

echo "runtime health check failed after restart" >&2
exit 1
