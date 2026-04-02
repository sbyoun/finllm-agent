from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agent_runtime.event.base import Event


@dataclass(slots=True)
class SystemPromptEvent(Event):
    system_prompt: str = ""
    dynamic_context: str | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)

    def __init__(
        self,
        *,
        system_prompt: str,
        dynamic_context: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> None:
        Event.__init__(self, event_type="system_prompt", source="agent")
        self.system_prompt = system_prompt
        self.dynamic_context = dynamic_context
        self.tools = tools or []

    def to_message_dict(self) -> dict:
        content = self.system_prompt
        if self.dynamic_context:
            content = f"{content}\n\n{self.dynamic_context}"
        return {"role": "system", "content": content}


@dataclass(slots=True)
class MessageEvent(Event):
    role: str = "user"
    content: str = ""

    def __init__(self, *, source: str, role: str, content: str) -> None:
        Event.__init__(self, event_type="message", source=source)  # type: ignore[arg-type]
        self.role = role
        self.content = content

    def to_message_dict(self) -> dict:
        return {"role": self.role, "content": self.content}
