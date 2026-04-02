from __future__ import annotations

import os
from pathlib import Path


def load_env(env_path: str | Path | None = None) -> None:
    target = Path(env_path) if env_path else Path.cwd() / ".env"
    if not target.exists():
        return

    for line in target.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        key = key.strip()
        value = value.strip()
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not configured.")
    return value
