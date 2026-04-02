from __future__ import annotations

from dataclasses import dataclass

from agent_runtime.llm.client import LLMResponse, Message


@dataclass(slots=True)
class DummyLLM:
    response_text: str = "Not implemented."

    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        return LLMResponse(message=Message(role="assistant", content=self.response_text))
