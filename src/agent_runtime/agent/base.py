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
    condenser: CondenserBase | None = None

    def resolved_dynamic_context(self) -> str | None:
        parts: list[str] = []
        if self.dynamic_context:
            parts.append(self.dynamic_context)
        if self.repo_root and self.skill_files:
            skill_text = load_skill_text(repo_root=self.repo_root, names=self.skill_files)
            if skill_text:
                parts.append(skill_text)
        return "\n\n".join(parts) if parts else None

    @abstractmethod
    def step(self, conversation: object) -> None:
        ...
