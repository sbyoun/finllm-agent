#!/usr/bin/env bash
set -euo pipefail

cd /home/ubuntu/financial-agent-runtime-py
PYTHONPATH=src /home/ubuntu/alpha-engine/.venv/bin/python scripts/chat_cli.py
