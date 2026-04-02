from __future__ import annotations

from typing import Literal

EventType = Literal[
    "system_prompt",
    "message",
    "action",
    "observation",
    "agent_error",
    "conversation_state_update",
    "condensation",
]
SourceType = Literal["agent", "user", "environment"]
