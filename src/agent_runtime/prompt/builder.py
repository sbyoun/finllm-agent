from __future__ import annotations

from pathlib import Path

from agent_runtime.tool.tool import ToolDefinition


DEFAULT_SKILL_FILES: list[str] = ["schema_guide.md"]


def _prompt_root() -> Path:
    return Path(__file__).resolve().parent


def load_system_prompt() -> str:
    path = _prompt_root() / "system_prompt.md"
    return path.read_text(encoding="utf-8").strip()


def load_skill_catalog(*, repo_root: str | Path) -> str:
    path = Path(repo_root) / "skills" / "skill_catalog.md"
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8").strip()


def build_tool_inventory(*, tools: list[ToolDefinition]) -> str:
    if not tools:
        return "## Available Tools\n\n- None"
    lines = ["## Available Tools", ""]
    for tool in tools:
        lines.append(f"- `{tool.name}`: {tool.description}")
    return "\n".join(lines)


def build_system_prompt(*, tools: list[ToolDefinition], repo_root: str | Path) -> str:
    parts = [
        load_system_prompt(),
        build_tool_inventory(tools=tools),
    ]
    skill_catalog = load_skill_catalog(repo_root=repo_root)
    if skill_catalog:
        parts.append(skill_catalog)
    return "\n\n".join(part for part in parts if part)


def build_default_dynamic_context(*, repo_root: str | Path) -> str:
    return load_skill_catalog(repo_root=repo_root)
