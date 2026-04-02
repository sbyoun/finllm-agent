from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol
from uuid import uuid4

from agent_runtime.event.types import EventType, SourceType


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(slots=True)
class Event:
    event_type: EventType
    source: SourceType
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: str = field(default_factory=_utc_now_iso)


class LLMConvertibleEvent(Protocol):
    def to_message_dict(self) -> dict:
        ...
