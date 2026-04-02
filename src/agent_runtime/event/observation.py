from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.event.base import Event
from agent_runtime.tool.schema import Observation


@dataclass(slots=True)
class ObservationEvent(Event):
    tool_name: str = ""
    tool_call_id: str = ""
    action_id: str = ""
    observation: Observation | None = None

    def __init__(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        action_id: str,
        observation: Observation,
    ) -> None:
        Event.__init__(self, event_type="observation", source="environment")
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.action_id = action_id
        self.observation = observation

    def to_message_dict(self) -> dict:
        return {
            "role": "tool",
            "name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "content": self.observation.to_text(),
        }


@dataclass(slots=True)
class AgentErrorEvent(Event):
    tool_name: str = ""
    tool_call_id: str = ""
    error: str = ""

    def __init__(self, *, tool_name: str, tool_call_id: str, error: str) -> None:
        Event.__init__(self, event_type="agent_error", source="agent")
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.error = error

    def to_message_dict(self) -> dict:
        return {
            "role": "tool",
            "name": self.tool_name,
            "tool_call_id": self.tool_call_id,
            "content": (
                f"TOOL_ERROR[{self.tool_name}]\n"
                f"{self.error}\n\n"
                "If this error came from SQL execution, keep the same analytical intent, "
                "correct the SQL, and try again."
            ),
        }
