from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.event.base import Event


@dataclass(slots=True)
class CondensationEvent(Event):
    forgotten_event_ids: list[str] = field(default_factory=list)
    summary: str = ""
    summary_offset: int = 0

    def __init__(
        self,
        *,
        forgotten_event_ids: list[str],
        summary: str,
        summary_offset: int,
    ) -> None:
        Event.__init__(self, event_type="condensation", source="agent")
        self.forgotten_event_ids = forgotten_event_ids
        self.summary = summary
        self.summary_offset = summary_offset

    def to_message_dict(self) -> dict:
        return {
            "role": "assistant",
            "content": f"Conversation summary so far:\n{self.summary}",
        }
