#!/usr/bin/env bash
set -euo pipefail

RUNTIME_SERVICE_NAME="${RUNTIME_SERVICE_NAME:-financial-agent-runtime-py}"
RUNTIME_PORT="${RUNTIME_PORT:-8010}"

echo "== systemd =="
systemctl --user status "$RUNTIME_SERVICE_NAME" --no-pager || true
echo
echo "== health =="

ok=0
for _ in $(seq 1 10); do
  if curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/health" >/dev/null 2>&1; then
    curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/health"
    echo
    ok=1
    break
  fi
  sleep 1
done

if [[ "$ok" -eq 1 ]]; then
  exit 0
fi

echo "runtime health check failed" >&2
exit 1
