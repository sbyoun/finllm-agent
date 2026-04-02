from __future__ import annotations

from pathlib import Path


def load_skill_text(*, repo_root: str | Path, names: list[str]) -> str:
    root = Path(repo_root) / "skills"
    parts: list[str] = []
    for name in names:
        path = root / name
        if path.exists():
            parts.append(path.read_text(encoding="utf-8").strip())
    return "\n\n".join(part for part in parts if part)
