from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from agent_runtime.env import require_env
from agent_runtime.llm.client import LLMResponse, LLMToolCall, Message


def _message_to_gemini_parts(message: dict) -> list[dict[str, Any]]:
    """Convert our event transcript into plain Gemini text history.

    We intentionally do not replay prior tool calls as Gemini native
    functionCall/functionResponse parts. Gemini validates those more strictly
    than OpenAI-style chat history and can reject replayed calls that do not
    carry internal metadata such as thought signatures. We keep tool use in the
    transcript as text observations and only allow the *current* response to
    emit native function calls.
    """

    content = message.get("content", "")
    role = message.get("role")

    if role == "tool":
        tool_name = message.get("name", "tool")
        return [{"text": f"[Result: {tool_name}]\n{content}"}]

    parts: list[dict[str, Any]] = []
    if isinstance(content, str) and content:
        parts.append({"text": content})

    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        # NOTE: 이전엔 `[Called: name] {args_json}` 형태로 JSON 페이로드까지
        # 덤프했는데, Gemini(특히 flash)가 이 패턴을 모방해 최종 답변 자리에
        # SQL/JSON 원본을 그대로 prepend하는 환각이 발생했음. args는 다음 턴의
        # tool result에 이미 반영되니 여기선 호출 사실만 산문으로 남긴다.
        rendered_calls = [
            f"(이전 턴에 {tool_call['function']['name']} 도구를 실행했습니다)"
            for tool_call in tool_calls
        ]
        parts.append({"text": "\n".join(rendered_calls)})
    return parts


def _tool_to_gemini_function(tool: dict) -> dict[str, Any]:
    fn = tool["function"]
    return {
        "name": fn["name"],
        "description": fn.get("description", ""),
        "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
    }


@dataclass(slots=True)
class GeminiClient:
    model: str | None = None
    api_key: str | None = None

    def completion(self, messages: list[dict], tools: list[dict] | None = None) -> LLMResponse:
        model = self.model or require_env("MANAGED_GEMINI_MODEL")
        api_key = self.api_key or require_env("MANAGED_GEMINI_API_KEY")

        system_message = next((m for m in messages if m.get("role") == "system"), None)
        non_system_messages = [m for m in messages if m.get("role") != "system"]

        contents = []
        for message in non_system_messages:
            role = "model" if message.get("role") == "assistant" else "user"
            contents.append(
                {
                    "role": role,
                    "parts": _message_to_gemini_parts(message),
                }
            )

        payload: dict[str, Any] = {
            "contents": contents,
        }
        if system_message:
            payload["system_instruction"] = {
                "parts": [{"text": system_message.get("content", "")}],
            }
        if tools:
            payload["tools"] = [{"functionDeclarations": [_tool_to_gemini_function(tool) for tool in tools]}]

        query = urlencode({"key": api_key})
        request = Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?{query}",
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode("utf-8"),
            method="POST",
        )

        data = self._request_with_retry(request)

        candidates = data.get("candidates") or []
        if not candidates:
            raise RuntimeError(f"Gemini returned no candidates: {data}")

        candidate = candidates[0]
        parts = (((candidate.get("content") or {}).get("parts")) or [])

        text_parts: list[str] = []
        tool_calls: list[LLMToolCall] = []
        for part in parts:
            if "text" in part:
                text_parts.append(part["text"])
            if "functionCall" in part:
                fn = part["functionCall"]
                tool_calls.append(
                    LLMToolCall(
                        name=fn["name"],
                        arguments=json.dumps(fn.get("args", {}), ensure_ascii=False),
                    )
                )

        content = "\n".join(text_parts).strip()

        finish_reason = candidate.get("finishReason", "")
        if not content and not tool_calls:
            logging.warning("Gemini empty response: finishReason=%s, candidate=%s", finish_reason, json.dumps(candidate, ensure_ascii=False)[:500])

        return LLMResponse(
            message=Message(
                role="assistant",
                content=content,
                tool_calls=tool_calls,
            )
        )

    def _request_with_retry(self, request: Request) -> dict[str, Any]:
        attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, attempts + 1):
            try:
                with urlopen(request, timeout=60) as response:
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")
                transient = exc.code >= 500 or exc.code == 429
                last_error = RuntimeError(f"Gemini HTTP {exc.code}: {body}")
                if not transient or attempt == attempts:
                    raise last_error from exc
            except URLError as exc:
                transient = "timed out" in str(exc.reason).lower()
                last_error = RuntimeError(f"Gemini transport error: {exc}")
                if not transient or attempt == attempts:
                    raise last_error from exc
            except TimeoutError as exc:
                last_error = RuntimeError(f"Gemini timeout: {exc}")
                if attempt == attempts:
                    raise last_error from exc

            time.sleep(0.6 * attempt)

        if last_error is not None:
            raise last_error
        raise RuntimeError("Gemini request failed without an error")
