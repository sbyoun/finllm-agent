from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent_runtime.event.base import Event


@dataclass(slots=True)
class ConversationStateUpdateEvent(Event):
    key: str = ""
    operation: str = "set"
    value: Any | None = None

    def __init__(
        self,
        *,
        key: str,
        operation: str,
        value: Any | None = None,
    ) -> None:
        Event.__init__(self, event_type="conversation_state_update", source="agent")
        self.key = key
        self.operation = operation
        self.value = value
