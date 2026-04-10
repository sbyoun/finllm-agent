from __future__ import annotations

from collections.abc import Sequence

from agent_runtime.event.action import ActionEvent
from agent_runtime.event.base import Event
from agent_runtime.event.message import MessageEvent
from agent_runtime.event.observation import AgentErrorEvent, ObservationEvent
from agent_runtime.event.state_update import ConversationStateUpdateEvent


def events_to_messages(events: Sequence[Event]) -> list[dict]:
    messages: list[dict] = []
    for event in events:
        if isinstance(event, MessageEvent):
            messages.append(event.to_message_dict())
        elif isinstance(event, ActionEvent):
            messages.append(event.to_message_dict())
        elif isinstance(event, (ObservationEvent, AgentErrorEvent)):
            messages.append(event.to_message_dict())
        elif isinstance(event, ConversationStateUpdateEvent):
            continue
    return messages
