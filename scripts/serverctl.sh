#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="/home/ubuntu/finllm/dev/runtime-dev"
TARGET_ENV="production"

if [[ "${1:-}" == "--env" ]]; then
  TARGET_ENV="${2:-}"
  shift 2 || true
fi

case "$TARGET_ENV" in
  production)
    RUNTIME_SERVICE_NAME="${RUNTIME_SERVICE_NAME:-financial-agent-runtime-py}"
    RUNTIME_PORT="${RUNTIME_PORT:-8010}"
    ;;
  staging)
    RUNTIME_SERVICE_NAME="${RUNTIME_SERVICE_NAME:-financial-agent-runtime-py-staging}"
    RUNTIME_PORT="${RUNTIME_PORT:-8001}"
    ;;
  *)
    echo "unknown env: $TARGET_ENV" >&2
    exit 1
    ;;
esac

export RUNTIME_SERVICE_NAME
export RUNTIME_PORT

usage() {
  cat <<EOF
usage: $0 [--env production|staging] <command>

commands:
  start     start systemd user service
  stop      stop systemd user service
  restart   restart service and wait for health
  status    show systemd status and health
  logs      show recent service logs
  health    call local health endpoint
EOF
}

cmd="${1:-}"

case "$cmd" in
  start)
    systemctl --user start "$RUNTIME_SERVICE_NAME"
    exec "$APP_ROOT/scripts/status-runtime.sh"
    ;;
  stop)
    systemctl --user stop "$RUNTIME_SERVICE_NAME"
    systemctl --user status "$RUNTIME_SERVICE_NAME" --no-pager --lines=20 || true
    ;;
  restart)
    exec "$APP_ROOT/scripts/restart-runtime.sh"
    ;;
  status)
    exec "$APP_ROOT/scripts/status-runtime.sh"
    ;;
  logs)
    shift || true
    exec "$APP_ROOT/scripts/logs-runtime.sh" "${1:-200}"
    ;;
  health)
    exec curl -fsS "http://127.0.0.1:${RUNTIME_PORT}/health"
    ;;
  *)
    usage
    exit 1
    ;;
esac
