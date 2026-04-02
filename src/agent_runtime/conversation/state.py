from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4

from agent_runtime.conversation.event_log import EventLog
from agent_runtime.event.state_update import ConversationStateUpdateEvent


class ConversationExecutionStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    FINISHED = "finished"
    ERROR = "error"


@dataclass(slots=True)
class ConversationState:
    id: str = field(default_factory=lambda: str(uuid4()))
    execution_status: ConversationExecutionStatus = ConversationExecutionStatus.IDLE
    max_iterations: int = 500
    agent_state: dict[str, Any] = field(default_factory=dict)
    event_log: EventLog = field(default_factory=EventLog)

    def get_agent_state(self, key: str, default: Any | None = None) -> Any | None:
        return self.agent_state.get(key, default)

    def set_agent_state(self, key: str, value: Any) -> None:
        self.agent_state[key] = value
        self.event_log.append(
            ConversationStateUpdateEvent(key=key, operation="set", value=value)
        )

    def delete_agent_state(self, key: str) -> None:
        if key not in self.agent_state:
            return
        self.agent_state.pop(key, None)
        self.event_log.append(
            ConversationStateUpdateEvent(key=key, operation="delete", value=None)
        )
