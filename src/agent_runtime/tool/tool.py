from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Protocol

from agent_runtime.tool.schema import Action, Observation


class ToolExecutor(Protocol):
    def __call__(self, action: Action, conversation: object | None = None) -> Observation:
        ...


@dataclass(slots=True)
class ToolDefinition(ABC):
    name: str
    description: str
    action_type: type[Action]
    observation_type: type[Observation]
    executor: ToolExecutor | None = None

    @abstractmethod
    def schema(self) -> dict:
        ...

    def action_from_arguments(self, arguments: dict) -> Action:
        return self.action_type(**arguments)

    def as_llm_tool(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.schema(),
            },
        }

    def __call__(self, action: Action, conversation: object | None = None) -> Observation:
        if self.executor is None:
            raise NotImplementedError(f"Tool '{self.name}' has no executor")
        return self.executor(action, conversation)
