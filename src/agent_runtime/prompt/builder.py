from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from agent_runtime.tool.tool import ToolDefinition


DEFAULT_SKILL_FILES: list[str] = ["schema_guide.md"]
DEFAULT_SKILL_FILES_COMPACT: list[str] = ["schema_guide_compact.md"]


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


_KST = timezone(timedelta(hours=9))


def current_time_context(now: datetime | None = None) -> str:
    """분 단위 현재 시각 — 매 호출 끝의 <context> 에 붙인다(agent.step).

    시스템 프롬프트에 넣으면 프롬프트가 매분 바뀌어, 시스템 프롬프트·툴 정의(호출당 약 8천 토큰)가
    실행 사이에 Gemini 자동 캐시로 재사용되지 않는다(11/6). 날짜는 하루 한 번만 바뀌므로 시스템 프롬프트에 둔다.
    """
    now_kst = (now or datetime.now(_KST)).astimezone(_KST)
    return f"Current time: {now_kst.strftime('%Y-%m-%d %H:%M')} KST."


def build_system_prompt(*, tools: list[ToolDefinition], repo_root: str | Path, now: datetime | None = None) -> str:
    today = (now or datetime.now(_KST)).astimezone(_KST).strftime("%Y-%m-%d")
    parts = [
        f"Today: {today} KST (UTC+9). The exact current time is given in the <context> block at the end. When scheduling jobs, convert user's time to UTC for cron expressions (e.g., '아침 9시' KST = '0 0 * * *' UTC).",
        load_system_prompt(),
        build_tool_inventory(tools=tools),
    ]
    skill_catalog = load_skill_catalog(repo_root=repo_root)
    if skill_catalog:
        parts.append(skill_catalog)
    return "\n\n".join(part for part in parts if part)


def build_default_dynamic_context(*, repo_root: str | Path) -> str:
    return load_skill_catalog(repo_root=repo_root)
