from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.event.base import Event
from agent_runtime.tool.schema import Action


@dataclass(slots=True)
class ActionEvent(Event):
    tool_name: str = ""
    tool_call_id: str = ""
    thought: str = ""
    action: Action | None = None
    llm_response_id: str | None = None
    summary: str | None = None

    def __init__(
        self,
        *,
        tool_name: str,
        tool_call_id: str,
        thought: str,
        action: Action | None,
        llm_response_id: str | None = None,
        summary: str | None = None,
    ) -> None:
        Event.__init__(self, event_type="action", source="agent")
        self.tool_name = tool_name
        self.tool_call_id = tool_call_id
        self.thought = thought
        self.action = action
        self.llm_response_id = llm_response_id
        self.summary = summary

    def to_message_dict(self) -> dict:
        return {
            "role": "assistant",
            "content": self.thought,
            "tool_calls": [
                {
                    "id": self.tool_call_id,
                    "type": "function",
                    "function": {
                        "name": self.tool_name,
                        "arguments": self.action.to_arguments_json()
                        if self.action is not None
                        else "{}",
                    },
                }
            ],
        }
