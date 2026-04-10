from __future__ import annotations

from dataclasses import dataclass, field

from agent_runtime.event.action import ActionEvent
from agent_runtime.event.base import Event
from agent_runtime.event.condensation import CondensationEvent
from agent_runtime.event.message import MessageEvent
from agent_runtime.event.observation import AgentErrorEvent, ObservationEvent


LLM_EVENT_TYPES = (MessageEvent, ActionEvent, ObservationEvent, AgentErrorEvent, CondensationEvent)


@dataclass(slots=True)
class View:
    events: list[Event] = field(default_factory=list)
    condensations: list[CondensationEvent] = field(default_factory=list)

    @classmethod
    def from_events(cls, events: list[Event]) -> "View":
        condensations = [event for event in events if isinstance(event, CondensationEvent)]
        if not condensations:
            visible = [event for event in events if isinstance(event, LLM_EVENT_TYPES[:-1])]
            return cls(events=visible, condensations=[])

        forgotten_ids: set[str] = set()
        latest = condensations[-1]
        for condensation in condensations:
            forgotten_ids.update(condensation.forgotten_event_ids)

        base_events = [
            event
            for event in events
            if isinstance(event, LLM_EVENT_TYPES[:-1]) and event.id not in forgotten_ids
        ]
        insert_at = max(0, min(latest.summary_offset, len(base_events)))
        visible = list(base_events[:insert_at]) + [latest] + list(base_events[insert_at:])
        return cls(events=visible, condensations=condensations)

    def to_messages(self) -> list[dict]:
        return [event.to_message_dict() for event in self.events if hasattr(event, "to_message_dict")]
