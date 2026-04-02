from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.tool.schema import Action, Observation
from agent_runtime.tool.tool import ToolDefinition


@dataclass(slots=True)
class FinishAction(Action):
    reason: str = "finished"


@dataclass(slots=True)
class FinishObservation(Observation):
    pass


class FinishTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "reason": {"type": "string"},
            },
            "required": [],
        }


@dataclass(slots=True)
class ThinkAction(Action):
    thought: str = ""


@dataclass(slots=True)
class ThinkObservation(Observation):
    pass


class ThinkTool(ToolDefinition):
    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "thought": {"type": "string"},
            },
            "required": ["thought"],
        }
