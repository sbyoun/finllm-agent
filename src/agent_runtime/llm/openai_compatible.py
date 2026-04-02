from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.llm.client import LLMResponse, LLMToolCall, Message


def _chat_completions_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/chat/completions"
    return f"{normalized}/v1/chat/completions"


@dataclass(slots=True)
class OpenAICompatibleClient:
    model: str
    api_key: str
    base_url: str = "https://api.openai.com"

    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        request = Request(
            _chat_completions_url(self.base_url),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )

        try:
            with urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenAI-compatible HTTP {exc.code}: {body}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"OpenAI-compatible provider returned no choices: {data}")

        raw_message = choices[0].get("message") or {}
        content = raw_message.get("content") or ""
        tool_calls = [
            LLMToolCall(
                id=tool_call.get("id") or tool_call.get("index") or "",
                name=((tool_call.get("function") or {}).get("name")) or "",
                arguments=((tool_call.get("function") or {}).get("arguments")) or "{}",
            )
            for tool_call in (raw_message.get("tool_calls") or [])
            if ((tool_call.get("function") or {}).get("name"))
        ]

        return LLMResponse(
            message=Message(
                role="assistant",
                content=content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
                tool_calls=tool_calls,
            )
        )
