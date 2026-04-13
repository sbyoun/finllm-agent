from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from agent_runtime.llm.client import LLMResponse, LLMToolCall, Message

_LOGGER = logging.getLogger("agent_runtime.llm.anthropic")


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

        # Rolling prompt caching: system + tools + 직전 turn 경계까지 3~4개의
        # cache_control breakpoint를 찍어 매 iteration마다 캐시 적중하도록 한다.
        # Anthropic 분당 input token 한도는 uncached 기준이라 이게 없으면 멀티턴 에이전트 루프가
        # 금방 429로 터진다 (Opus 30K/min tier).
        anthropic_messages = [
            {
                "role": "assistant" if message.get("role") == "assistant" else "user",
                "content": _message_to_anthropic_content(message),
            }
            for message in non_system_messages
        ]
        # Rolling breakpoint는 messages[-2]에 찍는다. messages[-1]은 <context> dynamic 블록
        # (agent.py가 맨 끝에 append), messages[-2]는 직전 iteration까지 확정된 static 마지막
        # 메시지. 동적 <context>를 제외한 모든 prefix가 캐시 대상이 되며, 다음 iteration에서
        # lookback이 이전 breakpoint 지점을 찾아 그 prefix를 재활용한다.
        if len(anthropic_messages) >= 2:
            target_content = anthropic_messages[-2].get("content")
            if isinstance(target_content, list) and target_content:
                last_block = target_content[-1]
                if isinstance(last_block, dict):
                    last_block["cache_control"] = {"type": "ephemeral"}

        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": anthropic_messages,
        }
        if system_message:
            # system을 블록 리스트로 보내고 cache_control. system prompt는 run 내내 고정이라
            # 한 번만 새로 계산되고 이후엔 전부 cache hit.
            payload["system"] = [
                {
                    "type": "text",
                    "text": system_message.get("content", "") or "",
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if tools:
            tool_blocks = [_tool_to_anthropic(tool) for tool in tools]
            if tool_blocks:
                tool_blocks[-1]["cache_control"] = {"type": "ephemeral"}
            payload["tools"] = tool_blocks

        sys_has_cc = bool(
            isinstance(payload.get("system"), list)
            and payload["system"]
            and payload["system"][0].get("cache_control")
        )
        tools_has_cc = bool(
            payload.get("tools")
            and isinstance(payload["tools"], list)
            and payload["tools"][-1].get("cache_control")
        )
        print(
            f"[anthropic.payload] sys_cc={sys_has_cc} tools_cc={tools_has_cc} "
            f"n_tools={len(payload.get('tools') or [])} n_msgs={len(anthropic_messages)}",
            flush=True,
        )

        request = Request(
            _messages_url(self.base_url),
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": "prompt-caching-2024-07-31",
            },
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )

        try:
            with urlopen(request, timeout=180) as response:
                data = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Anthropic HTTP {exc.code}: {body}") from exc

        import hashlib
        sys_hash = hashlib.sha1(
            json.dumps(payload.get("system") or "", ensure_ascii=False).encode()
        ).hexdigest()[:8]
        tools_hash = hashlib.sha1(
            json.dumps(payload.get("tools") or [], ensure_ascii=False).encode()
        ).hexdigest()[:8]
        msg0_hash = hashlib.sha1(
            json.dumps(anthropic_messages[0] if anthropic_messages else {}, ensure_ascii=False).encode()
        ).hexdigest()[:8]
        print(
            f"[anthropic.hash] sys={sys_hash} tools={tools_hash} msg0={msg0_hash} n_msgs={len(anthropic_messages)}",
            flush=True,
        )
        usage = data.get("usage") or {}
        print(
            f"[anthropic.usage] model={self.model} input={usage.get('input_tokens')} "
            f"cache_creation={usage.get('cache_creation_input_tokens')} "
            f"cache_read={usage.get('cache_read_input_tokens')} "
            f"output={usage.get('output_tokens')}",
            flush=True,
        )

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
