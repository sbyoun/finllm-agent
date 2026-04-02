from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.llm.client import LLMResponse, LLMToolCall, Message


def _message_to_anthropic_content(message: dict) -> list[dict[str, Any]]:
    role = message.get("role")
    content = message.get("content", "")

    if role == "tool":
        tool_name = message.get("name", "tool")
        return [{"type": "text", "text": f"TOOL_RESULT[{tool_name}]\n{content}"}]

    parts: list[dict[str, Any]] = []
    if isinstance(content, str) and content:
        parts.append({"type": "text", "text": content})

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        rendered_calls = []
        for tool_call in tool_calls:
            rendered_calls.append(
                "TOOL_CALL[{name}] {args}".format(
                    name=tool_call["function"]["name"],
                    args=tool_call["function"]["arguments"] or "{}",
                )
            )
        parts.append({"type": "text", "text": "\n".join(rendered_calls)})
    return parts


def _tool_to_anthropic(tool: dict) -> dict[str, Any]:
    fn = tool["function"]
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
    }


def _messages_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/messages"):
        return normalized
    if normalized.endswith("/v1"):
        return f"{normalized}/messages"
    return f"{normalized}/v1/messages"


@dataclass(slots=True)
class AnthropicClient:
    model: str
    api_key: str
    base_url: str = "https://api.anthropic.com"

    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        system_message = next((m for m in messages if m.get("role") == "system"), None)
        non_system_messages = [m for m in messages if m.get("role") != "system"]

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": [
                {
                    "role": "assistant" if message.get("role") == "assistant" else "user",
                    "content": _message_to_anthropic_content(message),
                }
                for message in non_system_messages
            ],
        }
        if system_message:
            payload["system"] = system_message.get("content", "")
        if tools:
            payload["tools"] = [_tool_to_anthropic(tool) for tool in tools]

        request = Request(
            _messages_url(self.base_url),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )

        try:
            with urlopen(request, timeout=60) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic HTTP {exc.code}: {body}") from exc

        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                text_parts.append(block.get("text", ""))
            elif block_type == "tool_use":
                tool_calls.append(
                    LLMToolCall(
                        id=block.get("id") or "",
                        name=block.get("name") or "",
                        arguments=json.dumps(block.get("input") or {}, ensure_ascii=False),
                    )
                )

        return LLMResponse(
            message=Message(
                role="assistant",
                content="\n".join(part for part in text_parts if part).strip(),
                tool_calls=tool_calls,
            )
        )
