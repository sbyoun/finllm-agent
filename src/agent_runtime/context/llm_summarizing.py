from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.context.rolling import RollingCondenser
from agent_runtime.event.action import ActionEvent
from agent_runtime.event.message import MessageEvent, SystemPromptEvent
from agent_runtime.event.observation import AgentErrorEvent, ObservationEvent
from agent_runtime.llm.client import LLMClient


@dataclass(slots=True)
class LLMSummarizingCondenser(RollingCondenser):
    llm: LLMClient | None = None

    def summarize(self, events: list) -> str:
        if self.llm is None:
            return self._fallback_summary(events)

        prompt_lines = [
            "Summarize the following conversation history for continued agent execution.",
            "Preserve user goals, required constraints, key tool findings, SQL/schema discoveries, failures, and unfinished work.",
            "Be concise and factual. Use short bullet points.",
            "",
        ]
        for event in events:
            prompt_lines.append(self._event_to_line(event))
        response = self.llm.completion(
            messages=[
                {
                    "role": "system",
                    "content": "You compress conversation history for an agent. Keep only durable facts and unresolved tasks.",
                },
                {
                    "role": "user",
                    "content": "\n".join(prompt_lines),
                },
            ],
            tools=None,
        )
        content = response.message.content.strip()
        return content or self._fallback_summary(events)

    def _fallback_summary(self, events: list) -> str:
        lines = [self._event_to_line(event) for event in events[:12]]
        if len(events) > 12:
            lines.append(f"- ... {len(events) - 12} more events omitted")
        return "\n".join(lines)

    def _event_to_line(self, event: object) -> str:
        if isinstance(event, SystemPromptEvent):
            return "- system prompt established"
        if isinstance(event, MessageEvent):
            content = " ".join(event.content.split())
            return f"- {event.role}: {content[:300]}"
        if isinstance(event, ActionEvent):
            return f"- tool call: {event.tool_name}"
        if isinstance(event, ObservationEvent):
            text = " ".join(event.observation.to_text().split())
            return f"- tool result ({event.tool_name}): {text[:300]}"
        if isinstance(event, AgentErrorEvent):
            return f"- tool error ({event.tool_name}): {event.error[:300]}"
        return f"- event: {type(event).__name__}"
