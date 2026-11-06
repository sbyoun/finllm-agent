from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from agent_runtime.context.condenser import CondenserBase
from agent_runtime.llm.client import LLMClient
from agent_runtime.skills import load_skill_text
from agent_runtime.tool.tool import ToolDefinition


@dataclass(slots=True)
class AgentBase(ABC):
    llm: LLMClient
    tools: list[ToolDefinition] = field(default_factory=list)
    system_prompt: str = "You are a helpful agent."
    dynamic_context: str | None = None
    repo_root: str | None = None
    skill_files: list[str] = field(default_factory=list)
    skill_files_compact: list[str] = field(default_factory=list)
    condenser: CondenserBase | None = None
    # True 면 스키마 안내(skill_files)가 이미 system_prompt 안에 있다 — 매 호출 끝 <context> 에 다시 붙이지 않는다.
    # Gemini 명시 캐시용: 고정 부분(시스템+툴+스키마)을 한 덩어리로 캐시에 올린다(llm/gemini_cache.py)
    schema_in_system: bool = False

    def resolved_dynamic_context(self, *, compact: bool = False) -> str | None:
        parts: list[str] = []
        if self.dynamic_context:
            parts.append(self.dynamic_context)
        if self.repo_root:
            names = self.skill_files_compact if (compact and self.skill_files_compact) else self.skill_files
            if names:
                skill_text = load_skill_text(repo_root=self.repo_root, names=names)
                if skill_text:
                    parts.append(skill_text)
        return "\n\n".join(parts) if parts else None

    @abstractmethod
    def step(self, conversation: object) -> None:
        ...
