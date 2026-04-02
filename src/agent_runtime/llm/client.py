from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4


@dataclass(slots=True)
class Message:
    role: str
    content: str
    tool_calls: list["LLMToolCall"] = field(default_factory=list)


@dataclass(slots=True)
class LLMToolCall:
    name: str
    arguments: str
    id: str = field(default_factory=lambda: str(uuid4()))


@dataclass(slots=True)
class LLMResponse:
    message: Message
    id: str = field(default_factory=lambda: str(uuid4()))


class LLMClient(Protocol):
    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        ...
